from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from watchfiles import awatch

from app.api.admin_routes import router as admin_router
from app.api.health_routes import router as health_router
from app.api.mcp_routes import router as mcp_router
from app.core.adaptation_engine import AdaptationEngine
from app.core.auth import ApiKeyAuth
from app.core.config_loader import ConfigLoader, SUPPORTED_CONFIG_SUFFIXES
from app.core.discovery_engine import ToolDiscoveryEngine
from app.core.mcp_service import McpService
from app.core.rate_limit import SlidingWindowRateLimiter
from app.core.rest_connector import RestConnector
from app.core.session_manager import SessionManager
from app.core.tool_registry import ToolRegistry
from app.db.database import Database
from app.db.repositories import AccessLogRepository, ConfigAuditRepository
from app.settings import Settings
from app.utils.logging import configure_logging, log_json, set_request_id

logger = logging.getLogger(__name__)

_CONFIG_DEBOUNCE_SECONDS = 1.0


def _is_config_file(path: str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_CONFIG_SUFFIXES


async def _watch_config_dir(config_dir: Path, registry: ToolRegistry) -> None:
    if not config_dir.exists():
        return
    log_json(logger, logging.INFO, "config_watcher_started", watch_dir=str(config_dir))
    try:
        async for changes in awatch(
            config_dir,
            debounce=int(_CONFIG_DEBOUNCE_SECONDS * 1000),
            step=200,
            rust_timeout=5000,
        ):
            config_changes = [
                (change, path) for change, path in changes if _is_config_file(path)
            ]
            if not config_changes:
                continue
            changed_files = [Path(p).name for _, p in config_changes]
            log_json(
                logger,
                logging.INFO,
                "config_file_changed",
                changed_files=changed_files,
            )
            try:
                result = await registry.reload()
                log_json(
                    logger,
                    logging.INFO,
                    "config_auto_reloaded",
                    version=result["version"],
                    tool_count=result["tool_count"],
                    changes=result["changes"],
                )
            except Exception as exc:
                log_json(
                    logger,
                    logging.ERROR,
                    "config_auto_reload_failed",
                    error=str(exc),
                )
    except asyncio.CancelledError:
        log_json(logger, logging.INFO, "config_watcher_stopped")
        raise


_SESSION_CLEANUP_INTERVAL_SECONDS = 60


async def _cleanup_sessions_periodically(session_manager: SessionManager) -> None:
    try:
        while True:
            await asyncio.sleep(_SESSION_CLEANUP_INTERVAL_SECONDS)
            await session_manager.cleanup_expired()
    except asyncio.CancelledError:
        log_json(logger, logging.INFO, "session_cleanup_task_stopped")
        raise


def create_app(
    settings: Settings | None = None,
    *,
    connector: RestConnector | None = None,
) -> FastAPI:
    base_dir = Path(__file__).resolve().parents[1]
    app_settings = (settings or Settings.from_env()).with_base_dir(base_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(app_settings.log_level)
        database = Database(app_settings.sqlite_path)
        await database.initialize()

        config_loader = ConfigLoader(app_settings.config_dir, app_settings.openapi_dir)
        config_audit = ConfigAuditRepository(database)
        access_logs = AccessLogRepository(database)
        registry = ToolRegistry(config_loader, config_audit)
        await registry.load_initial_snapshot()

        rest_connector = connector or RestConnector()
        adaptation_engine = AdaptationEngine(rest_connector, access_logs)
        auth = ApiKeyAuth(app_settings.api_key, app_settings.admin_api_key)
        rate_limiter = SlidingWindowRateLimiter(
            app_settings.rate_limit_max_requests,
            app_settings.rate_limit_window_seconds,
        )

        discovery_engine = ToolDiscoveryEngine(app_settings)
        discovery_engine.rebuild_index(registry.list_tools())
        registry.set_discovery_engine(discovery_engine)

        discovery_rate_limiter = SlidingWindowRateLimiter(
            app_settings.discovery_rate_limit_max,
            app_settings.discovery_rate_limit_window,
        )

        mcp_service = McpService(
            registry,
            adaptation_engine,
            server_name=app_settings.project_name,
            server_version=app_settings.project_version,
            discovery_engine=discovery_engine,
            discovery_rate_limiter=discovery_rate_limiter,
        )
        session_manager = SessionManager(
            ttl_seconds=app_settings.session_ttl_seconds,
        )

        app.state.settings = app_settings
        app.state.database = database
        app.state.config_loader = config_loader
        app.state.config_audit = config_audit
        app.state.access_logs = access_logs
        app.state.registry = registry
        app.state.auth = auth
        app.state.rate_limiter = rate_limiter
        app.state.mcp_service = mcp_service
        app.state.session_manager = session_manager
        app.state.connector = rest_connector
        app.state.discovery_engine = discovery_engine

        watcher_task = asyncio.create_task(
            _watch_config_dir(app_settings.config_dir, registry)
        )
        session_cleanup_task = asyncio.create_task(
            _cleanup_sessions_periodically(session_manager)
        )
        yield
        watcher_task.cancel()
        session_cleanup_task.cancel()
        try:
            await watcher_task
        except asyncio.CancelledError:
            pass
        try:
            await session_cleanup_task
        except asyncio.CancelledError:
            pass
        await rest_connector.close()

    app = FastAPI(
        title="MCP Smart API Gateway",
        version=app_settings.project_version,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id", uuid4().hex)
        set_request_id(request_id)
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        response.headers["x-request-id"] = request_id
        log_json(
            logger,
            logging.INFO,
            "http_request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            latency_ms=elapsed_ms,
        )
        return response

    app.include_router(health_router)
    app.include_router(admin_router)
    app.include_router(mcp_router)

    static_dir = base_dir / "static"
    if static_dir.exists():
        @app.get("/dashboard")
        async def dashboard():
            return FileResponse(static_dir / "index.html")

        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    return app


app = create_app()
