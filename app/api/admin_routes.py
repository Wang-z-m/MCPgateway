from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.errors import GatewayError


router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/reload")
async def reload_config(request: Request) -> JSONResponse:
    try:
        request.app.state.auth.require_admin_key(request)
        result = await request.app.state.registry.reload()
        return JSONResponse(
            {
                "ok": True,
                **result,
                "gateway": request.app.state.registry.status(),
            }
        )
    except GatewayError as exc:
        return JSONResponse(
            status_code=401,
            content={"ok": False, "error": exc.message, "category": exc.category},
        )
    except Exception as exc:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": str(exc),
                "gateway": request.app.state.registry.status(),
            },
        )


@router.post("/rollback")
async def rollback_config(request: Request) -> JSONResponse:
    try:
        request.app.state.auth.require_admin_key(request)
        result = await request.app.state.registry.rollback()
        return JSONResponse(
            {
                "ok": True,
                **result,
                "gateway": request.app.state.registry.status(),
            }
        )
    except GatewayError as exc:
        return JSONResponse(
            status_code=401,
            content={"ok": False, "error": exc.message, "category": exc.category},
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": str(exc),
                "gateway": request.app.state.registry.status(),
            },
        )
    except Exception as exc:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": str(exc),
                "gateway": request.app.state.registry.status(),
            },
        )


@router.get("/tools")
async def list_registered_tools(request: Request) -> JSONResponse:
    try:
        request.app.state.auth.require_admin_key(request)
        return JSONResponse(
            {
                "ok": True,
                "summary": request.app.state.registry.management_summary(),
                "tools": request.app.state.registry.describe_tools(),
                "openapi_preview_tools": request.app.state.registry.preview_tools(),
            }
        )
    except GatewayError as exc:
        return JSONResponse(
            status_code=401,
            content={"ok": False, "error": exc.message, "category": exc.category},
        )


@router.get("/status")
async def gateway_status(request: Request) -> JSONResponse:
    try:
        request.app.state.auth.require_admin_key(request)
        metrics = await request.app.state.access_logs.build_metrics_snapshot()
        audit = await request.app.state.config_audit.latest_events()
        audit_summary = await request.app.state.config_audit.build_summary()
        recent_calls = await request.app.state.access_logs.latest_calls()
        return JSONResponse(
            {
                "ok": True,
                "gateway": request.app.state.registry.status(),
                "security": {
                    "auth": request.app.state.auth.describe(),
                    "rate_limit": request.app.state.rate_limiter.describe(),
                },
                "sessions": request.app.state.session_manager.describe(),
                "metrics": metrics,
                "audit_summary": audit_summary,
                "recent_audit": audit,
                "recent_calls": recent_calls,
                "openapi_preview_count": len(
                    request.app.state.config_loader.preview_openapi_tools()
                ),
            }
        )
    except GatewayError as exc:
        return JSONResponse(
            status_code=401,
            content={"ok": False, "error": exc.message, "category": exc.category},
        )
