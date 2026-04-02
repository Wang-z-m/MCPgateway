from __future__ import annotations

from typing import Any

from app.core.error_mapper import (
    JSONRPC_INVALID_REQUEST,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_PARSE_ERROR,
    gateway_error,
)
from app.core.errors import GatewayError
from app.core.tool_registry import ToolRegistry
from app.models.jsonrpc import InitializeParams, JsonRpcError, JsonRpcRequest, JsonRpcResponse, ToolCallParams
from app.core.adaptation_engine import AdaptationEngine


SUPPORTED_METHODS = ("initialize", "notifications/initialized", "tools/list", "tools/call")


class McpService:
    def __init__(
        self,
        registry: ToolRegistry,
        adaptation_engine: AdaptationEngine,
        *,
        server_name: str,
        server_version: str,
    ) -> None:
        self.registry = registry
        self.adaptation_engine = adaptation_engine
        self.server_name = server_name
        self.server_version = server_version

    async def handle(self, request: JsonRpcRequest) -> dict[str, Any]:
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
                return self._success(
                    request.id,
                    {"tools": [tool.mcp_tool_schema() for tool in self.registry.list_tools()]},
                )
            if request.method == "tools/call":
                params = ToolCallParams.model_validate(request.params or {})
                tool = self.registry.get_tool(params.name)
                result = await self.adaptation_engine.execute_tool(tool, params.arguments)
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

    def _success(self, request_id: str | int | None, result: Any) -> dict[str, Any]:
        return JsonRpcResponse(id=request_id, result=result).model_dump(exclude_none=True)

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
