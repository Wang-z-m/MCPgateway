from __future__ import annotations

import shutil
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.core.rest_connector import RestConnector
from app.main import create_app
from app.settings import Settings
from scripts.mock_rest_api import create_mock_app


def build_runtime_client(tmp_path: Path, *, rate_limit_max_requests: int = 3) -> TestClient:
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
        sqlite_path=tmp_path / "runtime.db",
        rate_limit_window_seconds=60,
        rate_limit_max_requests=rate_limit_max_requests,
        session_ttl_seconds=300,
    )
    app = create_app(settings, connector=RestConnector(client=mock_http_client))
    return TestClient(app)


def _do_initialize(client: TestClient) -> str:
    """Perform MCP handshake, return session_id."""
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


def test_downstream_error_is_normalized(tmp_path: Path) -> None:
    with build_runtime_client(tmp_path, rate_limit_max_requests=10) as client:
        session_id = _do_initialize(client)
        response = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": "probe_failure", "arguments": {}},
            },
        )
        payload = response.json()
        assert payload["error"]["code"] == -32050
        assert payload["error"]["data"]["downstream_status"] == 500


def test_rate_limit_is_enforced(tmp_path: Path) -> None:
    # Budget: 3 requests. initialize(1) + notifications/initialized(2) + tools/list(3)
    # The 4th request should be rate-limited.
    with build_runtime_client(tmp_path, rate_limit_max_requests=3) as client:
        session_id = _do_initialize(client)
        first = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={"jsonrpc": "2.0", "id": 11, "method": "tools/list", "params": {}},
        )
        second = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={"jsonrpc": "2.0", "id": 12, "method": "tools/list", "params": {}},
        )
        assert first.status_code == 200
        assert first.headers["x-rate-limit-limit"] == "3"
        assert second.json()["error"]["data"]["category"] == "RATE_LIMITED"
        assert second.headers["x-rate-limit-limit"] == "3"
        assert second.headers["x-rate-limit-remaining"] == "0"
        assert int(second.headers["x-rate-limit-reset"]) >= 1


def test_admin_requires_admin_key(tmp_path: Path) -> None:
    with build_runtime_client(tmp_path, rate_limit_max_requests=10) as client:
        response = client.get("/admin/status", headers={"x-api-key": "dev-api-key"})
        assert response.status_code == 401


def test_admin_status_includes_recent_calls_and_audit_summary(tmp_path: Path) -> None:
    with build_runtime_client(tmp_path, rate_limit_max_requests=10) as client:
        session_id = _do_initialize(client)
        client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 20,
                "method": "tools/call",
                "params": {"name": "get_user", "arguments": {"user_id": 1, "verbose": True}},
            },
        )

        response = client.get("/admin/status", headers={"x-api-key": "dev-admin-key"})
        payload = response.json()

        assert response.status_code == 200
        assert payload["audit_summary"]["total_events"] >= 1
        assert payload["security"]["auth"]["gateway_key_enabled"] is True
        assert payload["security"]["rate_limit"]["max_requests"] == 10
        assert payload["metrics"]["total_calls"] >= 1
        assert payload["recent_calls"][0]["tool_name"] == "get_user"
        assert payload["recent_calls"][0]["attempts_made"] >= 1


def test_admin_reload_reports_added_tool(tmp_path: Path) -> None:
    config_dir = tmp_path / "tools"
    shutil.copytree(Path("configs/tools"), config_dir)

    mock_app = create_mock_app()
    mock_http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app),
        base_url="http://127.0.0.1:9001",
    )
    settings = Settings(
        api_key="dev-api-key",
        admin_api_key="dev-admin-key",
        config_dir=config_dir,
        openapi_dir=Path("configs/openapi"),
        sqlite_path=tmp_path / "runtime.db",
        rate_limit_window_seconds=60,
        rate_limit_max_requests=10,
        session_ttl_seconds=300,
    )
    app = create_app(settings, connector=RestConnector(client=mock_http_client))

    with TestClient(app) as client:
        extra_tool = """
tool_meta:
  name: probe_added
  title: Probe added tool
  description: Added during reload test.
input_schema:
  type: object
  properties: {}
  additionalProperties: false
http_target:
  method: GET
  base_url: http://127.0.0.1:9001
  path: /slow
request_mapping: {}
response_mapping:
  result_path: data
error_mapping: {}
""".strip()
        (config_dir / "probe_added.yaml").write_text(extra_tool, encoding="utf-8")

        response = client.post("/admin/reload", headers={"x-api-key": "dev-admin-key"})
        payload = response.json()

        assert response.status_code == 200
        assert payload["changes"]["added"] == ["probe_added"]
        assert payload["gateway"]["tool_count"] >= 1


def test_request_id_header_is_returned(tmp_path: Path) -> None:
    with build_runtime_client(tmp_path, rate_limit_max_requests=10) as client:
        session_id = _do_initialize(client)
        response = client.post(
            "/mcp",
            headers={
                "x-api-key": "dev-api-key",
                "x-request-id": "req-test-123",
                "mcp-session-id": session_id,
            },
            json={"jsonrpc": "2.0", "id": 30, "method": "tools/list", "params": {}},
        )

        assert response.status_code == 200
        assert response.headers["x-request-id"] == "req-test-123"


def test_retry_probe_reports_second_attempt(tmp_path: Path) -> None:
    with build_runtime_client(tmp_path, rate_limit_max_requests=10) as client:
        session_id = _do_initialize(client)
        response = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 31,
                "method": "tools/call",
                "params": {"name": "probe_retry", "arguments": {}},
            },
        )
        payload = response.json()

        assert response.status_code == 200
        assert payload["result"]["structuredContent"]["status"] == "retry-ok"
        assert payload["result"]["meta"]["attempts_made"] == 2


def test_admin_rollback_restores_previous_config(tmp_path: Path) -> None:
    config_dir = tmp_path / "tools"
    shutil.copytree(Path("configs/tools"), config_dir)

    mock_app = create_mock_app()
    mock_http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app),
        base_url="http://127.0.0.1:9001",
    )
    settings = Settings(
        api_key="dev-api-key",
        admin_api_key="dev-admin-key",
        config_dir=config_dir,
        openapi_dir=Path("configs/openapi"),
        sqlite_path=tmp_path / "runtime.db",
        rate_limit_window_seconds=60,
        rate_limit_max_requests=100,
        session_ttl_seconds=300,
    )
    app = create_app(settings, connector=RestConnector(client=mock_http_client))

    with TestClient(app) as client:
        admin_headers = {"x-api-key": "dev-admin-key"}

        status_before = client.get("/admin/status", headers=admin_headers).json()
        v1_count = status_before["gateway"]["tool_count"]

        extra_tool = """
tool_meta:
  name: temp_rollback_tool
  title: Temp
  description: Temp tool for rollback test.
input_schema:
  type: object
  properties: {}
  additionalProperties: false
http_target:
  method: GET
  base_url: http://127.0.0.1:9001
  path: /slow
response_mapping:
  result_path: data
""".strip()
        (config_dir / "temp_rollback_tool.yaml").write_text(extra_tool, encoding="utf-8")
        reload_resp = client.post("/admin/reload", headers=admin_headers)
        assert reload_resp.json()["changes"]["added"] == ["temp_rollback_tool"]
        v2_count = reload_resp.json()["gateway"]["tool_count"]
        assert v2_count == v1_count + 1

        rollback_resp = client.post("/admin/rollback", headers=admin_headers)
        rollback_payload = rollback_resp.json()
        assert rollback_resp.status_code == 200
        assert rollback_payload["ok"] is True
        assert rollback_payload["gateway"]["tool_count"] == v1_count
        assert rollback_payload["changes"]["removed"] == ["temp_rollback_tool"]


def test_reload_detects_removed_tool(tmp_path: Path) -> None:
    config_dir = tmp_path / "tools"
    shutil.copytree(Path("configs/tools"), config_dir)

    mock_app = create_mock_app()
    mock_http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app),
        base_url="http://127.0.0.1:9001",
    )
    settings = Settings(
        api_key="dev-api-key",
        admin_api_key="dev-admin-key",
        config_dir=config_dir,
        openapi_dir=Path("configs/openapi"),
        sqlite_path=tmp_path / "runtime.db",
        rate_limit_window_seconds=60,
        rate_limit_max_requests=100,
        session_ttl_seconds=300,
    )
    app = create_app(settings, connector=RestConnector(client=mock_http_client))

    with TestClient(app) as client:
        admin_headers = {"x-api-key": "dev-admin-key"}

        probe_slow_path = config_dir / "probe_slow.yaml"
        assert probe_slow_path.exists()
        probe_slow_path.unlink()

        reload_resp = client.post("/admin/reload", headers=admin_headers)
        payload = reload_resp.json()

        assert reload_resp.status_code == 200
        assert "probe_slow" in payload["changes"]["removed"]


def test_admin_status_audit_records_rollback_event(tmp_path: Path) -> None:
    with build_runtime_client(tmp_path, rate_limit_max_requests=100) as client:
        admin_headers = {"x-api-key": "dev-admin-key"}

        client.post("/admin/reload", headers=admin_headers)
        client.post("/admin/rollback", headers=admin_headers)

        status = client.get("/admin/status", headers=admin_headers).json()
        audit_statuses = [e["status"] for e in status["recent_audit"]]
        assert "rolled_back" in audit_statuses
