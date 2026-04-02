from __future__ import annotations

from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings
from app.core.rest_connector import RestConnector
from scripts.mock_rest_api import create_mock_app


def build_client(tmp_path: Path) -> TestClient:
    mock_app = create_mock_app()
    mock_http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app),
        base_url="http://127.0.0.1:9001",
    )
    settings = Settings(
        api_key="dev-api-key",
        admin_api_key="dev-admin-key",
        config_dir=Path("configs/tools"),
        openapi_dir=Path("configs/openapi"),
        sqlite_path=tmp_path / "gateway.db",
        rate_limit_max_requests=100,
        session_ttl_seconds=300,
    )
    app = create_app(settings, connector=RestConnector(client=mock_http_client))
    return TestClient(app)


def _do_initialize(client: TestClient) -> str:
    """Perform full MCP handshake (initialize + notifications/initialized), return session_id."""
    resp = client.post(
        "/mcp",
        headers={"x-api-key": "dev-api-key"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert resp.status_code == 200
    session_id = resp.headers["mcp-session-id"]
    client.post(
        "/mcp",
        headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    return session_id


def test_initialize_and_tools_list(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        resp = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["result"]["serverInfo"]["name"] == "mcp-smart-api-gateway"
        session_id = resp.headers["mcp-session-id"]

        client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

        response = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        tool_names = {tool["name"] for tool in response.json()["result"]["tools"]}
        assert {"get_user", "create_order"} <= tool_names


def test_tools_call_get_user_success(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        session_id = _do_initialize(client)
        response = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "get_user",
                    "arguments": {"user_id": 7, "verbose": True},
                },
            },
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["structuredContent"]["id"] == 7
        assert result["meta"]["status_code"] == 200


def test_tools_call_validation_error(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        session_id = _do_initialize(client)
        response = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "get_user", "arguments": {"verbose": True}},
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["error"]["code"] == -32602
        assert payload["error"]["data"]["message"]


def test_unknown_method_returns_method_not_found(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        session_id = _do_initialize(client)
        response = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={"jsonrpc": "2.0", "id": 5, "method": "tools/unknown", "params": {}},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["error"]["code"] == -32601
        assert payload["error"]["data"]["method"] == "tools/unknown"
        assert "tools/call" in payload["error"]["data"]["supported_methods"]


def test_invalid_json_body_returns_parse_error(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        response = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key"},
            content="{bad json",
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["error"]["code"] == -32700
        assert payload["error"]["data"]["category"] == "PARSE_ERROR"


def test_batch_request_returns_invalid_request(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        response = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key"},
            json=[{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}],
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["error"]["code"] == -32600
        assert payload["error"]["data"]["received_type"] == "list"


def test_invalid_api_key_returns_unauthorized_code(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        response = client.post(
            "/mcp",
            headers={"x-api-key": "wrong-key"},
            json={"jsonrpc": "2.0", "id": 6, "method": "initialize", "params": {}},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["error"]["code"] == -32001
        assert payload["error"]["message"] == "Invalid gateway API key."


def test_admin_reload_and_status(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        reload_response = client.post("/admin/reload", headers={"x-api-key": "dev-admin-key"})
        assert reload_response.status_code == 200
        reload_payload = reload_response.json()
        assert reload_payload["ok"] is True
        assert "changes" in reload_payload
        assert "gateway" in reload_payload

        status_response = client.get("/admin/status", headers={"x-api-key": "dev-admin-key"})
        assert status_response.status_code == 200
        status_payload = status_response.json()
        assert status_payload["gateway"]["status"] == "ready"
        assert status_payload["openapi_preview_count"] >= 2


def test_admin_tools_returns_management_view(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        response = client.get("/admin/tools", headers={"x-api-key": "dev-admin-key"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["summary"]["active_tool_count"] >= 2
        assert payload["summary"]["preview_tool_count"] >= 2
        assert any(tool["name"] == "get_user" for tool in payload["tools"])
        assert "required_fields" in payload["tools"][0]


def test_tools_call_create_order_success(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        session_id = _do_initialize(client)
        response = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "create_order",
                    "arguments": {
                        "item_name": "keyboard",
                        "amount": 99.5,
                        "customer_name": "Alice",
                        "request_source": "test",
                    },
                },
            },
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["structuredContent"]["order_id"] == "ORD-10001"
        assert result["structuredContent"]["status"] == "created"
        assert result["structuredContent"]["amount"] == 99.5
        assert result["structuredContent"]["customer"] == "Alice"
        assert result["meta"]["status_code"] == 200


def test_tools_call_unknown_tool_returns_error(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        session_id = _do_initialize(client)
        response = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {"name": "nonexistent_tool", "arguments": {}},
            },
        )
        payload = response.json()
        assert payload["error"]["code"] == -32601
        assert payload["error"]["data"]["category"] == "TOOL_NOT_FOUND"
        assert "nonexistent_tool" in payload["error"]["data"]["tool_name"]
        assert "available_tools" in payload["error"]["data"]


def test_tools_call_missing_params_name_returns_error(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        session_id = _do_initialize(client)
        response = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {"arguments": {}},
            },
        )
        payload = response.json()
        assert payload["error"] is not None
        assert payload["error"]["code"] in (-32602, -32603)


def test_missing_api_key_returns_unauthorized(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 13, "method": "initialize", "params": {}},
        )
        payload = response.json()
        assert payload["error"]["code"] == -32001


def test_auto_generated_request_id_header(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        session_id = _do_initialize(client)
        response = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={"jsonrpc": "2.0", "id": 14, "method": "tools/list", "params": {}},
        )
        assert response.headers.get("x-request-id")
        assert len(response.headers["x-request-id"]) > 0


def test_admin_rollback_after_reload(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        client.post("/admin/reload", headers={"x-api-key": "dev-admin-key"})

        response = client.post("/admin/rollback", headers={"x-api-key": "dev-admin-key"})
        payload = response.json()

        assert response.status_code == 200
        assert payload["ok"] is True
        assert payload["status"] == "ok"
        assert "rolled_back_from" in payload
        assert "changes" in payload


def test_admin_rollback_without_previous_returns_409(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        response = client.post("/admin/rollback", headers={"x-api-key": "dev-admin-key"})

        assert response.status_code == 409
        assert response.json()["ok"] is False
        assert "No previous snapshot" in response.json()["error"]


def test_admin_rollback_requires_admin_key(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        response = client.post("/admin/rollback", headers={"x-api-key": "dev-api-key"})
        assert response.status_code == 401


def test_health_endpoint_returns_status(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        response = client.get("/health")
        payload = response.json()

        assert response.status_code == 200
        assert payload["status"] == "ok"
        assert payload["gateway"]["status"] == "ready"
        assert payload["gateway"]["tool_count"] >= 2
