"""
FastAPI server — serves ChatGPT as an API.

Launches the browser on startup, shuts it down on exit.

Usage:
    python -m src.api.server
    # or
    uvicorn src.api.server:app --host 192.168.8.222 --port 5061
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.browser.manager import BrowserManager
from src.browser.auto_login import ensure_logged_in
from src.chatgpt.client import ChatGPTClient
from src.claude.client import ClaudeClient
from src.config import Config
from src.files.cleanup import FileCleanupTask, sweep_expired_files, sweep_temp_files
from src.api.routes import router, set_client
from src.api.openai_routes import openai_router, set_openai_client, set_openai_services
from src.api.managed_routes import managed_router, set_managed_services
from src.api.dashboard import dashboard_router, set_dashboard_services
from src.api.agent_routes import agent_router, set_agent_services
from src.api.task_routes import task_router, set_task_services
from src.api.monitoring import RequestTelemetryMiddleware, request_tracker
from src.api.session_service import SessionWorkerPool
from src.api.session_guard import ProjectSessionMiddleware
from src.api.runtime_control import BrowserRuntimeMiddleware, runtime_controller
from src.api.errors import install_exception_handlers
from src.storage.database import Database
from src.log import setup_logging
from src.auth import resolve_token, set_auth_database

log = setup_logging("api_server")

# Global instances — needed for lifespan
_browser: BrowserManager | None = None
_client: ChatGPTClient | ClaudeClient | None = None
_database: Database | None = None
_session_pool: SessionWorkerPool | None = None
_cleanup_task: FileCleanupTask | None = None
_runtime_watchdog_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Keep the dashboard alive while independently controlling browser work."""
    global _browser, _client, _database, _session_pool, _cleanup_task, _runtime_watchdog_task

    _database = Database()
    set_auth_database(_database)
    sweep_expired_files(_database)
    sweep_temp_files()
    set_managed_services(_database, None)
    set_openai_services(_database, None)
    set_dashboard_services(None, None, _database)
    set_agent_services(_database)
    set_task_services(_database, None)

    async def start_browser_runtime() -> None:
        global _browser, _client, _session_pool
        if _browser is not None and _session_pool is not None:
            return

        browser = BrowserManager()
        pool: SessionWorkerPool | None = None
        try:
            log.info("Starting browser for API runtime...")
            page = await browser.start()
            target_url = Config.provider_url()
            provider_name = "Claude" if Config.PROVIDER == "claude" else "ChatGPT"
            log.info("Provider: %s (%s)", provider_name, target_url)

            for attempt in range(1, 6):
                try:
                    log.info("Navigation attempt %s/5 to %s", attempt, target_url)
                    await browser.navigate(target_url)
                    break
                except Exception as error:
                    log.warning("Navigation attempt %s failed: %s", attempt, error)
                    if attempt == 5:
                        raise
                    await asyncio.sleep(attempt * 5)

            await browser.apply_stealth_patches()
            await asyncio.sleep(3)
            if not await browser.is_logged_in():
                log.info("Not logged in — starting auto-login flow...")
                if not await ensure_logged_in(browser):
                    raise RuntimeError(f"Could not log in to {provider_name}")

            client: ChatGPTClient | ClaudeClient
            if Config.PROVIDER == "claude":
                client = ClaudeClient(page)
            else:
                client = ChatGPTClient(page)

            pool = SessionWorkerPool()
            await pool.start(browser)

            _browser = browser
            _client = client
            _session_pool = pool
            set_client(client, browser)
            set_openai_client(client)
            set_managed_services(_database, pool)
            set_openai_services(_database, pool)
            set_dashboard_services(browser, pool, _database)
            set_task_services(_database, pool)
            log.info("Browser runtime ready — logged in to %s", provider_name)
        except Exception:
            if pool is not None:
                await pool.close()
            await browser.close()
            raise

    async def stop_browser_runtime() -> None:
        global _browser, _client, _session_pool
        pool, browser = _session_pool, _browser
        _session_pool = None
        _client = None
        _browser = None
        set_client(None, None)
        set_openai_client(None)
        set_managed_services(_database, None)
        set_openai_services(_database, None)
        set_dashboard_services(None, None, _database)
        set_task_services(_database, None)
        if pool is not None:
            await pool.close()
        if browser is not None:
            log.info("Closing browser runtime and all worker pages...")
            await browser.close()
            log.info("Browser runtime pages closed")

    runtime_controller.configure(start_browser_runtime, stop_browser_runtime)

    async def runtime_watchdog() -> None:
        while True:
            await asyncio.sleep(15)
            pool = _session_pool
            if not runtime_controller.running or pool is None:
                continue
            try:
                if pool.healthy < pool.size and pool.available == pool.size:
                    log.warning(
                        "Detected closed idle worker page(s): healthy=%s/%s; repairing",
                        pool.healthy,
                        pool.size,
                    )
                    await pool.repair_closed_workers()
            except Exception as error:
                log.error("Worker page repair failed: %s", error, exc_info=True)

    _cleanup_task = FileCleanupTask(_database)
    _cleanup_task.start()
    await runtime_controller.restore()
    _runtime_watchdog_task = asyncio.create_task(runtime_watchdog())
    log.info("API dashboard ready — runtime status=%s", runtime_controller.status)

    yield  # Server is running

    if _cleanup_task:
        await _cleanup_task.stop()
    if _runtime_watchdog_task:
        _runtime_watchdog_task.cancel()
        try:
            await _runtime_watchdog_task
        except asyncio.CancelledError:
            pass
    await runtime_controller.shutdown()


app = FastAPI(
    title="CatGPT Gateway API",
    description=(
        "把 ChatGPT / Claude 网页能力包装成 HTTP API 的网关服务。\n\n"
        "**多用户、多会话、文件上传下载请优先使用「多用户会话与文件」分组**"
        "（/v1/sessions、/v1/files），它提供用户隔离、数据库存储和单 Worker 串行执行。\n\n"
        "「OpenAI 兼容接口」分组兼容 OpenAI SDK；请求中传入 `session_id` "
        "即可走单 Worker 会话队列并获得用户隔离与消息持久化。\n\n"
        "「旧版兼容接口」为早期单会话接口，仅作兼容保留。\n\n"
        "认证方式：业务接口需在请求头携带 `Authorization: Bearer <token>`。"
        "推荐在运维仪表盘中为每个项目生成独立 Token；.env 中的 "
        "API_TOKEN / API_USER_TOKENS 仅用于向后兼容。"
        "点击右上角 **Authorize** 按钮即可填入 token 进行在线调试。"
    ),
    version="1.4.4",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "多用户会话与文件", "description": "推荐入口：多用户隔离的会话管理、消息发送、文件上传与下载。"},
        {"name": "OpenAI 兼容接口", "description": "兼容 OpenAI SDK 的聊天、图片生成与模型列表接口，可通过 session_id 接入单 Worker 会话队列。"},
        {"name": "旧版兼容接口", "description": "早期单会话接口，仅作兼容保留，不建议新接入使用。"},
        {"name": "Agents 与能力", "description": "项目内 Agent 实体与最小 Capability ACL。"},
        {"name": "多智能体 Task", "description": "一个 Task 一个独立对话、并发入队、单页串行执行。"},
    ],
)
install_exception_handlers(app)

# ── Bearer Token Auth Middleware ────────────────────────────────
class BearerTokenMiddleware:
    """
    Pure ASGI middleware for Bearer token auth.

    Uses raw ASGI protocol instead of BaseHTTPMiddleware to avoid the
    Python 3.9 event-loop mismatch bug that corrupts asyncio.Lock
    when exceptions propagate through BaseHTTPMiddleware's task group.

    Skips auth for documentation, health checks, and the read-only dashboard.
    """

    OPEN_PATHS = {
        b"/docs", b"/redoc", b"/openapi.json", b"/healthz",
        b"/dashboard", b"/dashboard/", b"/api/dashboard/state",
        b"/api/dashboard/logs", b"/api/dashboard/developer-guide",
    }

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path_str = scope.get("path", "")
        if (
            path_str in {
                "/docs", "/redoc", "/openapi.json", "/healthz",
                "/dashboard", "/dashboard/", "/api/dashboard/state",
                "/api/dashboard/logs", "/api/dashboard/developer-guide",
            }
            or path_str.startswith("/api/dashboard/projects")
            or path_str.startswith("/api/dashboard/runtime")
            or (path_str.startswith("/v1/files/") and path_str.endswith("/content"))
        ):
            await self.app(scope, receive, send)
            return

        token_owners = Config.token_owners()
        if not token_owners and (_database is None or _database.project_count(enabled_only=True) == 0):
            log.error("Business API locked: no enabled project token is configured")
            response = JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "type": "service_not_configured",
                        "code": "SERVICE_NOT_CONFIGURED",
                        "message": "No project token is configured; initialize one through the authenticated admin API",
                        "retryable": False,
                    }
                },
            )
            await response(scope, receive, send)
            return

        # Extract Authorization header
        headers = dict(scope.get("headers", []))
        auth_value = headers.get(b"authorization", b"").decode()

        provided = ""
        if auth_value.startswith("Bearer "):
            provided = auth_value[7:]

        identity = resolve_token(provided)
        if identity is None:
            response = JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "message": "Invalid or missing project token. Set Authorization: Bearer <PROJECT_TOKEN>",
                        "type": "auth_error",
                    }
                },
            )
            await response(scope, receive, send)
            return

        scope["catgpt.owner_id"] = identity["project_id"]
        scope["catgpt.project"] = identity
        await self.app(scope, receive, send)


app.add_middleware(ProjectSessionMiddleware)
app.add_middleware(BrowserRuntimeMiddleware)
app.add_middleware(BearerTokenMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(Config.CORS_ALLOWED_ORIGINS),
    allow_credentials=bool(Config.CORS_ALLOWED_ORIGINS) and "*" not in Config.CORS_ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Added last so telemetry wraps authentication and receives the resolved
# owner_id in scope while still measuring rejected/failed requests.
app.add_middleware(RequestTelemetryMiddleware, tracker=request_tracker)

app.include_router(router)
app.include_router(openai_router)
app.include_router(managed_router)
app.include_router(dashboard_router)
app.include_router(agent_router)
app.include_router(task_router)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    """Unauthenticated health-check for Docker / load-balancers."""
    return {"status": "ok"}


if __name__ == "__main__":
    import logging
    import uvicorn

    uvicorn_log = setup_logging("uvicorn")
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        framework_logger = logging.getLogger(logger_name)
        framework_logger.handlers = list(uvicorn_log.handlers)
        framework_logger.setLevel(logging.INFO)
        framework_logger.propagate = False

    uvicorn.run(
        "src.api.server:app",
        host=Config.API_HOST,
        port=Config.API_PORT,
        log_level="info",
        log_config=None,
        access_log=False,
    )
