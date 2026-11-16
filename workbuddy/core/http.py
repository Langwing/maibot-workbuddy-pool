"""WorkBuddy 上游的公共 HTTP 约定：站点、请求头、响应信封、域名分区。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

ORIGIN_CN = "https://www.codebuddy.cn"
ORIGIN_GLOBAL = "https://www.workbuddy.ai"

# 与参考实现保持一致：CN 聊天网关是 copilot.tencent.com，账单/配置在
# www.codebuddy.cn；国际版按凭据域名分家。
CN_CHAT_BASE = "https://copilot.tencent.com"
CN_BILLING_BASE = "https://www.codebuddy.cn"
GLOBAL_BASE = "https://www.workbuddy.ai"
GLOBAL_CODEBUDDY_BASE = "https://www.codebuddy.ai"

CLIENT_VERSION = "5.5.4"
CLI_VERSION = "2.137.1"

STATE_PATH = "/v2/plugin/auth/state"
TOKEN_PATH = "/v2/plugin/auth/token"
ACCOUNT_PATH = "/v2/plugin/login/account"
REFRESH_PATH = "/v2/plugin/auth/token/refresh"
CHAT_PATH = "/v2/chat/completions"


def origin_for(realm: str) -> str:
    return ORIGIN_GLOBAL if realm == "global" else ORIGIN_CN


def accept_language(realm: str) -> str:
    return "en-US" if realm == "global" else "zh-CN"


def common_headers(realm: str, token: str | None = None) -> dict[str, str]:
    origin = origin_for(realm)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": origin,
        "Referer": origin + "/",
        "User-Agent": f"WorkBuddy/{CLIENT_VERSION} WorkBuddy/{CLIENT_VERSION} CLI/{CLI_VERSION}",
        "X-CodeBuddy-Request": "1",
        "Accept-Language": accept_language(realm),
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def refresh_headers(realm: str, refresh_token: str) -> dict[str, str]:
    """刷新端点专用头；``X-Refresh-Token`` 只允许出现在这里。"""
    headers = common_headers(realm)
    headers.pop("Authorization", None)
    headers["X-Refresh-Token"] = refresh_token
    headers["X-Auth-Refresh-Source"] = "plugin"
    return headers


def envelope(body) -> tuple[object, dict, object]:
    """从响应信封里取 ``(code, data, msg)``，对畸形结构保持宽容。"""
    if not isinstance(body, dict):
        return None, {}, None
    data = body.get("data")
    return body.get("code"), data if isinstance(data, dict) else {}, body.get("msg")


def parse_retry_after(resp) -> float | None:
    """把 ``Retry-After`` 解析成秒数（支持秒数与 HTTP 日期）。"""
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


def stable_account_id(uid: str, purpose: str) -> str:
    digest = hashlib.sha256(f"wb2a:{purpose}:{uid}".encode("utf-8")).hexdigest()
    return digest[:36]


def normalize_domain(value) -> str:
    """把导出文件里的域名整理成裸主机名。"""
    if not isinstance(value, str):
        return ""
    domain = value.strip().lower()
    if "://" in domain:
        domain = domain.split("://", 1)[1]
    return domain.split("/", 1)[0].strip(".")


def region_of(domain: str) -> str:
    """宽松分区：无法识别时按国内版处理。"""
    lowered = normalize_domain(domain)
    if lowered == "workbuddy.ai" or lowered.endswith(".workbuddy.ai"):
        return "global"
    if lowered == "codebuddy.ai" or lowered.endswith(".codebuddy.ai"):
        return "global"
    return "cn"


def known_region_of(domain: str) -> str | None:
    """严格分区：只有能明确归属的域名才返回区域。

    导入外部文件时必须用这个：``region_of`` 的兜底会把未知域名当成国内版，
    那对上游签发的域名成立，对别人文件里写的值不成立。
    """
    lowered = normalize_domain(domain)
    if lowered == "":
        return None
    if lowered == "workbuddy.ai" or lowered.endswith(".workbuddy.ai"):
        return "global"
    if lowered == "codebuddy.ai" or lowered.endswith(".codebuddy.ai"):
        return "global"
    if lowered == "workbuddy.cn" or lowered.endswith(".workbuddy.cn"):
        return "cn"
    if lowered == "codebuddy.cn" or lowered.endswith(".codebuddy.cn"):
        return "cn"
    if lowered == "copilot.tencent.com" or lowered.endswith(".copilot.tencent.com"):
        return "cn"
    return None


def chat_base_for(realm: str, domain: str, cn_base: str, global_base: str) -> str:
    """按账号区域选择聊天/刷新网关。"""
    if realm == "global":
        lowered = normalize_domain(domain)
        if lowered == "codebuddy.ai" or lowered.endswith(".codebuddy.ai"):
            return GLOBAL_CODEBUDDY_BASE
        return (global_base or GLOBAL_BASE).rstrip("/")
    return (cn_base or CN_CHAT_BASE).rstrip("/")
