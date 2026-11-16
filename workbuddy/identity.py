"""每个账号稳定的设备身份。

WorkBuddy 客户端会在请求里带 ``X-Machine-ID`` / ``X-Session-ID``。dsh 的做法是
由本机盐 + uid 派生，绝不采集真实硬件信息；这里沿用同一条思路，把盐持久化到
插件数据目录，保证同一个账号每次发出的值一致，同时允许在 WebUI 里逐字段覆盖。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from pathlib import Path
from typing import Any

log = logging.getLogger("_maibot_plugin_local_workbuddy_pool.identity")

DEVICE_FILENAME = "device.json"


class DeviceIdentity:
    """账号级设备身份的派生与存储。"""

    def __init__(self, data_dir: str | Path) -> None:
        self._path = Path(data_dir) / DEVICE_FILENAME
        self._doc = self._load()
        if not self._doc.get("salt"):
            self._doc["salt"] = secrets.token_hex(16)
            self._save()

    # ------------------------------------------------------------------ 持久化
    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._doc, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            log.warning("保存设备身份失败：%s", exc)

    # ------------------------------------------------------------------ 派生
    @property
    def salt(self) -> str:
        return str(self._doc.get("salt") or "")

    def derive(self, uid: str, purpose: str) -> str:
        if not uid or not self.salt:
            return ""
        digest = hmac.new(
            self.salt.encode("utf-8"),
            f"workbuddy:{purpose}:{uid}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return digest[:32]

    def _override(self, account_id: str) -> dict[str, str]:
        overrides = self._doc.get("overrides")
        if not isinstance(overrides, dict):
            return {}
        item = overrides.get(account_id)
        return item if isinstance(item, dict) else {}

    def headers_for(self, account) -> dict[str, str]:
        """返回该账号应带的设备头；盐缺失时整对不发。"""
        uid = getattr(account, "uid", "") or ""
        override = self._override(getattr(account, "id", ""))
        machine = override.get("machine_id") or self.derive(uid, "machine")
        session = override.get("session_id") or self.derive(uid, "session")
        headers: dict[str, str] = {}
        if machine:
            headers["X-Machine-ID"] = machine
        if session:
            headers["X-Session-ID"] = session
        return headers

    # ------------------------------------------------------------------ 管理
    def state(self, accounts: list) -> dict[str, Any]:
        rows = []
        for account in accounts:
            override = self._override(account.id)
            rows.append(
                {
                    "id": account.id,
                    "nickname": account.label,
                    "uid_masked": account.masked_uid,
                    "machine_id": override.get("machine_id") or self.derive(account.uid, "machine"),
                    "session_id": override.get("session_id") or self.derive(account.uid, "session"),
                    "derived": not override,
                    "overridden": bool(override),
                }
            )
        return {"salt_hint": self.salt[:6] + "…" if self.salt else "", "accounts": rows}

    def set_override(self, account_id: str, machine_id: str, session_id: str) -> None:
        overrides = self._doc.setdefault("overrides", {})
        if not isinstance(overrides, dict):
            overrides = {}
            self._doc["overrides"] = overrides
        machine_id = (machine_id or "").strip()
        session_id = (session_id or "").strip()
        if not machine_id and not session_id:
            overrides.pop(account_id, None)
        else:
            overrides[account_id] = {"machine_id": machine_id, "session_id": session_id}
        self._save()

    def regenerate_salt(self) -> None:
        self._doc["salt"] = secrets.token_hex(16)
        self._doc.pop("overrides", None)
        self._save()
