from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from app.core.discovery_engine import SEARCH_APIS_SCHEMA, ToolDiscoveryEngine
from app.core.error_mapper import (
    JSONRPC_INVALID_REQUEST,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_PARSE_ERROR,
    gateway_error,
)
from app.core.errors import GatewayError
from app.core.rate_limit import SlidingWindowRateLimiter
from app.core.tool_registry import ToolRegistry
from app.models.discovery_models import SearchApisParams
from app.models.jsonrpc import InitializeParams, JsonRpcError, JsonRpcRequest, JsonRpcResponse, ToolCallParams
from app.core.adaptation_engine import AdaptationEngine
from app.utils.logging import log_json

logger = logging.getLogger(__name__)

SUPPORTED_METHODS = ("initialize", "notifications/initialized", "tools/list", "tools/call")

_TOOL_EXEC_ERROR_CATEGORIES = frozenset({"DOWNSTREAM_ERROR", "TIMEOUT_ERROR", "INTERNAL_ERROR"})

_SEARCH_APIS_NAME = "search_apis"


class McpService:
    def __init__(
        self,
        registry: ToolRegistry,
        adaptation_engine: AdaptationEngine,
        *,
        server_name: str,
        server_version: str,
        discovery_engine: ToolDiscoveryEngine | None = None,
        discovery_rate_limiter: SlidingWindowRateLimiter | None = None,
    ) -> None:
        self.registry = registry
        self.adaptation_engine = adaptation_engine
        self.server_name = server_name
        self.server_version = server_version
        self.discovery_engine = discovery_engine
        self.discovery_rate_limiter = discovery_rate_limiter

    async def handle(
        self,
        request: JsonRpcRequest,
        *,
        client_key: str | None = None,
    ) -> dict[str, Any]:
        try:
            if request.method == "initialize":
                params = InitializeParams.model_validate(request.params or {})
                return self._success(
                    request.id,
                    {
                        "protocolVersion": params.protocolVersion or "2025-03-26",
                        "serverInfo": {
                            "name": self.server_name,
                            "version": self.server_version,
                        },
                        "capabilities": {
                            "tools": {"listChanged": True},
                            "logging": {},
                        },
                    },
                )
            if request.method == "tools/list":
                return self._build_tools_list(request.id)
            if request.method == "tools/call":
                params = ToolCallParams.model_validate(request.params or {})
                if params.name == _SEARCH_APIS_NAME:
                    return self._handle_search_apis(request.id, params.arguments, client_key)
                tool = self.registry.get_tool(params.name)
                try:
                    result = await self.adaptation_engine.execute_tool(tool, params.arguments)
                except GatewayError as exc:
                    if exc.category in _TOOL_EXEC_ERROR_CATEGORIES:
                        return self._tool_error_result(request.id, exc)
                    raise
                return self._success(request.id, result)
            raise gateway_error(
                "TOOL_NOT_FOUND",
                f"Method '{request.method}' not found.",
                data={
                    "method": request.method,
                    "supported_methods": list(SUPPORTED_METHODS),
                },
            )
        except GatewayError as exc:
            return self._error(request.id, exc)
        except Exception as exc:
            return self._error(
                request.id,
                gateway_error(
                    "INTERNAL_ERROR",
                    "Unhandled error while processing the MCP request.",
                    data={"detail": str(exc)},
                ),
            )

    def _build_tools_list(self, request_id: str | int | None) -> dict[str, Any]:
        if self.discovery_engine is not None:
            tools = self.registry.list_primary_tools()
            schemas = [tool.mcp_tool_schema() for tool in tools]
            schemas.append(SEARCH_APIS_SCHEMA)
        else:
            schemas = [tool.mcp_tool_schema() for tool in self.registry.list_tools()]
        return self._success(request_id, {"tools": schemas})

    def _handle_search_apis(
        self,
        request_id: str | int | None,
        arguments: dict[str, Any],
        client_key: str | None,
    ) -> dict[str, Any]:
        if self.discovery_engine is None:
            raise gateway_error(
                "INTERNAL_ERROR",
                "Discovery engine is not available.",
            )

        if self.discovery_rate_limiter is not None:
            effective_key = client_key or "__anonymous__"
            try:
                self.discovery_rate_limiter.enforce(effective_key)
            except GatewayError:
                self.discovery_engine.increment_rate_limited()
                log_json(
                    logger,
                    logging.WARNING,
                    "search_apis_rate_limited",
                    request_id=request_id,
                    client_key=effective_key,
                )
                return self._success(request_id, {
                    "content": [{
                        "type": "text",
                        "text": (
                            "[RATE_LIMITED] API 搜索调用过于频繁，请稍后再试。"
                            "如果多次搜索未找到合适工具，当前系统可能不支持该功能，"
                            "请直接告知用户。"
                        ),
                    }],
                    "isError": True,
                })

        try:
            search_params = SearchApisParams.model_validate(arguments)
        except ValidationError as exc:
            raise gateway_error(
                "VALIDATION_ERROR",
                "Invalid search_apis parameters.",
                data={"detail": exc.errors()},
            ) from exc

        primary_tools = self.registry.list_primary_tools()
        try:
            result = self.discovery_engine.search(
                query=search_params.query,
                category=search_params.category,
                top_k=search_params.top_k,
                primary_tools=primary_tools,
                request_id=request_id,
            )
        except Exception as exc:
            log_json(
                logger,
                logging.ERROR,
                "search_apis_engine_error",
                request_id=request_id,
                error=str(exc),
            )
            result = self.discovery_engine.build_fallback_response(
                query=search_params.query,
                category=search_params.category,
                primary_tools=primary_tools,
            )
        return self._success(request_id, result)

    def _success(self, request_id: str | int | None, result: Any) -> dict[str, Any]:
        return JsonRpcResponse(id=request_id, result=result).model_dump(exclude_none=True)

    def _tool_error_result(
        self, request_id: str | int | None, error: GatewayError
    ) -> dict[str, Any]:
        """Return a tool-execution failure as a normal result with ``isError``
        instead of a JSON-RPC error, so that MCP clients can distinguish
        protocol errors from tool failures."""
        parts = [f"[{error.category}] {error.message}"]
        for key in ("downstream_status", "elapsed_ms", "downstream_message", "detail"):
            value = error.data.get(key)
            if value is not None:
                parts.append(f"{key}: {value}")
        return self._success(request_id, {
            "content": [{"type": "text", "text": "\n".join(parts)}],
            "isError": True,
        })

    def _error(self, request_id: str | int | None, error: GatewayError) -> dict[str, Any]:
        return JsonRpcResponse(
            id=request_id,
            error=JsonRpcError(
                code=error.jsonrpc_code,
                message=error.message,
                data={"category": error.category, **error.data},
            ),
        ).model_dump(exclude_none=True)


def build_invalid_request_response(
    request_id: str | int | None,
    message: str,
    detail: dict[str, Any],
) -> dict[str, Any]:
    return JsonRpcResponse(
        id=request_id,
        error=JsonRpcError(
            code=JSONRPC_INVALID_PARAMS,
            message=message,
            data=detail,
        ),
    ).model_dump(exclude_none=True)


def build_request_shape_error_response(
    request_id: str | int | None,
    message: str,
    detail: dict[str, Any],
) -> dict[str, Any]:
    return JsonRpcResponse(
        id=request_id,
        error=JsonRpcError(
            code=JSONRPC_INVALID_REQUEST,
            message=message,
            data={"category": "INVALID_REQUEST", **detail},
        ),
    ).model_dump(exclude_none=True)


def build_parse_error_response(message: str) -> dict[str, Any]:
    return JsonRpcResponse(
        id=None,
        error=JsonRpcError(
            code=JSONRPC_PARSE_ERROR,
            message=message,
            data={"category": "PARSE_ERROR"},
        ),
    ).model_dump(exclude_none=True)


def build_gateway_error_response(
    request_id: str | int | None,
    error: GatewayError,
) -> dict[str, Any]:
    return JsonRpcResponse(
        id=request_id,
        error=JsonRpcError(
            code=error.jsonrpc_code,
            message=error.message,
            data={"category": error.category, **error.data},
        ),
    ).model_dump(exclude_none=True)
