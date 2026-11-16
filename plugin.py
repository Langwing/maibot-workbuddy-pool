"""WorkBuddy 账号池接入 —— MaiBot LLM Provider 插件。

参考 dsh-connect-workbuddy 的导出格式与网关约定、MaiBot-WorkBuddy 的 Provider
实现：

* 支持从 ``/wb 导入`` 读取 dsh-connect-workbuddy 导出的账号文件（多账号）；
* token 过期自动刷新，401/429/5xx 时按配置换号重试；
* 通过 ``@LLMProvider`` 注册 ``local.workbuddy-pool``，在
  ``model_config.toml`` 的 ``api_providers[].client_type`` 指向它即可。

凭据保存在 ``<插件数据目录>/accounts.enc``（AES-256-GCM）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any

from maibot_sdk import API, Command, Field, LLMProvider, MaiBotPlugin, PluginConfigBase

try:  # 作为包加载时走相对导入
    from .workbuddy import billing
    from .workbuddy.catalog import CatalogService, fallback_models
    from .workbuddy.core.errors import WorkBuddyError
    from .workbuddy.core.storage import load_or_create_master_key
    from .workbuddy.core.transfer import parse_transfer_text, record_to_account
    from .workbuddy.identity import DeviceIdentity
    from .workbuddy.models_server import ModelsServer
    from .workbuddy.oauth import LoginFlow
    from .workbuddy.pool import AccountPool, WorkBuddyService
    from .workbuddy.prefs import REASONING_LEVELS, ModelPrefs
    from .workbuddy.provider import WorkBuddyProvider
    from .workbuddy.sync import apply as sync_apply
    from .workbuddy.sync import plan as sync_plan
    from .workbuddy.sync import resolve_model_config_path
    from .workbuddy.sync import visual_ids_in_config
except ImportError:  # 兜底：插件目录在 sys.path 上时的绝对导入
    from workbuddy import billing  # type: ignore
    from workbuddy.catalog import CatalogService, fallback_models  # type: ignore
    from workbuddy.core.errors import WorkBuddyError  # type: ignore
    from workbuddy.core.storage import load_or_create_master_key  # type: ignore
    from workbuddy.core.transfer import parse_transfer_text, record_to_account  # type: ignore
    from workbuddy.identity import DeviceIdentity  # type: ignore
    from workbuddy.models_server import ModelsServer  # type: ignore
    from workbuddy.oauth import LoginFlow  # type: ignore
    from workbuddy.pool import AccountPool, WorkBuddyService  # type: ignore
    from workbuddy.prefs import REASONING_LEVELS, ModelPrefs  # type: ignore
    from workbuddy.provider import WorkBuddyProvider  # type: ignore
    from workbuddy.sync import apply as sync_apply  # type: ignore
    from workbuddy.sync import plan as sync_plan  # type: ignore
    from workbuddy.sync import resolve_model_config_path  # type: ignore
    from workbuddy.sync import visual_ids_in_config  # type: ignore

CLIENT_TYPE = "local.workbuddy-pool"
IMPORT_FILENAME = "accounts-import.json"

log = logging.getLogger("_maibot_plugin_local_workbuddy_pool")

_LOGIN_TASKS: set[asyncio.Task] = set()


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "bot"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用 WorkBuddy Provider")
    config_version: str = Field(default="1.0.0", description="配置版本")


class PoolSectionConfig(PluginConfigBase):
    __ui_label__ = "账号池"
    __ui_icon__ = "users"
    __ui_order__ = 1

    default_model: str = Field(
        default="deepseek-v4.1-flash",
        description="请求未指定模型时使用的 WorkBuddy 模型 ID",
    )
    max_attempts: int = Field(
        default=3, ge=1, le=8, description="一次调用最多尝试几个账号（含当前账号）"
    )
    cooldown_seconds: int = Field(
        default=120, ge=10, le=3600, description="账号失败后的冷却秒数（429 优先用 Retry-After）"
    )
    concurrency: int = Field(default=1, ge=1, le=4, description="同时在途的聊天请求数上限")
    rpm: int = Field(
        default=60,
        ge=0,
        le=600,
        description="每分钟请求上限；超出会排队等待窗口释放，0 表示不限制",
    )
    import_path: str = Field(
        default="",
        description="默认导入文件路径；留空则读数据目录下的 accounts-import.json",
    )
    stable_conversation_id: bool = Field(
        default=False,
        description="实验开关：把 X-Conversation-ID 固定为该账号的派生值，便于上游前缀缓存命中；默认关闭",
    )


class NetworkSectionConfig(PluginConfigBase):
    __ui_label__ = "网络"
    __ui_icon__ = "globe"
    __ui_order__ = 2

    cn_chat_base: str = Field(
        default="https://copilot.tencent.com",
        description="国内版聊天网关；被网络拦截时可改成 https://www.codebuddy.cn",
    )
    global_chat_base: str = Field(
        default="https://www.workbuddy.ai", description="国际版聊天网关"
    )
    request_timeout: int = Field(
        default=180, ge=30, le=600, description="单次上游读取超时（秒）"
    )


class CatalogSectionConfig(PluginConfigBase):
    __ui_label__ = "模型目录"
    __ui_icon__ = "layers"
    __ui_order__ = 3

    enabled: bool = Field(default=True, description="是否抓取并缓存 WorkBuddy 模型目录")
    ttl_seconds: int = Field(
        default=900, ge=60, le=86400, description="目录缓存有效期（秒），过期后按需刷新"
    )
    refresh_on_start: bool = Field(default=True, description="插件加载后自动刷新一次目录")


class ModelsServerSectionConfig(PluginConfigBase):
    __ui_label__ = "模型列表服务"
    __ui_icon__ = "server"
    __ui_order__ = 4

    auto_start: bool = Field(default=True, description="是否启动本地 /v1/models 服务")
    host: str = Field(default="127.0.0.1", description="监听地址，只建议 127.0.0.1")
    port: int = Field(default=8765, ge=1024, le=65535, description="监听端口")


class SyncSectionConfig(PluginConfigBase):
    __ui_label__ = "模型同步"
    __ui_icon__ = "file-down"
    __ui_order__ = 5

    provider_name: str = Field(default="WorkBuddy", description="model_config.toml 里的提供商名称")
    model_config_path: str = Field(
        default="",
        description="model_config.toml 路径；留空则从数据目录向上推断（通常是 /MaiBot/config/model_config.toml）",
    )


class WorkBuddyPoolConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    pool: PoolSectionConfig = Field(default_factory=PoolSectionConfig)
    network: NetworkSectionConfig = Field(default_factory=NetworkSectionConfig)
    catalog: CatalogSectionConfig = Field(default_factory=CatalogSectionConfig)
    models_server: ModelsServerSectionConfig = Field(default_factory=ModelsServerSectionConfig)
    sync: SyncSectionConfig = Field(default_factory=SyncSectionConfig)


# --------------------------------------------------------------------------
# 插件
# --------------------------------------------------------------------------
class WorkBuddyPoolPlugin(MaiBotPlugin):
    config_model = WorkBuddyPoolConfig

    def __init__(self) -> None:
        super().__init__()
        self._pool: AccountPool | None = None
        self._service: WorkBuddyService | None = None
        self._provider: WorkBuddyProvider | None = None
        self._login_flow: LoginFlow | None = None
        self._catalog: CatalogService | None = None
        self._models_server: ModelsServer | None = None
        self._identity: DeviceIdentity | None = None
        self._prefs: ModelPrefs | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._test_jobs: dict[str, dict[str, Any]] = {}
        self._credits_cache: dict[str, dict[str, Any]] = {}
        self._credits_updated_at: float = 0.0
        self._webui_login: dict[str, str] = {
            "status": "idle",
            "url": "",
            "label": "",
            "error": "",
        }

    # ------------------------------------------------------------ 生命周期
    async def on_load(self) -> None:
        self._build_runtime()
        accounts = self._pool.accounts if self._pool else []
        log.info(
            "WorkBuddy 账号池插件已加载：client_type=%s 账号数=%d",
            CLIENT_TYPE,
            len(accounts),
        )
        if not accounts:
            log.warning(
                "WorkBuddy 账号池为空：把 dsh-connect-workbuddy 导出的 JSON 放到 %s 后执行 /wb 导入",
                self._data_dir() / IMPORT_FILENAME,
            )
        await self._start_services()

    async def on_unload(self) -> None:
        await self._stop_services()
        for task in list(_LOGIN_TASKS):
            task.cancel()
        _LOGIN_TASKS.clear()
        if self._login_flow is not None:
            self._login_flow.cancel()
            self._login_flow = None
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        self._provider = None

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        log.info("WorkBuddy 配置已更新：scope=%s version=%s", scope, version)
        await self._stop_services()
        if self._pool is not None:
            await self._pool.close()
        self._build_runtime()
        await self._start_services()

    async def _start_services(self) -> None:
        cfg = self.config
        if self._models_server is not None and cfg.models_server.auto_start:
            await self._models_server.start()
        if self._catalog is not None and cfg.catalog.refresh_on_start and self._pool is not None:
            task = asyncio.create_task(self._refresh_catalog_safe())
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    async def _stop_services(self) -> None:
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()
        if self._models_server is not None:
            await self._models_server.stop()
            self._models_server = None
        self._catalog = None

    async def _refresh_catalog_safe(self) -> None:
        if self._catalog is None:
            return
        try:
            await self._catalog.refresh()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("WorkBuddy 模型目录自动刷新失败：%s", exc)

    # ------------------------------------------------------------ 运行时
    def _data_dir(self) -> Path:
        return Path(self.ctx.paths.data_dir)

    def _build_runtime(self) -> None:
        cfg = self.config
        data_dir = self._data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            master_key = load_or_create_master_key(data_dir)
        except ValueError as exc:
            log.error("WorkBuddy 主密钥无效：%s", exc)
            self._pool = None
            self._provider = None
            return
        self._pool = AccountPool(
            data_dir,
            master_key,
            cn_chat_base=cfg.network.cn_chat_base,
            global_chat_base=cfg.network.global_chat_base,
            cooldown_seconds=cfg.pool.cooldown_seconds,
            request_timeout=cfg.network.request_timeout,
            stable_conversation_id=cfg.pool.stable_conversation_id,
        )
        self._identity = DeviceIdentity(data_dir)
        self._pool.device_identity = self._identity
        self._prefs = ModelPrefs(data_dir)
        if self._prefs.needs_seed():
            seeded = visual_ids_in_config(self._sync_path(), cfg.sync.provider_name)
            self._prefs.set(visual=seeded)
            log.info("已按当前 model_config 初始化图片开关：%d 个模型", len(seeded))
        service = WorkBuddyService(
            self._pool,
            max_attempts=cfg.pool.max_attempts,
            concurrency=cfg.pool.concurrency,
            rpm=cfg.pool.rpm,
        )
        self._provider = WorkBuddyProvider(service, cfg.pool.default_model, prefs=self._prefs)
        self._service = service
        self._login_flow = LoginFlow(self._pool.http, base=cfg.network.cn_chat_base)
        if cfg.catalog.enabled:
            self._catalog = CatalogService(
                self._pool, data_dir, ttl_seconds=cfg.catalog.ttl_seconds
            )
            self._models_server = ModelsServer(
                self._catalog,
                host=cfg.models_server.host,
                port=cfg.models_server.port,
            )
        else:
            self._catalog = None
            self._models_server = None
        self._auto_import()

    def _auto_import(self) -> None:
        """账号池为空时自动导入默认文件，方便首次部署。"""
        if self._pool is None or self._pool.accounts:
            return
        path = self._default_import_path()
        if path is None or not path.exists():
            return
        try:
            records = parse_transfer_text(path.read_text(encoding="utf-8"))
            payloads = []
            for record in records:
                payload, _ = record_to_account(record)
                if payload:
                    payloads.append(payload)
            added, updated = self._pool.add_payloads(payloads)
            log.info("WorkBuddy 自动导入 %s：新增 %d，更新 %d", path, added, updated)
        except Exception:  # noqa: BLE001
            log.exception("WorkBuddy 自动导入失败：%s", path)

    # ------------------------------------------------------------ Provider
    @LLMProvider(
        CLIENT_TYPE,
        name="WorkBuddy 账号池",
        description="通过 WorkBuddy 账号池转发的 OpenAI 兼容模型通道",
    )
    async def handle_llm(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        if not self.config.plugin.enabled:
            raise ValueError("WorkBuddy 插件已停用")
        if self._provider is None:
            raise ValueError("WorkBuddy 账号池未初始化，请检查插件日志")
        return await self._provider.dispatch(operation, request)

    # ------------------------------------------------------------ 命令
    @Command(
        "workbuddy",
        description="WorkBuddy 账号池管理（导入/列表/切换/状态/登录/删除）",
        pattern=r"^/wb(?:\s+(?P<wb_args>[\s\S]*))?$",
        permission="operator",
        intercept_message_level=1,
    )
    async def handle_wb(self, **kwargs: Any) -> str:
        stream_id = str(kwargs.get("stream_id") or "")
        args = self._command_args(kwargs)
        if not args:
            return await self._reply(stream_id, self._help_text())
        name, _, rest = args.partition(" ")
        name = name.strip().lower()
        rest = rest.strip()

        handlers = {
            "help": lambda: self._reply(stream_id, self._help_text()),
            "帮助": lambda: self._reply(stream_id, self._help_text()),
            "list": lambda: self._reply(stream_id, self._list_text()),
            "列表": lambda: self._reply(stream_id, self._list_text()),
            "status": lambda: self._reply(stream_id, self._status_text()),
            "状态": lambda: self._reply(stream_id, self._status_text()),
            "import": lambda: self._import(stream_id, rest),
            "导入": lambda: self._import(stream_id, rest),
            "use": lambda: self._use(stream_id, rest),
            "切换": lambda: self._use(stream_id, rest),
            "remove": lambda: self._remove(stream_id, rest),
            "删除": lambda: self._remove(stream_id, rest),
            "login": lambda: self._login(stream_id),
            "登录": lambda: self._login(stream_id),
        }
        handler = handlers.get(name)
        if handler is None:
            return await self._reply(stream_id, "未知子命令。\n\n" + self._help_text())
        return await handler()

    def _command_args(self, kwargs: dict) -> str:
        matched = kwargs.get("matched_groups")
        if isinstance(matched, dict):
            raw = matched.get("wb_args")
            if isinstance(raw, str):
                return raw.strip()
        text = str(kwargs.get("text") or "")
        return re.sub(r"^/wb\b", "", text, flags=re.IGNORECASE).strip()

    async def _reply(self, stream_id: str, text: str) -> str:
        if stream_id:
            try:
                await self.ctx.send.text(text, stream_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("WorkBuddy 回复失败：%s", exc)
            return ""
        return text

    # ------------------------------------------------------------ 子命令实现
    def _help_text(self) -> str:
        return (
            "WorkBuddy 账号池\n"
            "/wb 列表 — 查看账号\n"
            "/wb 状态 — 查看运行配置\n"
            "/wb 导入 [路径] — 导入 dsh-connect-workbuddy 导出的 JSON\n"
            "/wb 切换 <序号|id|昵称> — 指定当前账号\n"
            "/wb 登录 — 浏览器 OAuth 新增账号\n"
            "/wb 删除 <序号|id> — 移除账号"
        )

    def _list_text(self) -> str:
        if self._pool is None:
            return "账号池未初始化。"
        accounts = self._pool.accounts
        if not accounts:
            return (
                "账号池为空。把导出文件放到\n"
                f"{self._data_dir() / IMPORT_FILENAME}\n"
                "然后执行 /wb 导入"
            )
        lines = [f"WorkBuddy 账号池（{len(accounts)} 个）"]
        for index, account in enumerate(accounts, start=1):
            mark = "★" if account.id == self._pool.active_id else " "
            if account.disabled:
                state = "已停用"
            elif account.auth_failed:
                state = "需重新登录/导入"
            else:
                state = "正常"
            left = self._pool.cooldown_left(account)
            if left:
                state += f"，冷却 {left}s"
            lines.append(
                f"{mark}{index}. {account.label} | {account.realm.upper()} | "
                f"{account.masked_uid} | {state}"
            )
        return "\n".join(lines)

    def _status_text(self) -> str:
        cfg = self.config
        if self._pool is None:
            return "账号池未初始化，请检查主密钥配置。"
        active = self._pool.active_account()
        accounts = self._pool.accounts
        usable = len(self._pool.candidates())
        return (
            "WorkBuddy 状态\n"
            f"插件启用：{'是' if cfg.plugin.enabled else '否'}\n"
            f"账号：{len(accounts)} 个（当前可用 {usable}）\n"
            f"当前账号：{active.label if active else '—'}\n"
            f"默认模型：{cfg.pool.default_model}\n"
            f"失败换号：最多尝试 {cfg.pool.max_attempts} 个账号\n"
            f"并发 / RPM：{cfg.pool.concurrency} / {cfg.pool.rpm or '不限'}\n"
            f"国内网关：{cfg.network.cn_chat_base}"
        )

    def _default_import_path(self) -> Path | None:
        raw = (self.config.pool.import_path or "").strip()
        path = Path(raw) if raw else self._data_dir() / IMPORT_FILENAME
        if not path.is_absolute():
            path = self._data_dir() / path
        return path

    def _resolve_import_path(self, raw: str) -> Path | None:
        if not raw:
            return self._default_import_path()
        path = Path(raw.strip())
        if not path.is_absolute():
            path = self._data_dir() / path
        return path

    async def _import(self, stream_id: str, raw_path: str) -> str:
        if self._pool is None:
            return await self._reply(stream_id, "账号池未初始化，请检查插件日志。")
        path = self._resolve_import_path(raw_path)
        if path is None or not path.exists():
            return await self._reply(
                stream_id,
                "找不到导入文件。\n"
                f"默认路径：{self._data_dir() / IMPORT_FILENAME}\n"
                "用法：/wb 导入 [路径]（相对路径按插件数据目录解析）",
            )
        try:
            records = parse_transfer_text(path.read_text(encoding="utf-8"))
        except (OSError, WorkBuddyError) as exc:
            return await self._reply(stream_id, f"读取导入文件失败：{exc}")

        payloads: list[dict] = []
        skipped: list[str] = []
        for index, record in enumerate(records, start=1):
            payload, reason = record_to_account(record)
            if payload is None:
                name = record.get("nickname") or record.get("uid") or f"第{index}条"
                skipped.append(f"{name}：{reason}")
                continue
            payloads.append(payload)

        added, updated = self._pool.add_payloads(payloads)
        lines = [
            f"导入完成：文件共 {len(records)} 条，新增 {added}，更新 {updated}，跳过 {len(skipped)}"
        ]
        if skipped:
            lines.append("跳过原因：")
            lines.extend(f"- {item}" for item in skipped[:10])
            if len(skipped) > 10:
                lines.append(f"- …另有 {len(skipped) - 10} 条")
        lines.append("")
        lines.append(self._list_text())
        return await self._reply(stream_id, "\n".join(lines))

    async def _use(self, stream_id: str, raw: str) -> str:
        if self._pool is None:
            return await self._reply(stream_id, "账号池未初始化。")
        if not raw:
            return await self._reply(stream_id, "用法：/wb 切换 <序号|id|昵称>")
        account = self._pool.find(raw)
        if account is None:
            return await self._reply(stream_id, f"找不到账号：{raw}")
        self._pool.set_active(account)
        return await self._reply(stream_id, f"当前账号已切换为：{account.label}")

    async def _remove(self, stream_id: str, raw: str) -> str:
        if self._pool is None:
            return await self._reply(stream_id, "账号池未初始化。")
        if not raw:
            return await self._reply(stream_id, "用法：/wb 删除 <序号|id>")
        account = self._pool.find(raw)
        if account is None:
            return await self._reply(stream_id, f"找不到账号：{raw}")
        self._pool.remove(account)
        return await self._reply(stream_id, f"已移除账号：{account.label}")

    async def _login(self, stream_id: str) -> str:
        if self._pool is None or self._login_flow is None:
            return await self._reply(stream_id, "账号池未初始化，请检查插件日志。")
        try:
            begin = await self._login_flow.begin()
        except Exception as exc:  # noqa: BLE001
            return await self._reply(stream_id, f"获取授权链接失败：{exc}")
        state = str(begin.get("state") or "")
        url = str(begin.get("authUrl") or "")
        if not url:
            return await self._reply(stream_id, "上游没有返回授权链接，请稍后重试。")
        if stream_id:
            task = asyncio.create_task(self._finish_login(state, stream_id))
            _LOGIN_TASKS.add(task)
            task.add_done_callback(_LOGIN_TASKS.discard)
        return await self._reply(
            stream_id,
            "请在 10 分钟内在浏览器打开下面的链接完成 WorkBuddy 授权：\n" + url,
        )

    async def _finish_login(self, state: str, stream_id: str) -> None:
        flow = self._login_flow
        if flow is None or self._pool is None:
            return
        try:
            payload = await flow.wait(state, timeout=600.0)
            self._pool.add_payloads([payload])
            label = payload.get("nickname") or payload.get("uid") or "新账号"
            await self._reply(stream_id, f"WorkBuddy 登录成功，已加入账号池：{label}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("WorkBuddy 登录失败：%s", exc)
            await self._reply(stream_id, f"WorkBuddy 登录失败：{exc}")

    # ------------------------------------------------------------ WebUI 接口
    @API("wb_page_state", version="1")
    async def api_page_state(self) -> dict[str, Any]:
        """WebUI 查询：账号池状态、账号列表与运行配置。"""
        cfg = self.config
        accounts: list[dict[str, Any]] = []
        if self._pool is not None:
            now = time.time()
            for index, account in enumerate(self._pool.accounts, start=1):
                left = self._pool.cooldown_left(account)
                if account.disabled:
                    state, state_text = "disabled", "已停用"
                elif account.auth_failed:
                    state, state_text = "auth_failed", "需重新登录/导入"
                elif left:
                    state, state_text = "cooldown", f"冷却 {left}s"
                else:
                    state, state_text = "ok", "正常"
                accounts.append(
                    {
                        "ref": str(index),
                        "id": account.id,
                        "nickname": account.label,
                        "realm": account.realm,
                        "realm_text": "国内版" if account.realm == "cn" else "国际版",
                        "uid_masked": account.masked_uid,
                        "state": state,
                        "state_text": state_text,
                        "cooldown": left,
                        "expires_at": account.expires_at,
                        "active": account.id == self._pool.active_id,
                        "now": now,
                    }
                )

        active = self._pool.active_account() if self._pool else None
        usable = len(self._pool.candidates()) if self._pool else 0
        return {
            "enabled": bool(cfg.plugin.enabled),
            "ready": self._pool is not None,
            "accounts": accounts,
            "counts": {"total": len(accounts), "usable": usable},
            "active": active.label if active else "",
            "settings": {
                "default_model": cfg.pool.default_model,
                "max_attempts": cfg.pool.max_attempts,
                "cooldown_seconds": cfg.pool.cooldown_seconds,
                "concurrency": cfg.pool.concurrency,
                "rpm": cfg.pool.rpm,
                "cn_chat_base": cfg.network.cn_chat_base,
                "global_chat_base": cfg.network.global_chat_base,
                "import_path": str(self._default_import_path() or ""),
            },
            "login": dict(self._webui_login),
        }

    @API("wb_page_import", version="1")
    async def api_page_import(self, payload: str = "", path: str = "") -> dict[str, Any]:
        """WebUI 操作：从粘贴文本或文件路径导入账号。"""
        if self._pool is None:
            raise ValueError("账号池未初始化，请检查插件日志")
        payload = str(payload or "").strip()
        raw_path = str(path or "").strip()

        if payload:
            source = "粘贴内容"
            try:
                records = parse_transfer_text(payload)
            except WorkBuddyError as exc:
                return {"error": str(exc)}
        elif raw_path:
            target = self._resolve_import_path(raw_path)
            source = str(target)
            if target is None or not target.exists():
                return {"error": f"找不到导入文件：{raw_path}"}
            try:
                records = parse_transfer_text(target.read_text(encoding="utf-8"))
            except OSError as exc:
                return {"error": f"读取导入文件失败：{exc}"}
            except WorkBuddyError as exc:
                return {"error": str(exc)}
        else:
            return {"error": "请粘贴导出 JSON，或填写文件路径"}

        payloads: list[dict] = []
        skipped: list[str] = []
        for index, record in enumerate(records, start=1):
            item, reason = record_to_account(record)
            if item is None:
                name = record.get("nickname") or record.get("uid") or f"第{index}条"
                skipped.append(f"{name}：{reason}")
                continue
            payloads.append(item)
        added, updated = self._pool.add_payloads(payloads)
        log.info("WebUI 导入账号：来源=%s 新增=%d 更新=%d 跳过=%d", source, added, updated, len(skipped))
        return {
            "source": source,
            "total": len(records),
            "added": added,
            "updated": updated,
            "skipped": skipped[:20],
            "skipped_total": len(skipped),
        }

    @API("wb_page_switch", version="1")
    async def api_page_switch(self, ref: str) -> dict[str, Any]:
        """WebUI 操作：把指定账号设为当前账号。"""
        if self._pool is None:
            raise ValueError("账号池未初始化")
        account = self._pool.find(str(ref or ""))
        if account is None:
            return {"error": f"找不到账号：{ref}"}
        self._pool.set_active(account)
        return {"active": account.label}

    @API("wb_page_remove", version="1")
    async def api_page_remove(self, ref: str) -> dict[str, Any]:
        """WebUI 操作：从账号池移除账号。"""
        if self._pool is None:
            raise ValueError("账号池未初始化")
        account = self._pool.find(str(ref or ""))
        if account is None:
            return {"error": f"找不到账号：{ref}"}
        self._pool.remove(account)
        log.info("WebUI 移除 WorkBuddy 账号：%s", account.label)
        return {"removed": account.label}

    @API("wb_page_login_begin", version="1")
    async def api_page_login_begin(self) -> dict[str, Any]:
        """WebUI 操作：生成 WorkBuddy 授权链接，并把结果写入账号池。"""
        if self._pool is None:
            raise ValueError("账号池未初始化，请检查插件日志")
        if self._webui_login.get("status") == "pending" and self._webui_login.get("url"):
            return {"url": self._webui_login["url"], "reused": True}
        flow = LoginFlow(self._pool.http, base=self.config.network.cn_chat_base)
        try:
            begin = await flow.begin()
        except Exception as exc:  # noqa: BLE001
            self._webui_login = {"status": "error", "url": "", "label": "", "error": str(exc)}
            return {"error": f"获取授权链接失败：{exc}"}
        url = str(begin.get("authUrl") or "")
        state = str(begin.get("state") or "")
        self._webui_login = {"status": "pending", "url": url, "label": "", "error": ""}
        task = asyncio.create_task(self._finish_webui_login(flow, state))
        _LOGIN_TASKS.add(task)
        task.add_done_callback(_LOGIN_TASKS.discard)
        return {"url": url, "reused": False}

    @API("wb_page_login_status", version="1")
    async def api_page_login_status(self) -> dict[str, Any]:
        """WebUI 查询：扫码登录进度。"""
        return dict(self._webui_login)

    async def _finish_webui_login(self, flow: LoginFlow, state: str) -> None:
        try:
            payload = await flow.wait(state, timeout=600.0)
            if self._pool is None:
                raise ValueError("账号池已卸载")
            self._pool.add_payloads([payload])
            label = payload.get("nickname") or payload.get("uid") or "新账号"
            self._webui_login = {
                "status": "success",
                "url": "",
                "label": label,
                "error": "",
            }
            log.info("WebUI 扫码登录成功：%s", label)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("WebUI 扫码登录失败：%s", exc)
            self._webui_login = {
                "status": "error",
                "url": "",
                "label": "",
                "error": str(exc),
            }


    # ------------------------------------------------------------ 模型目录
    def _catalog_models(self) -> list[dict[str, Any]]:
        if self._catalog is None:
            return []
        models = self._catalog.models()
        return models or []

    def _model_rows(self) -> list[dict[str, Any]]:
        models = self._catalog_models()
        if not models:
            models = fallback_models("cn")
        if self._prefs is None:
            return []
        return self._prefs.state(models)

    def _sync_path(self) -> Path:
        return resolve_model_config_path(self.config.sync.model_config_path, self._data_dir())

    def _sync_visual_ids(self) -> set[str]:
        """图片开关完全以页面手动勾选为准。"""
        return self._prefs.visual_ids() if self._prefs is not None else set()

    def _sync_allow_remove(self) -> bool:
        """目录抓取有区域失败时目录可能不完整，本次不删模型。"""
        if self._catalog is None or not self._catalog_models():
            return False
        errors = self._catalog.snapshot().get("errors") or {}
        return not errors

    def _sync_plan_payload(self) -> dict[str, Any]:
        server = self._models_server.describe() if self._models_server else {}
        models = self._catalog_models()
        try:
            return sync_plan(
                self._sync_path(),
                self.config.sync.provider_name,
                models,
                visual_ids=self._sync_visual_ids(),
                server_base_url=str(server.get("base_url") or ""),
                allow_remove=self._sync_allow_remove(),
            )
        except FileNotFoundError as exc:
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            log.exception("生成模型同步计划失败")
            return {"error": f"生成同步计划失败：{exc}"}

    def _catalog_state_payload(self) -> dict[str, Any]:
        catalog = self._catalog
        return {
            "updated_at": catalog.updated_at if catalog else 0,
            "updated_text": (catalog.snapshot().get("updated_text") if catalog else "") or "",
            "stale": catalog.stale if catalog else True,
            "errors": (catalog.snapshot().get("errors") if catalog else {}) or {},
            "count": len(self._catalog_models()),
            "server": self._models_server.describe() if self._models_server else {"running": False},
        }

    @API("wb_models_state", version="1")
    async def api_models_state(self) -> dict[str, Any]:
        """WebUI 查询：模型目录、每模型开关、本地服务与同步计划。"""
        return {
            "catalog": self._catalog_state_payload(),
            "models": self._model_rows(),
            "sync": self._sync_plan_payload(),
            "reasoning_levels": list(REASONING_LEVELS),
        }

    @API("wb_catalog_refresh", version="1")
    async def api_catalog_refresh(self) -> dict[str, Any]:
        """WebUI 操作：立即刷新模型目录（超时则转入后台继续）。"""
        if self._catalog is None:
            return {"error": "模型目录功能未启用"}
        if not self._pool or not self._pool.accounts:
            return {"error": "账号池为空，无法读取目录"}
        task = asyncio.create_task(self._catalog.refresh())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=8.0)
        except asyncio.TimeoutError:
            return {"pending": True, "catalog": self._catalog_state_payload()}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"刷新失败：{exc}", "catalog": self._catalog_state_payload()}
        return {"pending": False, "catalog": self._catalog_state_payload()}

    # ------------------------------------------------------------ 积分 / 诊断
    @API("wb_credits_state", version="1")
    async def api_credits_state(self) -> dict[str, Any]:
        """WebUI 查询：最近一次读取到的积分（只读缓存）。"""
        return {"updated_at": self._credits_updated_at, "items": list(self._credits_cache.values())}

    @API("wb_fetch_credits", version="1")
    async def api_fetch_credits(self, ref: str = "") -> dict[str, Any]:
        """WebUI 操作：读取积分余额（只读，不签到、不领取）。"""
        if self._pool is None:
            return {"error": "账号池未初始化"}
        accounts = self._pool.accounts
        if ref:
            account = self._pool.find(str(ref))
            if account is None:
                return {"error": f"找不到账号：{ref}"}
            accounts = [account]
        else:
            accounts = accounts[:8]

        async def read_one(account) -> dict[str, Any]:
            try:
                data = await asyncio.wait_for(
                    billing.fetch_credits(self._pool, account, self._identity), timeout=8.0
                )
                return {"ok": True, "id": account.id, "nickname": account.label, **data}
            except asyncio.TimeoutError:
                return {"ok": False, "id": account.id, "nickname": account.label, "error": "读取超时"}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "id": account.id, "nickname": account.label, "error": str(exc)}

        results = await asyncio.gather(*(read_one(account) for account in accounts))
        for item in results:
            self._credits_cache[item["id"]] = item
        self._credits_updated_at = time.time()
        return {"items": results, "updated_at": self._credits_updated_at}

    @API("wb_diagnose", version="1")
    async def api_diagnose(self) -> dict[str, Any]:
        """WebUI 查询：本地诊断快照（不发写操作、不触发领取）。"""
        checks: list[dict[str, str]] = []

        def add(name: str, status: str, detail: str) -> None:
            checks.append({"name": name, "status": status, "detail": detail})

        cfg = self.config
        add("插件启用", "ok" if cfg.plugin.enabled else "error", "provider 开关")
        add(
            "账号池",
            "ok" if self._pool and self._pool.accounts else "error",
            f"{len(self._pool.accounts) if self._pool else 0} 个账号",
        )
        if self._models_server is not None:
            desc = self._models_server.describe()
            add(
                "本地模型服务",
                "ok" if desc["running"] else "error",
                desc["base_url"] if desc["running"] else (desc.get("error") or "未启动"),
            )
        else:
            add("本地模型服务", "warn", "模型目录功能未启用")
        state = self._catalog_state_payload()
        if state["count"]:
            status = "warn" if state["stale"] else "ok"
            add("模型目录", status, f"{state['count']} 个模型，更新于 {state['updated_text'] or '未知'}")
        else:
            add("模型目录", "error", "尚未成功读取目录")
        for realm, error in (state.get("errors") or {}).items():
            add(f"目录读取（{realm}）", "warn", str(error))

        plan = self._sync_plan_payload()
        if plan.get("error"):
            add("model_config", "error", plan["error"])
        else:
            patches = plan.get("provider_patch") or []
            add(
                "model_config",
                "ok" if not patches else "warn",
                "已对齐" if not patches else "待同步：" + ", ".join(patches),
            )
        if self._identity is not None:
            add("设备身份", "ok", "盐已就绪，按账号稳定派生")
        if self._pool is not None and self._pool.accounts:
            account = self._pool.active_account() or self._pool.accounts[0]
            model = cfg.pool.default_model
            try:
                queue = await asyncio.wait_for(
                    billing.fetch_queue_status(self._pool, account, model, self._identity), timeout=5.0
                )
                status = queue.get("status") or "idle"
                labels = {
                    "idle": "空闲",
                    "expired": "空闲",
                    "none": "空闲",
                    "waiting": "排队中",
                    "full": "队列已满",
                    "user_limit": "已达并发上限",
                }
                add(
                    "队列状态",
                    "ok" if status in ("idle", "expired", "none") else "warn",
                    f"{model}: {labels.get(status, status)}",
                )
            except Exception as exc:  # noqa: BLE001
                add("队列状态", "warn", f"读取失败：{str(exc)[:80]}")

        return {
            "ok": all(item["status"] != "error" for item in checks),
            "checks": checks,
            "generated_at": time.time(),
        }

    # ------------------------------------------------------------ 模型开关
    @API("wb_set_model_prefs", version="1")
    async def api_set_model_prefs(
        self, visual: list | None = None, reasoning: str = ""
    ) -> dict[str, Any]:
        """WebUI 操作：保存每模型的图片/思考开关（思考以 JSON 字符串传入）。"""
        if self._prefs is None:
            return {"error": "插件未初始化"}
        levels: dict[str, str] | None = None
        if reasoning:
            try:
                parsed = json.loads(reasoning)
            except ValueError:
                return {"error": "reasoning 不是合法 JSON"}
            if not isinstance(parsed, dict):
                return {"error": "reasoning 必须是 {模型id: 档位} 对象"}
            levels = {str(key): str(value) for key, value in parsed.items()}
        self._prefs.set(visual=visual, reasoning=levels)
        return {"models": self._model_rows()}

    # ------------------------------------------------------------ 模型同步
    @API("wb_sync_apply", version="1")
    async def api_sync_apply(self) -> dict[str, Any]:
        """WebUI 操作：按目录对齐本插件 provider 名下的模型（写前备份）。"""
        if self._models_server is None or not self._models_server.running:
            return {"error": "本地模型列表服务未启动，先修复服务再同步"}
        models = self._catalog_models()
        try:
            result = sync_apply(
                self._sync_path(),
                self.config.sync.provider_name,
                models,
                visual_ids=self._sync_visual_ids(),
                server_base_url=self._models_server.describe()["base_url"],
                allow_remove=self._sync_allow_remove(),
                backup_dir=self._data_dir() / "backups",
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("模型同步失败")
            return {"error": f"模型同步失败：{exc}"}
        return result

    # ------------------------------------------------------------ 设备身份
    @API("wb_identity_state", version="1")
    async def api_identity_state(self) -> dict[str, Any]:
        """WebUI 查询：每账号的设备身份。"""
        if self._identity is None or self._pool is None:
            return {"accounts": []}
        return self._identity.state(self._pool.accounts)

    @API("wb_identity_set", version="1")
    async def api_identity_set(
        self, ref: str, machine_id: str = "", session_id: str = ""
    ) -> dict[str, Any]:
        """WebUI 操作：覆盖某账号的设备 id（留空恢复派生值）。"""
        if self._identity is None or self._pool is None:
            return {"error": "插件未初始化"}
        account = self._pool.find(str(ref or ""))
        if account is None:
            return {"error": f"找不到账号：{ref}"}
        self._identity.set_override(account.id, machine_id, session_id)
        return self._identity.state(self._pool.accounts)

    @API("wb_identity_reset", version="1")
    async def api_identity_reset(self) -> dict[str, Any]:
        """WebUI 操作：重新生成设备盐并清空所有覆盖值。"""
        if self._identity is None or self._pool is None:
            return {"error": "插件未初始化"}
        self._identity.regenerate_salt()
        return self._identity.state(self._pool.accounts)

    # ------------------------------------------------------------ 账号运维
    @API("wb_account_toggle", version="1")
    async def api_account_toggle(self, ref: str) -> dict[str, Any]:
        """WebUI 操作：启用/停用账号（不删除凭据）。"""
        if self._pool is None:
            return {"error": "账号池未初始化"}
        account = self._pool.find(str(ref or ""))
        if account is None:
            return {"error": f"找不到账号：{ref}"}
        account.disabled = not account.disabled
        self._pool.save()
        return {"id": account.id, "disabled": account.disabled}

    @API("wb_account_refresh", version="1")
    async def api_account_refresh(self, ref: str) -> dict[str, Any]:
        """WebUI 操作：立即刷新该账号的 token。"""
        if self._pool is None:
            return {"error": "账号池未初始化"}
        account = self._pool.find(str(ref or ""))
        if account is None:
            return {"error": f"找不到账号：{ref}"}
        try:
            await self._pool.refresh(account)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"刷新失败：{exc}"}
        return {"id": account.id, "expires_at": account.expires_at}

    # ------------------------------------------------------------ 模型测试
    @API("wb_start_test", version="1")
    async def api_start_test(self, model: str) -> dict[str, Any]:
        """WebUI 操作：后台测一次真实调用，返回任务 id。"""
        model = (model or "").strip()
        if not model:
            return {"error": "缺少模型 id"}
        job = self._new_job(model)
        task = asyncio.create_task(self._run_test_job(job["id"], model))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return {"job": job}

    @API("wb_start_batch_test", version="1")
    async def api_start_batch_test(self, models: list | None = None) -> dict[str, Any]:
        """WebUI 操作：批量测试（串行，避免打爆账号额度）。"""
        targets = [str(item).strip() for item in (models or []) if str(item).strip()]
        if not targets:
            return {"error": "请选择要测试的模型"}
        jobs = [self._new_job(model) for model in targets[:20]]
        task = asyncio.create_task(self._run_batch_jobs(jobs))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return {"jobs": jobs}

    @API("wb_test_status", version="1")
    async def api_test_status(self) -> dict[str, Any]:
        """WebUI 查询：测试任务进度。"""
        return {"jobs": list(self._test_jobs.values())}

    def _new_job(self, model: str) -> dict[str, Any]:
        job_id = secrets.token_hex(4)
        job = {
            "id": job_id,
            "model": model,
            "status": "running",
            "latency_ms": 0,
            "text": "",
            "error": "",
            "started_at": time.time(),
        }
        self._test_jobs[job_id] = job
        while len(self._test_jobs) > 30:
            self._test_jobs.pop(next(iter(self._test_jobs)))
        return job

    async def _run_batch_jobs(self, jobs: list[dict[str, Any]]) -> None:
        for job in jobs:
            await self._run_test_job(job["id"], job["model"])

    async def _run_test_job(self, job_id: str, model: str) -> None:
        job = self._test_jobs.get(job_id)
        if job is None or self._service is None:
            return
        started = time.time()
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "只回复两个字：正常"},
            ],
            "max_tokens": 64,
            "stream": False,
        }
        try:
            data = await self._service.complete(payload)
            text = ""
            choices = data.get("choices") if isinstance(data, dict) else None
            if isinstance(choices, list) and choices:
                message = choices[0].get("message") if isinstance(choices[0], dict) else None
                if isinstance(message, dict):
                    text = str(message.get("content") or "")
            job.update(
                status="ok",
                latency_ms=int((time.time() - started) * 1000),
                text=text.strip()[:160],
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            job.update(
                status="fail",
                latency_ms=int((time.time() - started) * 1000),
                error=str(exc)[:300],
            )


def create_plugin() -> MaiBotPlugin:
    return WorkBuddyPoolPlugin()
