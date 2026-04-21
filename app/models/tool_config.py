from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ToolMeta(BaseModel):
    name: str = Field(min_length=1)
    title: str
    description: str
    version: str = "1.0.0"
    tags: list[str] = Field(default_factory=list)
    category: str | None = None
    tier: Literal["primary", "secondary", "utility"] = "primary"


class HttpTarget(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    base_url: str
    path: str
    timeout_seconds: float = 10.0
    retry_count: int = 1
    idempotent: bool | None = None

    @field_validator("base_url", "path")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("HTTP target fields cannot be empty.")
        return cleaned

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("HTTP target path must start with '/'.")
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def validate_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("timeout_seconds must be greater than 0.")
        return value

    @field_validator("retry_count")
    @classmethod
    def validate_retry_count(cls, value: int) -> int:
        if value < 0:
            raise ValueError("retry_count cannot be negative.")
        return value


class RequestMapping(BaseModel):
    path_map: dict[str, str] = Field(default_factory=dict)
    query_map: dict[str, str] = Field(default_factory=dict)
    header_map: dict[str, str] = Field(default_factory=dict)
    body_map: dict[str, str] = Field(default_factory=dict)
    constant_headers: dict[str, str] = Field(default_factory=dict)


class ResponseMapping(BaseModel):
    result_path: str | None = None
    field_whitelist: list[str] = Field(default_factory=list)
    include_http_meta: bool = False
    text_template: str | None = None


class ErrorMapping(BaseModel):
    status_map: dict[str, str] = Field(default_factory=dict)
    default_code: str = "DOWNSTREAM_ERROR"
    expose_downstream_body: bool = False


class ToolConfig(BaseModel):
    tool_meta: ToolMeta
    input_schema: dict[str, Any]
    http_target: HttpTarget
    request_mapping: RequestMapping = Field(default_factory=RequestMapping)
    response_mapping: ResponseMapping = Field(default_factory=ResponseMapping)
    error_mapping: ErrorMapping = Field(default_factory=ErrorMapping)
    source_file: str | None = None

    @field_validator("input_schema")
    @classmethod
    def validate_input_schema_shape(cls, value: dict[str, Any]) -> dict[str, Any]:
        schema_type = value.get("type")
        properties = value.get("properties")
        required = value.get("required", [])

        if schema_type != "object":
            raise ValueError("input_schema.type must be 'object'.")
        if not isinstance(properties, dict):
            raise ValueError("input_schema.properties must be an object.")
        if not isinstance(required, list):
            raise ValueError("input_schema.required must be a list.")
        unknown_required = [name for name in required if name not in properties]
        if unknown_required:
            raise ValueError(
                "input_schema.required contains undefined properties: "
                + ", ".join(unknown_required)
            )
        return value

    @model_validator(mode="after")
    def infer_idempotency(self) -> "ToolConfig":
        if self.http_target.idempotent is None:
            self.http_target.idempotent = self.http_target.method in {"GET", "DELETE"}
        return self

    def mcp_tool_schema(self) -> dict[str, Any]:
        return {
            "name": self.tool_meta.name,
            "title": self.tool_meta.title,
            "description": self.tool_meta.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "version": self.tool_meta.version,
                "tags": self.tool_meta.tags,
            },
        }
