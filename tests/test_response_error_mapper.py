from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.core.adaptation_engine import AdaptationEngine
from app.core.error_mapper import from_http_error, from_request_error
from app.core.errors import GatewayError
from app.core.response_mapper import build_tool_result
from app.core.rest_connector import RestConnector, RestResponse
from app.db.database import Database
from app.db.repositories import AccessLogRepository
from app.models.tool_config import ErrorMapping, ResponseMapping, ToolConfig


def test_build_tool_result_includes_http_meta_and_template_text() -> None:
    payload = {"data": {"id": 7, "name": "User 7", "email": "user7@example.com", "role": "tester"}}
    response = RestResponse(
        status_code=200,
        headers={},
        body=payload,
        text='{"data": {"id": 7}}',
        elapsed_ms=12,
        attempts_made=2,
    )
    mapping = ResponseMapping(
        result_path="data",
        field_whitelist=["id", "name", "email", "role"],
        include_http_meta=True,
        text_template="用户 {name} 的邮箱是 {email}。",
    )

    result = build_tool_result(payload, response, mapping)

    assert result["structuredContent"]["id"] == 7
    assert result["content"][0]["text"] == "用户 User 7 的邮箱是 user7@example.com。"
    assert result["meta"]["attempts_made"] == 2


def test_build_tool_result_raises_downstream_error_when_path_missing() -> None:
    response = RestResponse(
        status_code=200,
        headers={},
        body={"data": {"id": 1}},
        text='{"data": {"id": 1}}',
        elapsed_ms=5,
    )
    mapping = ResponseMapping(result_path="data.profile")

    with pytest.raises(GatewayError) as exc_info:
        build_tool_result({"data": {"id": 1}}, response, mapping)

    assert exc_info.value.category == "DOWNSTREAM_ERROR"
    assert exc_info.value.data["result_path"] == "data.profile"


def test_build_tool_result_raises_internal_error_when_template_field_missing() -> None:
    response = RestResponse(
        status_code=200,
        headers={},
        body={"data": {"id": 1, "name": "User 1"}},
        text='{"data": {"id": 1, "name": "User 1"}}',
        elapsed_ms=5,
    )
    mapping = ResponseMapping(result_path="data", text_template="邮箱 {email}")

    with pytest.raises(GatewayError) as exc_info:
        build_tool_result({"data": {"id": 1, "name": "User 1"}}, response, mapping)

    assert exc_info.value.category == "INTERNAL_ERROR"
    assert exc_info.value.data["missing_field"] == "email"


def test_from_http_error_extracts_downstream_message() -> None:
    error = from_http_error(
        status_code=500,
        response_text='{"detail":"Mock failure"}',
        elapsed_ms=21,
        error_mapping=ErrorMapping(expose_downstream_body=True),
    )

    assert error.category == "DOWNSTREAM_ERROR"
    assert error.message == "Downstream REST service returned HTTP 500."
    assert error.data["downstream_message"] == "Mock failure"
    assert "downstream_body" in error.data


def test_from_request_error_maps_connection_failures() -> None:
    request = httpx.Request("GET", "http://127.0.0.1:9999/fail")
    error = from_request_error(httpx.ConnectError("connect failed", request=request))

    assert error.category == "DOWNSTREAM_ERROR"
    assert error.data["request_url"] == "http://127.0.0.1:9999/fail"


@pytest.mark.asyncio
async def test_adaptation_engine_normalizes_request_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connect failed", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    database = Database(tmp_path / "gateway.db")
    await database.initialize()
    access_logs = AccessLogRepository(database)
    engine = AdaptationEngine(RestConnector(client=client), access_logs)
    tool = ToolConfig.model_validate(
        {
            "tool_meta": {
                "name": "connect_fail",
                "title": "Connect fail",
                "description": "Simulate downstream connection failure.",
            },
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "http_target": {
                "method": "GET",
                "base_url": "http://127.0.0.1:9999",
                "path": "/fail",
            },
        }
    )

    with pytest.raises(GatewayError) as exc_info:
        await engine.execute_tool(tool, {})

    assert exc_info.value.category == "DOWNSTREAM_ERROR"
    assert exc_info.value.data["request_url"] == "http://127.0.0.1:9999/fail"


def test_build_tool_result_whitelist_without_template_outputs_json() -> None:
    payload = {"data": {"id": 1, "name": "Alice", "secret": "should-be-excluded"}}
    response = RestResponse(
        status_code=200, headers={}, body=payload, text="", elapsed_ms=5
    )
    mapping = ResponseMapping(result_path="data", field_whitelist=["id", "name"])

    result = build_tool_result(payload, response, mapping)

    assert result["structuredContent"] == {"id": 1, "name": "Alice"}
    assert '"id": 1' in result["content"][0]["text"]
    assert "secret" not in result["content"][0]["text"]


def test_build_tool_result_without_result_path_uses_full_payload() -> None:
    payload = {"status": "ok", "count": 3}
    response = RestResponse(
        status_code=200, headers={}, body=payload, text="", elapsed_ms=5
    )
    mapping = ResponseMapping()

    result = build_tool_result(payload, response, mapping)

    assert result["structuredContent"]["status"] == "ok"
    assert result["structuredContent"]["count"] == 3


def test_build_tool_result_without_http_meta() -> None:
    payload = {"data": {"ok": True}}
    response = RestResponse(
        status_code=200, headers={}, body=payload, text="", elapsed_ms=5
    )
    mapping = ResponseMapping(result_path="data", include_http_meta=False)

    result = build_tool_result(payload, response, mapping)

    assert "meta" not in result


def test_from_http_error_hides_body_when_expose_is_false() -> None:
    error = from_http_error(
        status_code=404,
        response_text='{"detail":"Not found"}',
        elapsed_ms=10,
        error_mapping=ErrorMapping(expose_downstream_body=False),
    )

    assert error.data["downstream_status"] == 404
    assert error.data["downstream_message"] == "Not found"
    assert "downstream_body" not in error.data


def test_from_http_error_uses_custom_status_map() -> None:
    error = from_http_error(
        status_code=429,
        response_text="Rate limited by upstream",
        elapsed_ms=3,
        error_mapping=ErrorMapping(status_map={"429": "RATE_LIMITED"}),
    )

    assert error.category == "RATE_LIMITED"


def test_from_http_error_uses_default_code_for_unmapped_status() -> None:
    error = from_http_error(
        status_code=418,
        response_text="I'm a teapot",
        elapsed_ms=1,
        error_mapping=ErrorMapping(default_code="DOWNSTREAM_ERROR"),
    )

    assert error.category == "DOWNSTREAM_ERROR"
    assert error.data["downstream_status"] == 418


def test_from_validation_error_includes_path_and_message() -> None:
    from app.core.error_mapper import from_validation_error
    from jsonschema import ValidationError as JsonSchemaValidationError

    exc = JsonSchemaValidationError(
        "'abc' is not of type 'integer'",
        validator="type",
        validator_value="integer",
        path=["user_id"],
    )
    error = from_validation_error(exc)

    assert error.category == "VALIDATION_ERROR"
    assert error.data["validator"] == "type"
    assert error.data["path"] == ["user_id"]
    assert "integer" in error.data["message"]
