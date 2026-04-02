from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, StrictInt, StrictStr, field_validator, model_validator


RpcId = StrictStr | StrictInt | None


class JsonRpcError(BaseModel):
    code: int
    message: str
    data: dict[str, Any] | None = None


class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: RpcId = None
    method: str
    params: dict[str, Any] | list[Any] | None = None

    @field_validator("jsonrpc")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if value != "2.0":
            raise ValueError("Only JSON-RPC 2.0 is supported.")
        return value

    @field_validator("id")
    @classmethod
    def validate_request_id(cls, value: RpcId) -> RpcId:
        if isinstance(value, bool):
            raise ValueError("JSON-RPC id cannot be a boolean.")
        return value

    @field_validator("method")
    @classmethod
    def validate_method(cls, value: str) -> str:
        method = value.strip()
        if not method:
            raise ValueError("JSON-RPC method cannot be empty.")
        return method


class JsonRpcResponse(BaseModel):
    jsonrpc: str = "2.0"
    id: RpcId = None
    result: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    error: JsonRpcError | None = None

    @model_validator(mode="after")
    def validate_payload_shape(self) -> "JsonRpcResponse":
        if self.result is not None and self.error is not None:
            raise ValueError("JSON-RPC response cannot include both result and error.")
        return self


class ToolCallParams(BaseModel):
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class InitializeParams(BaseModel):
    protocolVersion: str | None = None
    clientInfo: dict[str, Any] | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)
