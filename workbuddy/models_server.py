"""本地 OpenAI 兼容模型列表服务。

MaiBot WebUI 的「获取 AI 模型」只认 HTTP：它拿 ``model_config.toml`` 里
provider 的 ``base_url`` + ``model_list_endpoint`` 直接 GET。WorkBuddy 上游没有
OpenAI 形状的 ``/models``，所以要由插件在容器内起一个只读小服务，把目录缓存按
OpenAI 格式吐出来。

只绑定 ``127.0.0.1``，只提供 GET，不写任何状态。
"""

from __future__ import annotations

import logging

from aiohttp import web

log = logging.getLogger("_maibot_plugin_local_workbuddy_pool.models_server")


class ModelsServer:
    """目录缓存的只读 HTTP 门面。"""

    def __init__(self, catalog, *, host: str = "127.0.0.1", port: int = 8765) -> None:
        self._catalog = catalog
        self._host = host or "127.0.0.1"
        self._port = int(port)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._error = ""

    # ------------------------------------------------------------------ 状态
    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}/v1"

    @property
    def running(self) -> bool:
        return self._runner is not None and self._site is not None

    @property
    def error(self) -> str:
        return self._error

    def describe(self) -> dict:
        return {
            "running": self.running,
            "host": self._host,
            "port": self._port,
            "base_url": self.url,
            "error": self._error,
        }

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        if self.running:
            return
        app = web.Application()
        app.router.add_get("/v1/models", self._handle_models)
        app.router.add_get("/models", self._handle_models)
        app.router.add_get("/healthz", self._handle_health)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        try:
            await site.start()
        except OSError as exc:
            self._error = f"监听 {self._host}:{self._port} 失败：{exc}"
            log.error("本地模型列表服务启动失败：%s", self._error)
            await runner.cleanup()
            return
        self._runner = runner
        self._site = site
        self._error = ""
        log.info("本地模型列表服务已启动：%s/models", self.url)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None

    # ------------------------------------------------------------------ 路由
    async def _handle_models(self, request: web.Request) -> web.Response:
        payload = self._catalog.openai_payload()
        return web.json_response(payload, headers={"Cache-Control": "no-store"})

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "models": len(self._catalog.models()),
                "updated_at": self._catalog.updated_at,
            }
        )
