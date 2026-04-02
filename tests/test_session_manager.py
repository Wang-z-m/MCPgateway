from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.session_manager import SessionManager
from app.main import create_app
from app.settings import Settings
from app.core.rest_connector import RestConnector
from scripts.mock_rest_api import create_mock_app


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

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
    """Perform full MCP handshake, return session_id."""
    resp = client.post(
        "/mcp",
        headers={"x-api-key": "dev-api-key"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert resp.status_code == 200
    session_id = resp.headers["mcp-session-id"]
    assert session_id

    notif_resp = client.post(
        "/mcp",
        headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert notif_resp.status_code == 202
    return session_id


# ---------------------------------------------------------------------------
# Unit tests: SessionManager
# ---------------------------------------------------------------------------


async def test_create_and_get_session() -> None:
    mgr = SessionManager(ttl_seconds=60)
    session = await mgr.create_session(client_info={"name": "test"})
    assert session.session_id
    assert not session.initialized

    found = await mgr.get_session(session.session_id)
    assert found is not None
    assert found.session_id == session.session_id


async def test_get_session_returns_none_for_unknown() -> None:
    mgr = SessionManager(ttl_seconds=60)
    assert await mgr.get_session("nonexistent") is None


async def test_mark_initialized() -> None:
    mgr = SessionManager(ttl_seconds=60)
    session = await mgr.create_session()
    assert not session.initialized

    result = await mgr.mark_initialized(session.session_id)
    assert result is True

    updated = await mgr.get_session(session.session_id)
    assert updated is not None
    assert updated.initialized is True


async def test_mark_initialized_unknown_returns_false() -> None:
    mgr = SessionManager(ttl_seconds=60)
    assert await mgr.mark_initialized("nope") is False


async def test_terminate_session() -> None:
    mgr = SessionManager(ttl_seconds=60)
    session = await mgr.create_session()
    assert mgr.active_count() == 1

    assert await mgr.terminate_session(session.session_id) is True
    assert mgr.active_count() == 0
    assert await mgr.get_session(session.session_id) is None


async def test_terminate_unknown_returns_false() -> None:
    mgr = SessionManager(ttl_seconds=60)
    assert await mgr.terminate_session("unknown") is False


async def test_expired_session_is_removed_on_get() -> None:
    mgr = SessionManager(ttl_seconds=1)
    session = await mgr.create_session()
    session.last_active_at -= 2
    assert await mgr.get_session(session.session_id) is None


async def test_cleanup_expired() -> None:
    mgr = SessionManager(ttl_seconds=1)
    s1 = await mgr.create_session()
    s2 = await mgr.create_session()
    s1.last_active_at -= 2
    s2.last_active_at -= 2
    assert mgr.active_count() == 2

    removed = await mgr.cleanup_expired()
    assert removed == 2
    assert mgr.active_count() == 0


async def test_describe() -> None:
    mgr = SessionManager(ttl_seconds=120)
    await mgr.create_session()
    desc = mgr.describe()
    assert desc["active_sessions"] == 1
    assert desc["ttl_seconds"] == 120


# ---------------------------------------------------------------------------
# Integration tests: HTTP session lifecycle
# ---------------------------------------------------------------------------


def test_initialize_returns_session_id(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        resp = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            },
        )
        assert resp.status_code == 200
        assert "mcp-session-id" in resp.headers
        assert len(resp.headers["mcp-session-id"]) == 64


def test_notifications_initialized_returns_202(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        init_resp = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        session_id = init_resp.headers["mcp-session-id"]

        resp = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert resp.status_code == 202


def test_tools_list_without_session_returns_400(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        resp = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert resp.status_code == 400


def test_tools_list_with_invalid_session_returns_404(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        resp = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": "bad-session-id"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert resp.status_code == 404


def test_tools_call_before_initialized_returns_400(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        init_resp = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        session_id = init_resp.headers["mcp-session-id"]

        resp = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_user", "arguments": {"user_id": 1}}},
        )
        assert resp.status_code == 400
        assert "not yet initialized" in resp.json()["error"].lower()


def test_full_session_lifecycle(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        session_id = _do_initialize(client)

        resp = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert resp.status_code == 200
        tool_names = {t["name"] for t in resp.json()["result"]["tools"]}
        assert "get_user" in tool_names

        resp = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_user", "arguments": {"user_id": 7}},
            },
        )
        assert resp.status_code == 200
        assert resp.json()["result"]["structuredContent"]["id"] == 7


def test_delete_mcp_terminates_session(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        session_id = _do_initialize(client)

        del_resp = client.delete(
            "/mcp",
            headers={"mcp-session-id": session_id},
        )
        assert del_resp.status_code == 204

        resp = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
        )
        assert resp.status_code == 404


def test_delete_mcp_without_session_returns_400(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        resp = client.delete("/mcp")
        assert resp.status_code == 400


def test_delete_mcp_unknown_session_returns_404(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        resp = client.delete("/mcp", headers={"mcp-session-id": "nonexistent"})
        assert resp.status_code == 404


def test_admin_status_includes_sessions(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        _do_initialize(client)

        resp = client.get("/admin/status", headers={"x-api-key": "dev-admin-key"})
        assert resp.status_code == 200
        payload = resp.json()
        assert "sessions" in payload
        assert payload["sessions"]["active_sessions"] >= 1
        assert payload["sessions"]["ttl_seconds"] == 300
