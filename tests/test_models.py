from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.jsonrpc import JsonRpcRequest, JsonRpcResponse, ToolCallParams
from app.models.tool_config import HttpTarget, ToolConfig


def test_jsonrpc_request_rejects_boolean_id() -> None:
    with pytest.raises(ValidationError):
        JsonRpcRequest.model_validate(
            {
                "jsonrpc": "2.0",
                "id": True,
                "method": "tools/list",
                "params": {},
            }
        )


def test_jsonrpc_request_valid_with_string_id() -> None:
    req = JsonRpcRequest.model_validate(
        {"jsonrpc": "2.0", "id": "abc-123", "method": "tools/list", "params": {}}
    )
    assert req.id == "abc-123"
    assert req.method == "tools/list"


def test_jsonrpc_request_valid_with_integer_id() -> None:
    req = JsonRpcRequest.model_validate(
        {"jsonrpc": "2.0", "id": 42, "method": "initialize"}
    )
    assert req.id == 42
    assert req.params is None


def test_jsonrpc_request_rejects_non_2_0_version() -> None:
    with pytest.raises(ValidationError, match="2.0"):
        JsonRpcRequest.model_validate(
            {"jsonrpc": "1.0", "id": 1, "method": "tools/list"}
        )


def test_jsonrpc_request_rejects_empty_method() -> None:
    with pytest.raises(ValidationError):
        JsonRpcRequest.model_validate(
            {"jsonrpc": "2.0", "id": 1, "method": "   "}
        )


def test_jsonrpc_response_rejects_result_and_error_together() -> None:
    with pytest.raises(ValidationError):
        JsonRpcResponse.model_validate(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"ok": True},
                "error": {"code": -32603, "message": "failed"},
            }
        )


def test_jsonrpc_response_valid_with_result_only() -> None:
    resp = JsonRpcResponse.model_validate(
        {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
    )
    assert resp.result == {"tools": []}
    assert resp.error is None


def test_jsonrpc_response_valid_with_error_only() -> None:
    resp = JsonRpcResponse.model_validate(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "Not found"}}
    )
    assert resp.error.code == -32601
    assert resp.result is None


def test_tool_call_params_rejects_empty_name() -> None:
    with pytest.raises(ValidationError):
        ToolCallParams.model_validate({"name": "", "arguments": {}})


def test_tool_config_rejects_required_property_not_in_schema() -> None:
    with pytest.raises(ValidationError):
        ToolConfig.model_validate(
            {
                "tool_meta": {
                    "name": "broken_tool",
                    "title": "Broken tool",
                    "description": "Invalid required field reference.",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {"user_id": {"type": "integer"}},
                    "required": ["missing_field"],
                },
                "http_target": {
                    "method": "GET",
                    "base_url": "http://127.0.0.1:9001",
                    "path": "/users/{user_id}",
                },
            }
        )


def test_tool_config_rejects_non_object_input_schema() -> None:
    with pytest.raises(ValidationError, match="object"):
        ToolConfig.model_validate(
            {
                "tool_meta": {"name": "bad", "title": "Bad", "description": "Bad schema type."},
                "input_schema": {"type": "array", "items": {"type": "string"}},
                "http_target": {"method": "GET", "base_url": "http://localhost", "path": "/x"},
            }
        )


def test_tool_config_infers_idempotency_for_get() -> None:
    config = ToolConfig.model_validate(
        {
            "tool_meta": {
                "name": "get_user",
                "title": "Get user",
                "description": "Fetch a user.",
            },
            "input_schema": {
                "type": "object",
                "properties": {"user_id": {"type": "integer"}},
                "required": ["user_id"],
            },
            "http_target": {
                "method": "GET",
                "base_url": "http://127.0.0.1:9001",
                "path": "/users/{user_id}",
            },
        }
    )

    assert config.http_target.idempotent is True


def test_tool_config_infers_non_idempotent_for_post() -> None:
    config = ToolConfig.model_validate(
        {
            "tool_meta": {"name": "create", "title": "Create", "description": "Create resource."},
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "http_target": {"method": "POST", "base_url": "http://localhost", "path": "/items"},
        }
    )
    assert config.http_target.idempotent is False


def test_tool_config_explicit_idempotent_overrides_inference() -> None:
    config = ToolConfig.model_validate(
        {
            "tool_meta": {"name": "safe_post", "title": "Safe", "description": "Idempotent POST."},
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "http_target": {
                "method": "POST",
                "base_url": "http://localhost",
                "path": "/items",
                "idempotent": True,
            },
        }
    )
    assert config.http_target.idempotent is True


def test_tool_config_mcp_tool_schema_structure() -> None:
    config = ToolConfig.model_validate(
        {
            "tool_meta": {
                "name": "my_tool",
                "title": "My Tool",
                "description": "A test tool.",
                "version": "2.0.0",
                "tags": ["test"],
            },
            "input_schema": {
                "type": "object",
                "properties": {"x": {"type": "integer"}},
                "required": ["x"],
            },
            "http_target": {"method": "GET", "base_url": "http://localhost", "path": "/x"},
        }
    )
    schema = config.mcp_tool_schema()
    assert schema["name"] == "my_tool"
    assert schema["title"] == "My Tool"
    assert schema["description"] == "A test tool."
    assert schema["inputSchema"]["properties"]["x"]["type"] == "integer"
    assert schema["annotations"]["version"] == "2.0.0"
    assert schema["annotations"]["tags"] == ["test"]


def test_http_target_rejects_negative_timeout() -> None:
    with pytest.raises(ValidationError, match="timeout"):
        HttpTarget(method="GET", base_url="http://localhost", path="/x", timeout_seconds=-1)


def test_http_target_rejects_negative_retry_count() -> None:
    with pytest.raises(ValidationError, match="retry_count"):
        HttpTarget(method="GET", base_url="http://localhost", path="/x", retry_count=-1)


def test_http_target_rejects_path_without_leading_slash() -> None:
    with pytest.raises(ValidationError, match="start with"):
        HttpTarget(method="GET", base_url="http://localhost", path="users")


def test_tool_meta_category_defaults_to_none() -> None:
    from app.models.tool_config import ToolMeta
    meta = ToolMeta(name="test", title="Test", description="Test tool.")
    assert meta.category is None


def test_tool_meta_tier_defaults_to_primary() -> None:
    from app.models.tool_config import ToolMeta
    meta = ToolMeta(name="test", title="Test", description="Test tool.")
    assert meta.tier == "primary"


def test_tool_meta_explicit_category_and_tier() -> None:
    from app.models.tool_config import ToolMeta
    meta = ToolMeta(
        name="test", title="Test", description="Test tool.",
        category="finance", tier="secondary",
    )
    assert meta.category == "finance"
    assert meta.tier == "secondary"
