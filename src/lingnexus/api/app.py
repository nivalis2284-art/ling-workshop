import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from lingnexus.api import (
    agents,
    background,
    browser,
    chat,
    content,
    conversations,
    dependencies,
    execution,
    maintenance,
    mcp,
    models,
    permissions,
    sandbox,
    skillpacks,
    system,
    tasks,
    usage,
    work_events,
)
from lingnexus.api.upload_boundary import UploadBoundary
from lingnexus.app import Application
from lingnexus.background import Supervisor
from lingnexus.browser import BrowserService
from lingnexus.common.errors import Conflict, DomainError, NotFound, PermissionDenied
from lingnexus.dependencies import DependencyService
from lingnexus.maintenance.automatic import AutomaticBackups
from lingnexus.runtime import ChatRuntime
from lingnexus.tasks import TaskRuntime
from lingnexus.work_events.summaries import EventSummaryRunner


def _package_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("lingnexus")
    except PackageNotFoundError:
        return "unknown"


def default_data_dir() -> Path:
    override = os.environ.get("LINGNEXUS_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "LingNexus" if local else Path.home() / ".local/share/lingnexus"


def create_app(
    *,
    data_dir: Path | None = None,
    token: str | None = None,
    allowed_origins: set[str] | None = None,
    frontend_dir: Path | None = None,
) -> FastAPI:
    directory = data_dir or default_data_dir()
    owner_token = token or os.environ.get("LINGNEXUS_OWNER_TOKEN") or secrets.token_urlsafe(32)
    if len(owner_token) < 8:
        raise ValueError("本机会话令牌过短")

    @asynccontextmanager
    async def lifespan(server: FastAPI):
        server.state.services = Application(
            directory / "ling.db", credential_cache=sys.platform == "win32"
        )
        # Startup engine probe: it informs the upgrade prompt but never switches tiers.
        server.state.services.sandbox.probe()
        chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
        server.state.browser = await BrowserService(
            server.state.services.database,
            directory,
            executable_path=chrome if chrome.exists() else None,
        ).start()
        server.state.services.browser = server.state.browser

        def chat_busy():
            with server.state.services.database.transaction() as db:
                return (
                    db.execute("SELECT 1 FROM chat_runs WHERE status='running' LIMIT 1").fetchone()
                    is not None
                )

        server.state.tasks = TaskRuntime(
            server.state.services, foreground_busy=chat_busy
        )
        await server.state.tasks.start()
        server.state.dependencies = DependencyService(
            server.state.services, server.state.tasks,
            sandbox=server.state.services.sandbox,
        )
        server.state.services.dependencies = server.state.dependencies
        server.state.sandbox = server.state.services.sandbox
        server.state.services.automatic_backups = AutomaticBackups(server.state.services)
        server.state.chat_runtime = ChatRuntime(server.state.services)

        def foreground_busy():
            with server.state.services.database.transaction() as db:
                return (
                    db.execute(
                        "SELECT 1 FROM chat_runs WHERE status='running' UNION SELECT 1 FROM tasks WHERE status='running' LIMIT 1"
                    ).fetchone()
                    is not None
                )

        server.state.background = Supervisor(
            server.state.services, foreground_busy=foreground_busy
        )
        server.state.services.event_summaries = EventSummaryRunner(
            server.state.services, foreground_busy=foreground_busy
        )
        server.state.background.event_summaries = server.state.services.event_summaries
        server.state.background.start()
        server.state.background.automatic_backups = server.state.services.automatic_backups
        try:
            yield
        finally:
            await server.state.background.aclose()
            await server.state.services.event_summaries.aclose()
            await server.state.chat_runtime.aclose()
            await server.state.dependencies.aclose()
            await server.state.tasks.aclose()
            await server.state.sandbox.sessions.aclose()
            await server.state.browser.aclose()
            server.state.services.close()

    server = FastAPI(title="LingNexus", lifespan=lifespan)
    server.state.owner_token = owner_token
    server.state.allowed_origins = allowed_origins or {
        "http://127.0.0.1:8765",
        "http://localhost:8765",
    }
    server.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    server.add_middleware(UploadBoundary)
    dev_origin = os.environ.get("LINGNEXUS_DEV_ORIGIN")
    if dev_origin:
        origin_url = urlsplit(dev_origin)
        if (
            origin_url.scheme != "http"
            or origin_url.hostname not in {"localhost", "127.0.0.1"}
            or origin_url.path
            or origin_url.username
        ):
            raise ValueError("开发页面地址必须是本机 HTTP 来源")
        server.state.allowed_origins.add(dev_origin)

    @server.exception_handler(DomainError)
    async def domain_error(request: Request, error: DomainError):
        status = (
            409
            if isinstance(error, Conflict)
            else 404
            if isinstance(error, NotFound)
            else 403
            if isinstance(error, PermissionDenied)
            else 422
        )
        return JSONResponse(status_code=status, content={"detail": str(error)})

    @server.exception_handler(ValueError)
    async def invalid_value(request: Request, error: ValueError):
        return JSONResponse(status_code=422, content={"detail": "请求内容无效"})

    @server.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        # FastAPI's default error response can echo a submitted secret in input fields.
        return JSONResponse(status_code=422, content={"detail": "请求字段或类型不正确"})

    @server.exception_handler(OSError)
    async def filesystem_error(request: Request, error: OSError):
        return JSONResponse(status_code=409, content={"detail": "无法访问文件，或目标已存在"})

    @server.get("/health")
    def health():
        return {"status": "ok", "version": _package_version()}

    server.include_router(agents.router)
    server.include_router(maintenance.router)
    server.include_router(models.router)
    server.include_router(content.router)
    server.include_router(chat.router)
    server.include_router(usage.router)
    server.include_router(conversations.router)
    server.include_router(permissions.router)
    server.include_router(background.router)
    server.include_router(browser.router)
    server.include_router(tasks.router)
    server.include_router(dependencies.router)
    server.include_router(sandbox.router)
    server.include_router(system.router)
    server.include_router(mcp.router)
    server.include_router(skillpacks.router)
    server.include_router(execution.router)
    server.include_router(work_events.router)
    for router in (
        agents.router,
        maintenance.router,
        models.router,
        content.router,
        chat.router,
        usage.router,
        conversations.router,
        permissions.router,
        background.router,
        browser.router,
        tasks.router,
        dependencies.router,
        sandbox.router,
        system.router,
        mcp.router,
        skillpacks.router,
        execution.router,
        work_events.router,
    ):
        server.include_router(router, prefix="/api")
    bundled = getattr(sys, "_MEIPASS", None)
    assets = frontend_dir or (
        Path(bundled) / "frontend"
        if bundled
        else Path(__file__).resolve().parents[3] / "frontend" / "dist"
    )
    if assets.is_dir():
        server.mount("/", StaticFiles(directory=assets, html=True), name="frontend")
    return server
