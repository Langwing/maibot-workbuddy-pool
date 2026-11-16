"""WorkBuddy OAuth 登录：为账号池新增一个账号。

``POST /v2/plugin/auth/state?platform=CLI`` 取授权链接；随后轮询
``GET /v2/plugin/auth/token?state=`` 直到拿到 accessToken，再用它读
``/v2/plugin/login/account`` 取 uid/昵称。``state`` 只保存在内存里。
"""

from __future__ import annotations

import asyncio
import logging
import time

from .core.errors import LoginStateError, LoginTimeoutError, UpstreamError
from .core.http import (
    ACCOUNT_PATH,
    CN_CHAT_BASE,
    STATE_PATH,
    TOKEN_PATH,
    common_headers,
    envelope,
    known_region_of,
    normalize_domain,
)
from .core.transfer import account_id_for

log = logging.getLogger("_maibot_plugin_local_workbuddy_pool.login")

_PENDING_MARKERS = ("login ing", "login-ing", "waiting")


def _is_pending(code, msg) -> bool:
    if code == 0:
        return True
    if isinstance(msg, str):
        low = msg.lower()
        return any(marker in low for marker in _PENDING_MARKERS)
    return False


class LoginFlow:
    """一次性的交互式登录流程。"""

    def __init__(self, http, *, base: str = CN_CHAT_BASE, poll_interval: float = 1.0) -> None:
        self._http = http
        self._base = (base or CN_CHAT_BASE).rstrip("/")
        self._poll_interval = poll_interval
        self._pending_state: str | None = None
        self._epoch = 0

    async def begin(self) -> dict:
        self._epoch += 1
        self._pending_state = None
        resp = await self._http.post(
            self._base + STATE_PATH,
            params={"platform": "CLI"},
            json={},
            headers=common_headers("cn"),
            allow_redirects=False,
        )
        try:
            body = await resp.json(content_type=None)
            status = resp.status
        except Exception:
            await resp.read()
            raise UpstreamError(resp.status, None) from None
        finally:
            resp.release()
        code, data, _ = envelope(body)
        if status != 200 or code != 0:
            raise UpstreamError(status, code)
        state = data.get("state")
        auth_url = data.get("authUrl")
        if not state or not isinstance(auth_url, str) or not auth_url.startswith("https://"):
            raise UpstreamError(status, code)
        self._pending_state = str(state)
        return {"state": str(state), "authUrl": auth_url}

    async def wait(self, state: str, timeout: float = 600.0) -> dict:
        """轮询直到登录完成，返回可直接交给账号池的条目。"""
        if not state or state != self._pending_state:
            raise LoginStateError("登录 state 不是当前流程签发的")
        epoch = self._epoch
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        data = None
        try:
            while True:
                if epoch != self._epoch:
                    raise LoginStateError("登录流程已被新的登录取代")
                resp = await self._http.get(
                    self._base + TOKEN_PATH,
                    params={"state": state},
                    headers=common_headers("cn"),
                    allow_redirects=False,
                )
                try:
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        await resp.read()
                        raise UpstreamError(resp.status, None) from None
                    status = resp.status
                finally:
                    resp.release()
                code, payload, msg = envelope(body)
                if status != 200:
                    raise UpstreamError(status, code)
                if code == 0 and payload.get("accessToken"):
                    data = payload
                    break
                if _is_pending(code, msg):
                    if loop.time() >= deadline:
                        raise LoginTimeoutError("等待授权超时")
                    await asyncio.sleep(self._poll_interval)
                    continue
                raise UpstreamError(status, code)

            access_token = str(data.get("accessToken"))
            account: dict = {}
            try:
                aresp = await self._http.get(
                    self._base + ACCOUNT_PATH,
                    params={"state": state},
                    headers=common_headers("cn", token=access_token),
                    allow_redirects=False,
                )
                try:
                    abody = await aresp.json(content_type=None)
                finally:
                    aresp.release()
                _, account, _ = envelope(abody)
            except Exception:  # noqa: BLE001 - 账号信息只是锦上添花
                account = {}

            domain = normalize_domain(data.get("domain")) or "www.codebuddy.cn"
            realm = known_region_of(domain) or "cn"
            uid = str(account.get("uid") or "")
            nickname = str(account.get("nickname") or "")
            uin = str(account.get("uin") or "")
            expires_in = data.get("expiresIn")
            expiry = 0
            if isinstance(expires_in, (int, float)):
                expiry = int(time.time() * 1000) + int(expires_in) * 1000

            return {
                "id": account_id_for(uin, uid, nickname or access_token[-8:]),
                "uid": uid,
                "uin": uin,
                "nickname": nickname,
                "domain": domain,
                "realm": realm,
                "access_token": access_token,
                "refresh_token": str(data.get("refreshToken") or ""),
                "expires_at": expiry,
                "refresh_expires_at": 0,
                "enterprise_id": str(account.get("enterpriseId") or ""),
            }
        finally:
            if epoch == self._epoch and self._pending_state == state:
                self._pending_state = None

    def cancel(self) -> None:
        self._epoch += 1
        self._pending_state = None
