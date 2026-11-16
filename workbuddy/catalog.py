"""WorkBuddy 模型目录：抓取、解析、缓存与 OpenAI ``/v1/models`` 投影。

移植自 dsh-connect-workbuddy 的 ``src/catalog.ts`` 与 ``src/upstream.ts``：

* 国内区优先读 ``/v3/config``（App 通道，带倍率与促销），失败降级到
  ``/v2/enterprises/personal/models``；
* 国际区分别以 CLI 与 App 两个 User-Agent 各读一次 ``/v3/config``，再按 id 合并
  （两份文档互不包含）；
* 目录落盘到 ``<插件数据目录>/catalog.json``，供本地 ``/v1/models`` 随时读取，
  上游抖动或未启动时也能给 MaiBot 的「获取AI模型」一个稳定答案。

目录只做只读 GET，不消耗积分。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core.errors import UpstreamError
from .core.http import (
    CN_BILLING_BASE,
    GLOBAL_BASE,
    GLOBAL_CODEBUDDY_BASE,
    normalize_domain,
    origin_for,
)

log = logging.getLogger("_maibot_plugin_local_workbuddy_pool.catalog")

CATALOG_FILENAME = "catalog.json"

GLOBAL_CONFIG_PATH = "/v3/config"
PERSONAL_MODELS_PATH = "/v2/enterprises/personal/models"

# 渠道选择靠 User-Agent，不靠域名：同一个 token 换 UA 会答不同的文档。
PRODUCT_UA = "WorkBuddy/5.8.3 WorkBuddy/5.8.3 CLI/2.161.0"
APP_CHANNEL_UA = "WorkBuddy/5.8.3"
CLI_CHANNEL_UA = "CLI/2.63.2 CodeBuddy/2.63.2"

#: 上游的 ``auto`` / ``default-model`` 是路由，不是模型，目录里一律剔除。
ROUTER_IDS = frozenset({"auto", "default-model"})

#: 上游不可用时的兜底目录（只保留 id/名称/窗口，绝不写死价格）。
FALLBACK_CN: tuple[dict[str, Any], ...] = (
    {"id": "hy3", "name": "Hy3", "context_window": 192_000, "max_tokens": 64_000},
    {"id": "glm-5v-turbo", "name": "GLM-5v-Turbo", "context_window": 200_000, "max_tokens": 64_000},
    {"id": "glm-5.3", "name": "GLM-5.3", "context_window": 1_000_000, "max_tokens": 48_000},
    {"id": "glm-5.2", "name": "GLM-5.2", "context_window": 1_000_000, "max_tokens": 48_000},
    {"id": "minimax-m3", "name": "MiniMax-M3", "context_window": 512_000, "max_tokens": 128_000},
    {"id": "kimi-k3-1", "name": "Kimi-K3", "context_window": 1_000_000, "max_tokens": 32_000},
    {"id": "kimi-k2.7", "name": "Kimi-K2.7-Code", "context_window": 256_000, "max_tokens": 32_000},
    {"id": "deepseek-v4-flash", "name": "Deepseek-V4-Flash", "context_window": 1_000_000, "max_tokens": 50_000},
    {"id": "deepseek-v4-pro", "name": "Deepseek-V4-Pro", "context_window": 1_000_000, "max_tokens": 50_000},
)

FALLBACK_GLOBAL: tuple[dict[str, Any], ...] = (
    {"id": "fast-model", "name": "Fast", "context_window": 200_000, "max_tokens": 32_000},
    {"id": "balanced-model", "name": "Balanced", "context_window": 256_000, "max_tokens": 32_000},
    {"id": "primary-model", "name": "Primary", "context_window": 272_000, "max_tokens": 72_000},
    {"id": "deep-model", "name": "Deep", "context_window": 176_000, "max_tokens": 24_000},
    {"id": "deepseek-v4.1-flash", "name": "Deepseek-V4.1-Flash", "context_window": 1_000_000, "max_tokens": 128_000},
    {"id": "gemini-3.5-flash", "name": "Gemini-3.5-Flash", "context_window": 1_000_000, "max_tokens": 65_536},
    {"id": "glm-5.3", "name": "GLM-5.3", "context_window": 1_000_000, "max_tokens": 48_000},
    {"id": "kimi-k3", "name": "Kimi-K3", "context_window": 1_000_000, "max_tokens": 32_000},
)


def fallback_models(region: str) -> list[dict[str, Any]]:
    source = FALLBACK_GLOBAL if region == "global" else FALLBACK_CN
    return [dict(item) for item in source]


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
def parse_credit_multiplier(value: Any) -> float | None:
    """从上游 ``credits`` 字符串（形如 ``x0.79``）解析倍率。"""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    index = text.find("x")
    if index < 0:
        return None
    digits = []
    dot = False
    for char in text[index + 1 :]:
        if char.isdigit():
            digits.append(char)
        elif char == "." and not dot:
            digits.append(char)
            dot = True
        elif digits:
            break
        else:
            continue
    if not digits:
        return None
    try:
        parsed = float("".join(digits))
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def parse_reasoning(value: Any) -> dict[str, Any] | None:
    """解析 ``reasoning`` 元数据；单数形式（只有 ``effort``）折成同一形状。"""
    if not isinstance(value, dict):
        return None
    effort = value.get("effort") if isinstance(value.get("effort"), str) else None
    supported = value.get("supportedEfforts")
    if isinstance(supported, list):
        levels = [item for item in supported if isinstance(item, str)]
    elif effort:
        levels = ["low", "medium", "high", "xhigh", "max"]
    else:
        levels = []
    default = value.get("defaultEffort") if isinstance(value.get("defaultEffort"), str) else effort
    can_disable = value.get("canDisableThinking")
    if not isinstance(can_disable, bool):
        can_disable = True if effort and not supported else None
    if not levels and default is None and can_disable is None:
        return None
    result: dict[str, Any] = {}
    if levels:
        result["supported_efforts"] = levels
    if default:
        result["default_effort"] = default
    if isinstance(can_disable, bool):
        result["can_disable_thinking"] = can_disable
    return result or None


def parse_upstream_model(value: Any) -> dict[str, Any] | None:
    """解析一条目录项；缺少 token 上限的条目直接丢弃。"""
    if not isinstance(value, dict):
        return None
    model_id = value.get("id")
    if not isinstance(model_id, str) or not model_id or value.get("disabled") is True:
        return None
    context = value.get("maxInputTokens")
    max_tokens = value.get("maxOutputTokens")
    context = int(context) if isinstance(context, (int, float)) else 0
    max_tokens = int(max_tokens) if isinstance(max_tokens, (int, float)) else 0
    if context <= 0 or max_tokens <= 0:
        return None
    name = value.get("name") if isinstance(value.get("name"), str) else model_id
    platform_images: bool | None = None
    if value.get("disabledMultimodal") is True:
        platform_images = False
    elif isinstance(value.get("supportsImages"), bool):
        platform_images = bool(value["supportsImages"])
    entry: dict[str, Any] = {
        "id": model_id,
        "name": name or model_id,
        "context_window": context,
        "max_tokens": max_tokens,
    }
    if platform_images is not None:
        # 仅作平台声明留档；图片能力以用户手动勾选为准，不做推断。
        entry["platform_images"] = platform_images
    multiplier = parse_credit_multiplier(value.get("credits"))
    if multiplier is not None:
        entry["credit_multiplier"] = multiplier
    reasoning = parse_reasoning(value.get("reasoning"))
    if reasoning is not None:
        entry["reasoning"] = reasoning
    if isinstance(value.get("supportsToolCall"), bool):
        entry["supports_tool_call"] = bool(value["supportsToolCall"])
    for key, out in (("descriptionZh", "description"), ("descriptionEn", "description_en")):
        text = value.get(key)
        if isinstance(text, str) and text:
            entry[out] = text
    return entry


def parse_promos(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """把 ``modelPromotions`` / ``modelTiers`` 归到模型 id 下（顺序保留）。"""
    result: dict[str, list[dict[str, Any]]] = {}

    def add(model_ids: Any, badge: dict[str, Any]) -> None:
        if not isinstance(model_ids, list):
            return
        for model_id in model_ids:
            if isinstance(model_id, str) and model_id:
                result.setdefault(model_id, []).append(badge)

    for key, kind in (("modelPromotions", "promotion"), ("modelTiers", "tier")):
        entries = data.get(key)
        for raw in entries if isinstance(entries, list) else []:
            if not isinstance(raw, dict):
                continue
            badge = {"kind": kind}
            for field_name in ("name", "title", "label", "detail", "color"):
                text = raw.get(field_name)
                if isinstance(text, str) and text:
                    badge[field_name] = text
            for field_name in ("startTime", "endTime", "start_time", "end_time"):
                text = raw.get(field_name)
                if isinstance(text, str) and text:
                    badge[field_name] = text
            add(raw.get("modelIds") or raw.get("model_ids"), badge)
    return result


def select_cli_models(
    raw_models: Any,
    agents: Any,
    keep_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """按 ``cli`` agent 的花名册投影聊天目录；花名册缺失时用全部条目。"""
    by_id: dict[str, dict[str, Any]] = {}
    for item in raw_models if isinstance(raw_models, list) else []:
        parsed = parse_upstream_model(item)
        if parsed is not None:
            by_id[parsed["id"]] = parsed
    cli_ids: list[str] | None = None
    for agent in agents if isinstance(agents, list) else []:
        if isinstance(agent, dict) and agent.get("name") == "cli" and isinstance(agent.get("models"), list):
            cli_ids = [item for item in agent["models"] if isinstance(item, str)]
            break
    ids = cli_ids if cli_ids else list(by_id.keys())
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for model_id in ids:
        if model_id in ROUTER_IDS or model_id in seen:
            continue
        entry = by_id.get(model_id)
        if entry is not None:
            selected.append(entry)
            seen.add(model_id)
    for model_id in keep_ids or []:
        if model_id in seen or model_id in ROUTER_IDS:
            continue
        entry = by_id.get(model_id)
        if entry is not None:
            selected.append(entry)
            seen.add(model_id)
    return selected


def with_promos(models: list[dict[str, Any]], data: dict[str, Any]) -> list[dict[str, Any]]:
    promos = parse_promos(data)
    if not promos:
        return models
    for model in models:
        badges = promos.get(model["id"])
        if badges:
            model["promos"] = badges
    return models


def merge_rosters(primary: list[dict[str, Any]], secondary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 id 合并两份目录，``primary`` 的同名条目优先。"""
    merged = list(primary)
    seen = {item["id"] for item in primary}
    for item in secondary:
        if item["id"] not in seen:
            merged.append(item)
            seen.add(item["id"])
    return merged


# --------------------------------------------------------------------------
# 抓取
# --------------------------------------------------------------------------
def _region_of(account) -> str:
    return "global" if getattr(account, "realm", "cn") == "global" else "cn"


def _catalog_base(account) -> str:
    if _region_of(account) == "global":
        domain = normalize_domain(getattr(account, "domain", ""))
        if domain == "codebuddy.ai" or domain.endswith(".codebuddy.ai"):
            return GLOBAL_CODEBUDDY_BASE
        return GLOBAL_BASE
    return CN_BILLING_BASE


def _catalog_headers(account, user_agent: str) -> dict[str, str]:
    headers = {
        "Authorization": "Bearer " + account.access_token,
        "Accept": "application/json",
        "Origin": origin_for(_region_of(account)),
        "Referer": origin_for(_region_of(account)) + "/",
        "User-Agent": user_agent,
        "X-Product": "SaaS",
        "X-Requested-With": "XMLHttpRequest",
        "Connection": "close",
    }
    if account.uid:
        headers["X-User-Id"] = account.uid
    if account.domain:
        headers["X-Domain"] = account.domain
    return headers


class CatalogService:
    """模型目录的抓取与缓存。"""

    def __init__(
        self,
        pool,
        data_dir: str | Path,
        *,
        ttl_seconds: int = 900,
        keep_ids: list[str] | None = None,
    ) -> None:
        self._pool = pool
        self._path = Path(data_dir) / CATALOG_FILENAME
        self._ttl = max(60, int(ttl_seconds))
        self._keep_ids = list(keep_ids or [])
        self._lock = asyncio.Lock()
        self._doc: dict[str, Any] = self._load()

    # ------------------------------------------------------------------ 缓存
    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self, doc: dict[str, Any]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            log.warning("保存模型目录失败：%s", exc)

    def snapshot(self) -> dict[str, Any]:
        return self._doc

    @property
    def updated_at(self) -> float:
        value = self._doc.get("updated_at")
        return float(value) if isinstance(value, (int, float)) else 0.0

    @property
    def stale(self) -> bool:
        return time.time() - self.updated_at > self._ttl

    def models(self, region: str | None = None) -> list[dict[str, Any]]:
        regions = self._doc.get("regions")
        if not isinstance(regions, dict):
            return []
        if region:
            items = regions.get(region)
            return [dict(item) for item in items] if isinstance(items, list) else []
        ordered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for key in ("cn", "global"):
            for item in regions.get(key) or []:
                if isinstance(item, dict) and item.get("id") not in seen:
                    ordered.append(dict(item))
                    seen.add(item.get("id"))
        return ordered

    def openai_payload(self) -> dict[str, Any]:
        """OpenAI ``/v1/models`` 形状；``owned_by`` 固定为 workbuddy。"""
        data = []
        for model in self.models():
            data.append(
                {
                    "id": model["id"],
                    "object": "model",
                    "created": int(self.updated_at) or 0,
                    "owned_by": "workbuddy",
                    "name": model.get("name") or model["id"],
                    "context_window": model.get("context_window", 0),
                    "max_tokens": model.get("max_tokens", 0),
                }
            )
        return {"object": "list", "data": data}

    # ------------------------------------------------------------------ 刷新
    async def refresh(self) -> dict[str, Any]:
        async with self._lock:
            accounts = self._pool.candidates() or self._pool.accounts
            regions: dict[str, list[dict[str, Any]]] = {}
            errors: dict[str, str] = {}
            for account in accounts:
                realm = _region_of(account)
                if realm in regions:
                    continue
                try:
                    regions[realm] = await self._fetch_account(account)
                except UpstreamError as exc:
                    errors[realm] = str(exc)
                    log.warning("读取 %s 模型目录失败（%s）：%s", realm, account.label, exc)
                except Exception as exc:  # noqa: BLE001
                    errors[realm] = str(exc)
                    log.warning("读取 %s 模型目录异常（%s）：%s", realm, account.label, exc)
            if not regions:
                raise RuntimeError("所有账号都读取不到模型目录：" + json.dumps(errors, ensure_ascii=False))
            for realm in ("cn", "global"):
                if realm not in regions:
                    continue
                if not regions[realm]:
                    regions[realm] = fallback_models(realm)
            doc = {
                "updated_at": time.time(),
                "updated_text": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                "regions": regions,
                "errors": errors,
            }
            self._doc = doc
            self._save(doc)
            total = len(self.models())
            log.info("WorkBuddy 模型目录已刷新：%d 个模型（%s）", total, ", ".join(regions))
            return doc

    # ------------------------------------------------------------------ 上游
    async def _fetch_account(self, account) -> list[dict[str, Any]]:
        if _region_of(account) == "global":
            return await self._fetch_global(account)
        return await self._fetch_cn(account)

    async def _read(self, account, path: str, user_agent: str) -> dict[str, Any]:
        url = _catalog_base(account) + path
        token = account.access_token
        for attempt in (1, 2):
            async with self._pool.http.get(url, headers=_catalog_headers(account, user_agent)) as resp:
                if resp.status == 401 and attempt == 1:
                    await resp.read()
                    await self._pool.refresh(account, token)
                    token = account.access_token
                    continue
                try:
                    body = await resp.json(content_type=None)
                except Exception:  # noqa: BLE001
                    await resp.read()
                    raise UpstreamError(resp.status, None) from None
                if resp.status != 200:
                    code = body.get("code") if isinstance(body, dict) else None
                    raise UpstreamError(resp.status, code)
                if not isinstance(body, dict):
                    raise UpstreamError(resp.status, None)
                code = body.get("code")
                if code not in (0, None):
                    raise UpstreamError(200, code)
                data = body.get("data")
                return data if isinstance(data, dict) else {}
        raise UpstreamError(401, None)

    async def _fetch_cn(self, account) -> list[dict[str, Any]]:
        for path in (GLOBAL_CONFIG_PATH, PERSONAL_MODELS_PATH):
            try:
                data = await self._read(account, path, PRODUCT_UA)
                models = select_cli_models(data.get("models"), data.get("agents"), self._keep_ids)
                if models:
                    return with_promos(models, data)
                log.info("国内目录 %s 为空，尝试下一条路径", path)
            except UpstreamError as exc:
                log.info("国内目录 %s 不可用：%s", path, exc)
        raise UpstreamError(200, None)

    async def _fetch_global(self, account) -> list[dict[str, Any]]:
        cli_roster: list[dict[str, Any]] | None = None
        app_roster: list[dict[str, Any]] | None = None
        try:
            data = await self._read(account, GLOBAL_CONFIG_PATH, CLI_CHANNEL_UA)
            cli_roster = with_promos(
                select_cli_models(data.get("models"), data.get("agents"), self._keep_ids), data
            )
        except UpstreamError as exc:
            log.info("国际版 CLI 目录读取失败：%s", exc)
        try:
            data = await self._read(account, GLOBAL_CONFIG_PATH, APP_CHANNEL_UA)
            app_roster = with_promos(
                select_cli_models(data.get("models"), data.get("agents"), self._keep_ids), data
            )
        except UpstreamError as exc:
            log.info("国际版 App 目录读取失败：%s", exc)
        if cli_roster and app_roster:
            return merge_rosters(cli_roster, app_roster)
        if cli_roster or app_roster:
            return cli_roster or app_roster or []
        raise UpstreamError(200, None)
