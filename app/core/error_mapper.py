from __future__ import annotations

import json
from typing import Any

import httpx
from jsonschema import ValidationError

from app.core.errors import GatewayError
from app.models.tool_config import ErrorMapping

JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_INTERNAL_ERROR = -32603
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_PARSE_ERROR = -32700

ERROR_CODES: dict[str, int] = {
    "PARSE_ERROR": JSONRPC_PARSE_ERROR,
    "INVALID_REQUEST": JSONRPC_INVALID_REQUEST,
    "VALIDATION_ERROR": JSONRPC_INVALID_PARAMS,
    "TOOL_NOT_FOUND": JSONRPC_METHOD_NOT_FOUND,
    "DOWNSTREAM_ERROR": -32050,
    "TIMEOUT_ERROR": -32060,
    "INTERNAL_ERROR": JSONRPC_INTERNAL_ERROR,
    "UNAUTHORIZED": -32001,
    "RATE_LIMITED": -32029,
}


def gateway_error(
    category: str,
    message: str,
    *,
    data: dict[str, Any] | None = None,
) -> GatewayError:
    return GatewayError(
        category=category,
        message=message,
        jsonrpc_code=ERROR_CODES.get(category, JSONRPC_INTERNAL_ERROR),
        data=data or {},
    )


def _extract_downstream_message(response_text: str) -> str | None:
    raw_text = response_text.strip()
    if not raw_text:
        return None
    try:
        payload = json.loads(raw_text)
    except ValueError:
        return raw_text[:300]

    if isinstance(payload, dict):
        for key in ("detail", "message", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return raw_text[:300]


def from_validation_error(exc: ValidationError) -> GatewayError:
    return gateway_error(
        "VALIDATION_ERROR",
        "Tool arguments do not match the input schema.",
        data={
            "validator": exc.validator,
            "validator_value": exc.validator_value,
            "path": list(exc.path),
            "message": exc.message,
        },
    )


def from_http_error(
    *,
    status_code: int,
    response_text: str,
    elapsed_ms: int,
    error_mapping: ErrorMapping,
) -> GatewayError:
    category = error_mapping.status_map.get(str(status_code), error_mapping.default_code)
    data: dict[str, Any] = {
        "downstream_status": status_code,
        "elapsed_ms": elapsed_ms,
    }
    downstream_message = _extract_downstream_message(response_text)
    if downstream_message:
        data["downstream_message"] = downstream_message
    if error_mapping.expose_downstream_body:
        data["downstream_body"] = response_text
    return gateway_error(
        category,
        f"Downstream REST service returned HTTP {status_code}.",
        data=data,
    )


def from_timeout(exc: httpx.TimeoutException) -> GatewayError:
    return gateway_error(
        "TIMEOUT_ERROR",
        "Downstream REST service timed out.",
        data={"detail": str(exc)},
    )


def from_request_error(exc: httpx.RequestError) -> GatewayError:
    request_url = str(exc.request.url) if exc.request else None
    return gateway_error(
        "DOWNSTREAM_ERROR",
        "Failed to connect to the downstream REST service.",
        data={"detail": str(exc), "request_url": request_url},
    )


def from_unknown_exception(exc: Exception) -> GatewayError:
    return gateway_error(
        "INTERNAL_ERROR",
        "Gateway internal error.",
        data={"detail": str(exc)},
    )
