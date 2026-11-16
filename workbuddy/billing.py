"""WorkBuddy 计费只读接口：积分余额与模型队列。

全部是只读端点，不包含签到/福利领取等任何写操作。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from .core.errors import UpstreamError
from .core.http import (
    CLIENT_VERSION,
    CN_BILLING_BASE,
    GLOBAL_BASE,
    GLOBAL_CODEBUDDY_BASE,
    accept_language,
    normalize_domain,
    origin_for,
)

CREDITS_PATH = "/v2/billing/meter/get-user-resource"
ENTERPRISE_CREDITS_PATH = "/billing/meter/get-enterprise-user-usage"
QUEUE_STATUS_PATH = "/v2/chat/queue/status"

SOON_SECONDS = 3 * 24 * 3600


def billing_base(account) -> str:
    if getattr(account, "realm", "cn") == "global":
        domain = normalize_domain(getattr(account, "domain", ""))
        if domain == "codebuddy.ai" or domain.endswith(".codebuddy.ai"):
            return GLOBAL_CODEBUDDY_BASE
        return GLOBAL_BASE
    return CN_BILLING_BASE


def _base_headers(account, identity=None) -> dict[str, str]:
    realm = getattr(account, "realm", "cn")
    origin = origin_for(realm)
    headers = {
        "Authorization": "Bearer " + (account.access_token or ""),
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": f"WorkBuddy/{CLIENT_VERSION}",
        "X-Requested-With": "XMLHttpRequest",
        "X-CodeBuddy-Request": "1",
        "Accept-Language": accept_language(realm),
        "Origin": origin,
        "Referer": origin + "/",
        "X-Agent-Purpose": "conversation",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Version": CLIENT_VERSION,
        "X-Product": "WorkBuddy",
    }
    if account.uid:
        headers["X-User-Id"] = account.uid
    if account.enterprise_id:
        headers["X-Enterprise-Id"] = account.enterprise_id
        headers["X-Tenant-Id"] = account.enterprise_id
    if account.domain:
        headers["X-Domain"] = account.domain
    if identity is not None:
        headers.update(identity.headers_for(account))
    return headers


async def _post_json(
    pool,
    account,
    url: str,
    body: dict[str, Any],
    *,
    identity=None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    token = account.access_token
    for attempt in (1, 2):
        headers = _base_headers(account, identity)
        if extra_headers:
            headers.update(extra_headers)
        async with pool.http.post(url, json=body, headers=headers) as resp:
            if resp.status == 401 and attempt == 1:
                await resp.read()
                await pool.refresh(account, token)
                token = account.access_token
                continue
            try:
                payload = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                await resp.read()
                raise UpstreamError(resp.status, None) from None
            if resp.status != 200:
                code = payload.get("code") if isinstance(payload, dict) else None
                raise UpstreamError(resp.status, code)
            if not isinstance(payload, dict):
                raise UpstreamError(resp.status, None)
            code = payload.get("code")
            if code not in (0, None):
                raise UpstreamError(200, code)
            data = payload.get("data")
            return data if isinstance(data, dict) else {}
    raise UpstreamError(401, None)


# --------------------------------------------------------------------------
# 积分
# --------------------------------------------------------------------------
def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _precise(account: dict[str, Any], key: str) -> float:
    raw = account.get(f"{key}Precise")
    if isinstance(raw, str) and raw.strip():
        try:
            return float(raw)
        except ValueError:
            pass
    if isinstance(raw, (int, float)):
        return float(raw)
    return _number(account.get(key))


def _parse_instant(value: Any) -> float | None:
    if isinstance(value, (int, float)) and value > 1_000_000_000_000:
        return float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace(" ", "T")
        for parser in (datetime.fromisoformat,):
            try:
                return parser(text).timestamp() * 1000
            except ValueError:
                continue
    return None


async def fetch_credits(pool, account, identity=None) -> dict[str, Any]:
    """读取账号可用积分；企业账号走企业表。"""
    base = billing_base(account)
    if account.enterprise_id:
        data = await _post_json(pool, account, base + ENTERPRISE_CREDITS_PATH, {}, identity=identity)
        wrapper = data.get("Data") if isinstance(data.get("Data"), dict) else data
        limit = wrapper.get("limitNum")
        used = wrapper.get("credit")
        if not isinstance(limit, (int, float)) or not isinstance(used, (int, float)):
            raise UpstreamError(200, None)
        remain = max(0.0, float(limit) - float(used))
        return {
            "kind": "enterprise",
            "total": round(remain, 2),
            "expiring_soon": 0,
            "packages": [
                {
                    "name": "企业积分",
                    "remain": round(remain, 2),
                    "size": round(float(limit), 2),
                    "monthly": True,
                    "refresh_at": _parse_instant(wrapper.get("cycleResetTime")),
                }
            ],
        }

    now = datetime.now()
    fmt = "%Y-%m-%d %H:%M:%S"
    body = {
        "PageNumber": 1,
        "PageSize": 100,
        "ProductCode": "p_tcaca",
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": now.strftime(fmt),
        "PackageEndTimeRangeEnd": (now + timedelta(days=365 * 101)).strftime(fmt),
    }
    data = await _post_json(pool, account, base + CREDITS_PATH, body, identity=identity)
    response = data.get("Response") if isinstance(data.get("Response"), dict) else {}
    inner = response.get("Data") if isinstance(response.get("Data"), dict) else {}
    rows = inner.get("Accounts") if isinstance(inner.get("Accounts"), list) else []

    total = 0.0
    expiring_soon = 0.0
    packages: list[dict[str, Any]] = []
    now_ms = time.time() * 1000
    for item in rows:
        if not isinstance(item, dict):
            continue
        monthly = _number(item.get("CapacityType")) == 4
        size = _precise(item, "CycleCapacitySize") or _precise(item, "CapacitySize")
        has_cycle = "CycleCapacityRemain" in item or "CycleCapacityRemainPrecise" in item
        remain = max(0.0, _precise(item, "CycleCapacityRemain") if has_cycle else _precise(item, "CapacityRemain"))
        cycle_end = _parse_instant(item.get("CycleEndTime"))
        expires_at = None if monthly else (_parse_instant(item.get("ExpiredTime")) or cycle_end)
        if not monthly and (remain <= 0 or (expires_at is not None and expires_at <= now_ms)):
            continue
        total += remain
        if expires_at is not None and expires_at - now_ms <= SOON_SECONDS * 1000:
            expiring_soon += remain
        packages.append(
            {
                "name": item.get("PackageName") if isinstance(item.get("PackageName"), str) else "(未命名)",
                "remain": round(remain, 2),
                "size": round(size, 2),
                "monthly": monthly,
                "refresh_at": cycle_end + 1000 if monthly and cycle_end is not None else None,
                "expires_at": expires_at,
            }
        )
    return {
        "kind": "personal",
        "total": round(total, 2),
        "expiring_soon": round(expiring_soon, 2),
        "packages": packages,
    }


# --------------------------------------------------------------------------
# 队列 / 负载
# --------------------------------------------------------------------------
async def fetch_queue_status(pool, account, model: str, identity=None) -> dict[str, Any]:
    """读取某模型当前的排队状态（只读）。"""
    url = pool.chat_base(account) + QUEUE_STATUS_PATH
    headers = {"X-Conversation-Request-ID": "%032x" % int(time.time() * 1_000_000)}
    data = await _post_json(pool, account, url, {"model": model}, identity=identity, extra_headers=headers)
    return {
        "status": str(data.get("status") or ""),
        "message": str(data.get("message") or ""),
        "position": data.get("queue_position"),
        "size": data.get("queue_size"),
    }
