from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from app.core.errors import GatewayError
from app.core.mcp_service import (
    build_gateway_error_response,
    build_invalid_request_response,
    build_parse_error_response,
    build_request_shape_error_response,
)
from app.core.session_manager import Session
from app.models.jsonrpc import JsonRpcRequest


router = APIRouter(tags=["mcp"])

_SESSION_HEADER = "mcp-session-id"


def _extract_request_id(body: Any) -> str | int | None:
    if isinstance(body, dict):
        request_id = body.get("id")
        if isinstance(request_id, (str, int)) or request_id is None:
            return request_id
    return None


def _apply_rate_limit_headers(response: Response, rate_limit: dict[str, int]) -> Response:
    response.headers["x-rate-limit-limit"] = str(rate_limit["max_requests"])
    response.headers["x-rate-limit-remaining"] = str(rate_limit["remaining"])
    response.headers["x-rate-limit-reset"] = str(rate_limit["retry_after_seconds"])
    return response


def _session_error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": message},
    )


async def _resolve_session(request: Request, session_id: str | None) -> Session | JSONResponse:
    """Look up and validate a session. Returns the Session or a JSONResponse error."""
    if not session_id:
        return _session_error(400, "Missing Mcp-Session-Id header.")
    session = await request.app.state.session_manager.get_session(session_id)
    if session is None:
        return _session_error(404, "Session not found or expired.")
    return session


@router.post("/mcp", response_model=None)
async def mcp_endpoint(request: Request) -> JSONResponse | Response:
    request_id = None
    rate_limit_status = None
    try:
        raw_body = await request.json()
        if not isinstance(raw_body, dict):
            return JSONResponse(
                build_request_shape_error_response(
                    None,
                    "Only single JSON-RPC request objects are supported.",
                    {"received_type": type(raw_body).__name__},
                )
            )

        request_id = _extract_request_id(raw_body)
        method = raw_body.get("method", "")
        is_notification = "id" not in raw_body
        session_id = request.headers.get(_SESSION_HEADER)

        request.app.state.auth.require_gateway_key(request)
        client_key = request.headers.get(
            "x-api-key", request.client.host if request.client else "unknown"
        )
        rate_limit_status = request.app.state.rate_limiter.enforce(client_key)

        if method == "initialize":
            rpc_request = JsonRpcRequest.model_validate(raw_body)
            payload = await request.app.state.mcp_service.handle(
                rpc_request, client_key=client_key
            )
            init_params = raw_body.get("params") or {}
            session = await request.app.state.session_manager.create_session(
                client_info=init_params.get("clientInfo"),
                protocol_version=init_params.get("protocolVersion", "2025-03-26"),
            )
            response = JSONResponse(payload)
            response.headers[_SESSION_HEADER] = session.session_id
            if rate_limit_status is not None:
                _apply_rate_limit_headers(
                    response,
                    {
                        "max_requests": rate_limit_status.max_requests,
                        "remaining": rate_limit_status.remaining,
                        "retry_after_seconds": rate_limit_status.retry_after_seconds,
                    },
                )
            return response

        if is_notification and method == "notifications/initialized":
            session_or_error = await _resolve_session(request, session_id)
            if isinstance(session_or_error, JSONResponse):
                return session_or_error
            await request.app.state.session_manager.mark_initialized(
                session_or_error.session_id
            )
            return Response(status_code=202)

        if is_notification:
            session_or_error = await _resolve_session(request, session_id)
            if isinstance(session_or_error, JSONResponse):
                return session_or_error
            if not session_or_error.initialized:
                return _session_error(400, "Session not yet initialized.")
            return Response(status_code=202)

        session_or_error = await _resolve_session(request, session_id)
        if isinstance(session_or_error, JSONResponse):
            return session_or_error
        if not session_or_error.initialized:
            return _session_error(
                400, "Session not yet initialized. Send notifications/initialized first."
            )

        rpc_request = JsonRpcRequest.model_validate(raw_body)
        payload = await request.app.state.mcp_service.handle(
            rpc_request, client_key=client_key
        )
        response = JSONResponse(payload)
        if rate_limit_status is not None:
            _apply_rate_limit_headers(
                response,
                {
                    "max_requests": rate_limit_status.max_requests,
                    "remaining": rate_limit_status.remaining,
                    "retry_after_seconds": rate_limit_status.retry_after_seconds,
                },
            )
        return response

    except ValueError:
        return JSONResponse(build_parse_error_response("Request body must be valid JSON."))
    except ValidationError as exc:
        return JSONResponse(
            build_request_shape_error_response(
                request_id,
                "Invalid JSON-RPC request.",
                {"detail": exc.errors()},
            )
        )
    except GatewayError as exc:
        response = JSONResponse(build_gateway_error_response(request_id, exc))
        if exc.category == "RATE_LIMITED":
            response = _apply_rate_limit_headers(
                response,
                {
                    "max_requests": int(exc.data.get("max_requests", 0)),
                    "remaining": int(exc.data.get("remaining", 0)),
                    "retry_after_seconds": int(exc.data.get("retry_after_seconds", 0)),
                },
            )
        return response


@router.delete("/mcp", response_model=None)
async def mcp_terminate_session(request: Request) -> Response:
    session_id = request.headers.get(_SESSION_HEADER)
    if not session_id:
        return _session_error(400, "Missing Mcp-Session-Id header.")
    removed = await request.app.state.session_manager.terminate_session(session_id)
    if not removed:
        return _session_error(404, "Session not found.")
    return Response(status_code=204)
