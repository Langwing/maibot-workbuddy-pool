"""dsh-connect-workbuddy 导出文件的解析。

导出格式：一个 JSON 数组（也接受 ``{"accounts": [...]}`` / ``{"data": [...]}`` /
``{"records": [...]}`` 及再套一层 ``data``/``result`` 的信封），每项字段为
``access_token`` / ``refresh_token`` / ``uid`` / ``nickname`` / ``domain`` /
``expiresAt``(毫秒) / ``refreshExpiresAt`` / ``auth_raw``。导入按账号 id
（``uin ?? uid ?? nickname``）去重，缺 access_token 或域名不可识别的记录跳过。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .errors import TransferError
from .http import known_region_of, normalize_domain


def _records_of(parsed: Any, depth: int = 0) -> list | None:
    if isinstance(parsed, list):
        return parsed
    if not isinstance(parsed, dict):
        return None
    for key in ("accounts", "data", "records"):
        value = parsed.get(key)
        if isinstance(value, list):
            return value
    if depth >= 1:
        return None
    for key in ("data", "result"):
        value = parsed.get(key)
        if isinstance(value, dict):
            inner = _records_of(value, depth + 1)
            if inner is not None:
                return inner
    return None


def parse_transfer_text(text: str) -> list[dict]:
    """解析导出文件文本，返回记录数组。"""
    if not text.strip():
        raise TransferError("文件为空")
    try:
        parsed = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        raise TransferError(f"不是合法 JSON：{exc}") from None
    records = _records_of(parsed)
    if records is None:
        raise TransferError('文件必须是账号记录数组，或 {"accounts": [...]} 这类包装')
    for index, item in enumerate(records, start=1):
        if not isinstance(item, dict):
            raise TransferError(f"第 {index} 条不是账号对象")
    return records


def _display(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _epoch_ms(value) -> int:
    """导出契约是毫秒；兼容个别工具写成秒的情况。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if value <= 0:
        return 0
    number = float(value)
    if number < 100_000_000_000:  # 约 1973 年之前，说明是秒
        number *= 1000
    return int(number)


def _uin_of_raw(raw) -> str:
    if not isinstance(raw, dict):
        return ""
    account = raw.get("account") if isinstance(raw.get("account"), dict) else raw
    return _display(account.get("uin"))


def account_id_for(uin: str, uid: str, nickname: str) -> str:
    stable = uin or uid or nickname
    digest = hashlib.sha256(f"workbuddy\0{stable}".encode("utf-8")).hexdigest()
    return digest[:12]


def record_to_account(record: dict) -> tuple[dict | None, str]:
    """把一条导出记录转换成账号池条目；无法导入时返回 ``(None, 原因)``。"""
    access_token = record.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        return None, "缺少 access_token"

    domain = normalize_domain(record.get("domain"))
    realm = known_region_of(domain)
    if realm is None:
        return None, f"无法识别 domain：{domain or '（空）'}"

    uid = _display(record.get("uid"))
    nickname = _display(record.get("nickname"))
    uin = _uin_of_raw(record.get("auth_raw"))
    if not (uin or uid or nickname):
        return None, "缺少 uid/uin/昵称，无法生成账号 id"

    refresh_token = record.get("refresh_token")
    return (
        {
            "id": account_id_for(uin, uid, nickname),
            "uid": uid,
            "uin": uin,
            "nickname": nickname,
            "domain": domain,
            "realm": realm,
            "access_token": access_token.strip(),
            "refresh_token": refresh_token.strip() if isinstance(refresh_token, str) else "",
            "expires_at": _epoch_ms(record.get("expiresAt")),
            "refresh_expires_at": _epoch_ms(record.get("refreshExpiresAt")),
            "enterprise_id": _display(record.get("enterpriseId")),
        },
        "",
    )
