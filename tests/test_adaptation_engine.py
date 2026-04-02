from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.adaptation_engine import AdaptationEngine
from app.core.errors import GatewayError
from app.core.rest_connector import PreparedRestRequest, RestConnector
from app.db.database import Database
from app.db.repositories import AccessLogRepository
from app.models.tool_config import ToolConfig


@pytest.mark.asyncio
async def test_rest_connector_retries_idempotent_request_once() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.ConnectTimeout("temporary timeout", request=request)
        return httpx.Response(200, json={"data": {"ok": True}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = RestConnector(client=client)

    response = await connector.send(
        PreparedRestRequest(
            method="GET",
            url="http://testserver/retry",
            retry_count=1,
            idempotent=True,
        )
    )

    assert attempts["count"] == 2
    assert response.status_code == 200
    assert response.attempts_made == 2


@pytest.mark.asyncio
async def test_rest_connector_does_not_retry_non_idempotent_request() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        raise httpx.ConnectError("downstream unavailable", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = RestConnector(client=client)

    with pytest.raises(httpx.ConnectError):
        await connector.send(
            PreparedRestRequest(
                method="POST",
                url="http://testserver/orders",
                retry_count=3,
                idempotent=False,
                json_body={"name": "demo"},
            )
        )

    assert attempts["count"] == 1


@pytest.mark.asyncio
async def test_adaptation_engine_reports_unresolved_path_template(tmp_path: Path) -> None:
    database = Database(tmp_path / "gateway.db")
    await database.initialize()
    access_logs = AccessLogRepository(database)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    )
    engine = AdaptationEngine(RestConnector(client=client), access_logs)
    tool = ToolConfig.model_validate(
        {
            "tool_meta": {
                "name": "broken_path_tool",
                "title": "Broken path tool",
                "description": "Has an unresolved path template variable.",
            },
            "input_schema": {
                "type": "object",
                "properties": {"user_id": {"type": "integer"}},
                "required": ["user_id"],
            },
            "http_target": {
                "method": "GET",
                "base_url": "http://127.0.0.1:9001",
                "path": "/users/{missing_id}",
            },
            "request_mapping": {
                "path_map": {"user_id": "user_id"},
            },
        }
    )

    with pytest.raises(GatewayError) as exc_info:
        await engine.execute_tool(tool, {"user_id": 1})

    assert exc_info.value.category == "INTERNAL_ERROR"
    assert exc_info.value.data["unresolved_variables"] == ["missing_id"]


@pytest.mark.asyncio
async def test_rest_connector_no_retry_when_retry_count_is_zero() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        raise httpx.ConnectTimeout("timeout", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = RestConnector(client=client)

    with pytest.raises(httpx.ConnectTimeout):
        await connector.send(
            PreparedRestRequest(
                method="GET",
                url="http://testserver/no-retry",
                retry_count=0,
                idempotent=True,
            )
        )
    assert attempts["count"] == 1


@pytest.mark.asyncio
async def test_rest_connector_retries_on_5xx_for_idempotent() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json={"data": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = RestConnector(client=client)

    response = await connector.send(
        PreparedRestRequest(
            method="GET",
            url="http://testserver/flaky",
            retry_count=1,
            idempotent=True,
        )
    )
    assert attempts["count"] == 2
    assert response.status_code == 200


def _make_tool(
    name: str = "test_tool",
    method: str = "GET",
    path: str = "/test",
    *,
    path_map: dict[str, str] | None = None,
    query_map: dict[str, str] | None = None,
    header_map: dict[str, str] | None = None,
    body_map: dict[str, str] | None = None,
    constant_headers: dict[str, str] | None = None,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> ToolConfig:
    return ToolConfig.model_validate(
        {
            "tool_meta": {"name": name, "title": name, "description": "Test tool."},
            "input_schema": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
            "http_target": {
                "method": method,
                "base_url": "http://testserver",
                "path": path,
            },
            "request_mapping": {
                "path_map": path_map or {},
                "query_map": query_map or {},
                "header_map": header_map or {},
                "body_map": body_map or {},
                "constant_headers": constant_headers or {},
            },
            "response_mapping": {"result_path": "data"},
            "error_mapping": {},
        }
    )


@pytest.mark.asyncio
async def test_adaptation_engine_maps_query_and_header_params(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"data": {"ok": True}})

    database = Database(tmp_path / "gateway.db")
    await database.initialize()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    engine = AdaptationEngine(RestConnector(client=client), AccessLogRepository(database))

    tool = _make_tool(
        properties={"q": {"type": "string"}, "token": {"type": "string"}},
        query_map={"q": "search"},
        header_map={"token": "X-Token"},
        constant_headers={"Accept": "application/json"},
    )
    await engine.execute_tool(tool, {"q": "hello", "token": "secret"})

    assert "search=hello" in captured["url"]
    assert captured["headers"]["x-token"] == "secret"
    assert captured["headers"]["accept"] == "application/json"


@pytest.mark.asyncio
async def test_adaptation_engine_maps_nested_body(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"data": {"created": True}})

    database = Database(tmp_path / "gateway.db")
    await database.initialize()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    engine = AdaptationEngine(RestConnector(client=client), AccessLogRepository(database))

    tool = _make_tool(
        method="POST",
        path="/orders",
        properties={
            "item_name": {"type": "string"},
            "amount": {"type": "number"},
        },
        required=["item_name", "amount"],
        body_map={"item_name": "item.name", "amount": "payment.amount"},
    )
    await engine.execute_tool(tool, {"item_name": "keyboard", "amount": 99.5})

    assert captured["body"]["item"]["name"] == "keyboard"
    assert captured["body"]["payment"]["amount"] == 99.5


@pytest.mark.asyncio
async def test_adaptation_engine_records_failure_on_validation_error(tmp_path: Path) -> None:
    database = Database(tmp_path / "gateway.db")
    await database.initialize()
    access_logs = AccessLogRepository(database)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {}}))
    )
    engine = AdaptationEngine(RestConnector(client=client), access_logs)

    tool = _make_tool(
        properties={"user_id": {"type": "integer"}},
        required=["user_id"],
    )

    with pytest.raises(GatewayError) as exc_info:
        await engine.execute_tool(tool, {"user_id": "not_a_number"})

    assert exc_info.value.category == "VALIDATION_ERROR"

    metrics = await access_logs.build_metrics_snapshot()
    assert metrics["failure_calls"] == 1


@pytest.mark.asyncio
async def test_adaptation_engine_missing_required_path_param(tmp_path: Path) -> None:
    database = Database(tmp_path / "gateway.db")
    await database.initialize()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {}}))
    )
    engine = AdaptationEngine(RestConnector(client=client), AccessLogRepository(database))

    tool = _make_tool(
        path="/users/{user_id}",
        properties={"user_id": {"type": "integer"}},
        path_map={"user_id": "user_id"},
    )

    with pytest.raises(GatewayError) as exc_info:
        await engine.execute_tool(tool, {})

    assert exc_info.value.category == "VALIDATION_ERROR"
