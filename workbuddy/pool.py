"""WorkBuddy 账号池：凭据保存、token 刷新、按账号发请求与失败换号。"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from .core.chat import ChatService, aggregate_chunks
from .core.errors import ChatStreamError, PoolEmptyError, UpstreamError
from .core.http import (
    CHAT_PATH,
    CLIENT_VERSION,
    REFRESH_PATH,
    chat_base_for,
    common_headers,
    envelope,
    parse_retry_after,
    refresh_headers,
    stable_account_id,
)
from .core.limits import ConcurrencyGate, LimitExceeded
from .core.prepare import prepare_payload
from .core.storage import EncryptedJsonStore

log = logging.getLogger("_maibot_plugin_local_workbuddy_pool.pool")

ACCOUNTS_FILENAME = "accounts.enc"
# 排队类业务码：队列跟随模型/平台，换号拿到的还是同一位次。
QUEUE_CODES = (6020, 6021, 6022)
CHANNEL_GATE_CODE = 11128
#: 工具定义被上游拒收：11129（DeepSeek）/ 11133（GLM、Kimi、MiMo）。
TOOL_SCHEMA_CODES = frozenset({11129, 11133})


@dataclass
class Account:
    """账号池里的一个 WorkBuddy 账号。"""

    id: str
    uid: str = ""
    uin: str = ""
    nickname: str = ""
    domain: str = ""
    realm: str = "cn"
    access_token: str = ""
    refresh_token: str = ""
    expires_at: int = 0
    refresh_expires_at: int = 0
    enterprise_id: str = ""
    disabled: bool = False
    auth_failed: bool = False

    @property
    def label(self) -> str:
        return self.nickname or self.uin or self.uid or self.id

    @property
    def masked_uid(self) -> str:
        raw = self.uin or self.uid
        if len(raw) <= 4:
            return raw or "—"
        return f"{raw[:4]}…{raw[-3:]}"

    def to_doc(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "uid": self.uid,
            "uin": self.uin,
            "nickname": self.nickname,
            "domain": self.domain,
            "realm": self.realm,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "refresh_expires_at": self.refresh_expires_at,
            "enterprise_id": self.enterprise_id,
            "disabled": self.disabled,
            "auth_failed": self.auth_failed,
        }

    @classmethod
    def from_doc(cls, doc: dict) -> "Account | None":
        if not isinstance(doc, dict):
            return None
        account_id = str(doc.get("id") or "").strip()
        if not account_id:
            return None
        return cls(
            id=account_id,
            uid=str(doc.get("uid") or ""),
            uin=str(doc.get("uin") or ""),
            nickname=str(doc.get("nickname") or ""),
            domain=str(doc.get("domain") or ""),
            realm="global" if doc.get("realm") == "global" else "cn",
            access_token=str(doc.get("access_token") or ""),
            refresh_token=str(doc.get("refresh_token") or ""),
            expires_at=int(doc.get("expires_at") or 0),
            refresh_expires_at=int(doc.get("refresh_expires_at") or 0),
            enterprise_id=str(doc.get("enterprise_id") or ""),
            disabled=bool(doc.get("disabled")),
            auth_failed=bool(doc.get("auth_failed")),
        )


class AccountPool:
    """账号池的持久化与运行时状态。"""

    def __init__(
        self,
        data_dir: str | Path,
        master_key: str,
        *,
        cn_chat_base: str,
        global_chat_base: str,
        cooldown_seconds: int = 120,
        request_timeout: int = 180,
        stable_conversation_id: bool = False,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._store = EncryptedJsonStore(self.data_dir / ACCOUNTS_FILENAME, master_key)
        self._cn_chat_base = cn_chat_base
        self._global_chat_base = global_chat_base
        self._cooldown_seconds = max(10, int(cooldown_seconds))
        self._request_timeout = max(30, int(request_timeout))
        self._stable_conversation_id = bool(stable_conversation_id)
        self._accounts: list[Account] = []
        self._active_id = ""
        self._cooldown: dict[str, float] = {}
        self._last_used: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._http: aiohttp.ClientSession | None = None
        # 由插件在构建运行时注入；缺席时退回旧的 uid 派生值。
        self.device_identity = None
        self._load()

    # ------------------------------------------------------------------ 生命周期
    @property
    def http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=None, sock_connect=30, sock_read=self._request_timeout
                )
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None

    # ------------------------------------------------------------------ 持久化
    def _load(self) -> None:
        try:
            doc = self._store.load()
        except ValueError as exc:
            log.error("读取账号池失败：%s", exc)
            doc = None
        if not doc:
            return
        for item in doc.get("accounts") or []:
            account = Account.from_doc(item)
            if account is not None:
                self._accounts.append(account)
        active = str(doc.get("active") or "")
        if active and self.find_by_id(active) is not None:
            self._active_id = active
        elif self._accounts:
            self._active_id = self._accounts[0].id
        log.info("账号池已加载：%d 个账号，当前 active=%s", len(self._accounts), self._active_id[:6])

    def save(self) -> None:
        doc = {
            "version": 1,
            "active": self._active_id,
            "accounts": [account.to_doc() for account in self._accounts],
        }
        self._store.save(doc)

    # ------------------------------------------------------------------ 查询
    @property
    def accounts(self) -> list[Account]:
        return list(self._accounts)

    @property
    def active_id(self) -> str:
        return self._active_id

    @property
    def cooldown_seconds(self) -> int:
        return self._cooldown_seconds

    def find_by_id(self, account_id: str) -> Account | None:
        for account in self._accounts:
            if account.id == account_id:
                return account
        return None

    def find(self, ref: str) -> Account | None:
        """按序号（1 起）、id、id 前缀、uid/uin、昵称查找账号。"""
        ref = (ref or "").strip()
        if not ref:
            return None
        if ref.isdigit():
            index = int(ref)
            if 1 <= index <= len(self._accounts):
                return self._accounts[index - 1]
        for account in self._accounts:
            if ref in (account.id, account.uid, account.uin, account.nickname):
                return account
        for account in self._accounts:
            if len(ref) >= 4 and account.id.startswith(ref):
                return account
        return None

    def active_account(self) -> Account | None:
        return self.find_by_id(self._active_id) if self._active_id else None

    def candidates(self) -> list[Account]:
        """可尝试的账号：当前账号优先，其余按最久未用排序。"""
        usable = [a for a in self._accounts if not a.disabled and not a.auth_failed]
        if not usable:
            return []
        now = time.time()
        ready = [a for a in usable if self._cooldown.get(a.id, 0.0) <= now]
        ordered = ready or usable
        return sorted(
            ordered,
            key=lambda a: (0 if a.id == self._active_id else 1, self._last_used.get(a.id, 0.0)),
        )

    # ------------------------------------------------------------------ 变更
    def add_payloads(self, payloads: list[dict]) -> tuple[int, int]:
        """合并导入结果，返回 ``(新增, 更新)``。"""
        added = 0
        updated = 0
        by_id = {account.id: account for account in self._accounts}
        for payload in payloads:
            account_id = payload.get("id")
            if not account_id:
                continue
            existing = by_id.get(account_id)
            if existing is None:
                account = Account(
                    id=account_id,
                    uid=payload.get("uid", ""),
                    uin=payload.get("uin", ""),
                    nickname=payload.get("nickname", ""),
                    domain=payload.get("domain", ""),
                    realm=payload.get("realm", "cn"),
                    access_token=payload.get("access_token", ""),
                    refresh_token=payload.get("refresh_token", ""),
                    expires_at=int(payload.get("expires_at") or 0),
                    refresh_expires_at=int(payload.get("refresh_expires_at") or 0),
                    enterprise_id=payload.get("enterprise_id", ""),
                )
                self._accounts.append(account)
                by_id[account_id] = account
                added += 1
            else:
                existing.uid = payload.get("uid") or existing.uid
                existing.uin = payload.get("uin") or existing.uin
                existing.nickname = payload.get("nickname") or existing.nickname
                existing.domain = payload.get("domain") or existing.domain
                existing.realm = payload.get("realm") or existing.realm
                existing.access_token = payload.get("access_token") or existing.access_token
                existing.refresh_token = payload.get("refresh_token") or existing.refresh_token
                existing.expires_at = int(payload.get("expires_at") or existing.expires_at)
                existing.refresh_expires_at = int(
                    payload.get("refresh_expires_at") or existing.refresh_expires_at
                )
                existing.enterprise_id = payload.get("enterprise_id") or existing.enterprise_id
                existing.auth_failed = False
                existing.disabled = False
                self._cooldown.pop(existing.id, None)
                updated += 1
        if not self._active_id and self._accounts:
            self._active_id = self._accounts[0].id
        self.save()
        return added, updated

    def set_active(self, account: Account) -> None:
        self._active_id = account.id
        self.save()

    def remove(self, account: Account) -> None:
        self._accounts = [a for a in self._accounts if a.id != account.id]
        self._cooldown.pop(account.id, None)
        self._last_used.pop(account.id, None)
        if self._active_id == account.id:
            self._active_id = self._accounts[0].id if self._accounts else ""
        self.save()

    # ------------------------------------------------------------------ 运行状态
    def mark_success(self, account: Account) -> None:
        self._last_used[account.id] = time.time()
        self._cooldown.pop(account.id, None)
        if account.auth_failed:
            account.auth_failed = False
            self.save()

    def mark_rate_limited(self, account: Account, retry_after: float | None) -> None:
        seconds = max(float(retry_after or 0), float(self._cooldown_seconds))
        self._cooldown[account.id] = time.time() + seconds

    def mark_error(self, account: Account, seconds: float | None = None) -> None:
        self._cooldown[account.id] = time.time() + float(seconds or self._cooldown_seconds)

    def mark_auth_failed(self, account: Account) -> None:
        account.auth_failed = True
        self.save()

    def cooldown_left(self, account: Account) -> int:
        return max(0, int(self._cooldown.get(account.id, 0.0) - time.time()))

    # ------------------------------------------------------------------ 网络
    def chat_base(self, account: Account) -> str:
        return chat_base_for(
            account.realm, account.domain, self._cn_chat_base, self._global_chat_base
        )

    async def refresh(self, account: Account, stale_token: str | None = None) -> Account:
        """单飞刷新：同一个旧 token 最多刷新一次。"""
        lock = self._locks.setdefault(account.id, asyncio.Lock())
        async with lock:
            if stale_token is not None and account.access_token != stale_token:
                return account
            if not account.refresh_token:
                raise UpstreamError(401, None)
            url = self.chat_base(account) + REFRESH_PATH
            resp = await self.http.post(
                url,
                json={},
                headers=refresh_headers(account.realm, account.refresh_token),
                allow_redirects=False,
            )
            try:
                data = await _read_envelope(resp)
            finally:
                resp.release()
            new_access = data.get("accessToken")
            if not new_access:
                raise UpstreamError(resp.status, None)
            account.access_token = str(new_access)
            if data.get("refreshToken"):
                account.refresh_token = str(data["refreshToken"])
            expires_in = data.get("expiresIn")
            if isinstance(expires_in, (int, float)):
                account.expires_at = int(time.time() * 1000) + int(expires_in) * 1000
            account.auth_failed = False
            self.save()
            log.info("WorkBuddy 账号 %s 的 token 已刷新", account.label)
            return account


class _AccountClient:
    """以某个账号的身份向上游发聊天请求；401 时刷新并重发一次。"""

    def __init__(self, pool: AccountPool, account: Account) -> None:
        self._pool = pool
        self._account = account

    @property
    def credentials(self) -> dict:
        return {"accessToken": self._account.access_token}

    def _headers(self, token: str | None) -> dict[str, str]:
        account = self._account
        headers = common_headers(account.realm, token=token)
        headers["Accept"] = "application/json, text/event-stream"
        if token:
            headers["Authorization"] = "Bearer " + token
        else:
            headers.pop("Authorization", None)
            headers["X-No-Authorization"] = "1"

        if account.uid:
            headers["X-User-Id"] = account.uid
            identity = getattr(self._pool, "device_identity", None)
            if identity is not None:
                headers.update(identity.headers_for(account))
            else:
                headers["X-Machine-ID"] = stable_account_id(account.uid, "machine")
                headers["X-Session-ID"] = stable_account_id(account.uid, "session")
        else:
            headers["X-No-User-Id"] = "1"

        if account.realm == "global":
            headers["X-No-Enterprise-Id"] = "1"
            headers["X-Domain"] = account.domain or "www.workbuddy.ai"
        else:
            if account.enterprise_id:
                headers["X-Enterprise-Id"] = account.enterprise_id
            else:
                headers["X-No-Enterprise-Id"] = "1"
            if account.domain:
                headers["X-Domain"] = account.domain
            else:
                headers["X-No-Department-Info"] = "1"

        headers["X-Agent-Purpose"] = "conversation"
        headers["X-IDE-Name"] = "WorkBuddy"
        headers["X-IDE-Type"] = "WorkBuddy"
        headers["X-IDE-Version"] = CLIENT_VERSION
        headers["X-Product"] = "WorkBuddy"

        request_id = secrets.token_hex(16)
        message_id = secrets.token_hex(16)
        # 默认每次请求一个全新会话 id；开启 stable_conversation_id 时固定为
        # 该账号的派生值，让上游前缀缓存能跨请求命中（A/B 实验开关）。
        if self._pool._stable_conversation_id and self._account.uid:
            headers["X-Conversation-ID"] = stable_account_id(self._account.uid, "conversation")
        else:
            headers["X-Conversation-ID"] = secrets.token_hex(16)
        headers["X-Conversation-Request-ID"] = request_id
        headers["X-Conversation-Message-ID"] = message_id
        headers["X-Request-ID"] = message_id
        headers["X-Root-Request-ID"] = request_id
        headers["X-Trace-ID"] = request_id
        headers["X-B3-TraceId"] = request_id
        headers["X-B3-SpanId"] = message_id[:16]
        headers["X-B3-Sampled"] = "1"
        return headers

    @asynccontextmanager
    async def chat_stream(self, payload=None, *, base=None, headers=None, before_post=None, observe_errors=True):
        url = (base or self._pool.chat_base(self._account)) + CHAT_PATH
        token = self._account.access_token
        if before_post is not None:
            await before_post()
        resp = await self._pool.http.post(
            url,
            json=payload,
            headers=self._headers(token),
            allow_redirects=False,
        )
        try:
            if resp.status == 401:
                await resp.read()
                resp.release()
                try:
                    await self._pool.refresh(self._account, token)
                except UpstreamError as exc:
                    # 5xx 是上游临时故障，交给换号/冷却；其余视为刷新凭据失效。
                    if exc.status is not None and exc.status >= 500:
                        raise
                    raise UpstreamError(401, None) from None
                token = self._account.access_token
                if before_post is not None:
                    await before_post()
                resp = await self._pool.http.post(
                    url,
                    json=payload,
                    headers=self._headers(token),
                    allow_redirects=False,
                )
            yield resp
        finally:
            resp.release()


class WorkBuddyService:
    """对上层暴露一个 ``complete(payload)``：自动选号、失败换号。"""

    def __init__(
        self,
        pool: AccountPool,
        *,
        max_attempts: int = 3,
        concurrency: int = 1,
        rpm: int = 60,
        queue_timeout: float = 30.0,
    ) -> None:
        self._pool = pool
        self._max_attempts = max(1, int(max_attempts))
        self.gate = ConcurrencyGate(
            concurrency=max(1, int(concurrency)),
            queue_size=32,
            queue_timeout=queue_timeout,
            rpm=int(rpm) if rpm else None,
        )

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        accounts = self._pool.candidates()
        if not accounts:
            if self._pool.accounts:
                raise PoolEmptyError(
                    "WorkBuddy 账号池里没有可用账号：请用 /wb 列表 查看，必要时 /wb 登录 或重新导入"
                )
            raise PoolEmptyError(
                "WorkBuddy 账号池没有可用账号：请先放置导出文件并执行 /wb 导入"
            )

        last_error: Exception | None = None
        for attempt, account in enumerate(accounts, start=1):
            if attempt > self._max_attempts:
                break
            try:
                data = await self._complete_with(account, payload)
                self._pool.mark_success(account)
                return data
            except LimitExceeded:
                raise
            except (UpstreamError, ChatStreamError) as exc:
                last_error = exc
                log.warning(
                    "WorkBuddy 账号 %s 请求失败（第 %d 次）：%s",
                    account.label,
                    attempt,
                    exc,
                )
                if not self._record_failure(account, exc):
                    raise
        if last_error is not None:
            raise last_error
        raise PoolEmptyError("WorkBuddy 没有可尝试的账号")

    def _record_failure(self, account: Account, exc: Exception) -> bool:
        """记录失败并返回是否换下一个账号。"""
        if isinstance(exc, ChatStreamError):
            self._pool.mark_error(account)
            return True
        if isinstance(exc, UpstreamError):
            status = exc.status
            if status == 401:
                self._pool.mark_auth_failed(account)
                return True
            if status == 403:
                self._pool.mark_error(account, self._pool.cooldown_seconds * 5)
                return True
            if status == 429:
                self._pool.mark_rate_limited(account, exc.retry_after)
                return True
            if status is not None and status >= 500:
                self._pool.mark_error(account)
                return True
            if status == 200 and exc.code in QUEUE_CODES:
                # 排队跟随模型，换号也一样，直接上报。
                return False
            if status == 200 and exc.code in TOOL_SCHEMA_CODES:
                log.error(
                    "WorkBuddy 命中工具 schema 拒收（%s）：插件已自动修复 tuple items / "
                    "布尔 exclusive 边界 / 字符串或重复 required / 外部 $ref / 错误类型；"
                    "仍然失败说明还有未覆盖的写法，请把该工具定义反馈给插件作者。",
                    exc.code,
                )
                return False
            # 400 这类请求本身的问题，换号也不会成功。
            return False
        return True

    async def _complete_with(self, account: Account, payload: dict[str, Any]) -> dict[str, Any]:
        async with self.gate.slot(count_rpm=False):
            try:
                return await self._run_once(account, payload)
            except UpstreamError as exc:
                if exc.code == CHANNEL_GATE_CODE:
                    log.warning("WorkBuddy 命中渠道指纹（11128），按中性系统提示重试一次")
                    return await self._run_once(
                        account, prepare_payload(payload, force_neutral=True)
                    )
                raise

    async def _run_once(self, account: Account, payload: dict[str, Any]) -> dict[str, Any]:
        body = prepare_payload(payload)
        client = _AccountClient(self._pool, account)
        chat = ChatService(client, limits=self.gate)
        chunks = [chunk async for chunk in chat.stream(body)]
        return aggregate_chunks(chunks, body)


async def _read_envelope(resp) -> dict:
    try:
        body = await resp.json(content_type=None)
    except Exception:
        await resp.read()
        raise UpstreamError(resp.status, None) from None
    code, data, _ = envelope(body)
    if resp.status != 200:
        raise UpstreamError(resp.status, code, parse_retry_after(resp))
    if code != 0:
        raise UpstreamError(200, code)
    return data
