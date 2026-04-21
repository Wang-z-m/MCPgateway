"""Tests for the intelligent tool discovery engine and search_apis integration."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.discovery_engine import ToolDiscoveryEngine, SEARCH_APIS_SCHEMA
from app.core.rest_connector import RestConnector
from app.main import create_app
from app.models.tool_config import ToolConfig, ToolMeta
from app.settings import Settings
from scripts.mock_rest_api import create_mock_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool(
    name: str,
    title: str,
    description: str,
    *,
    tags: list[str] | None = None,
    category: str | None = None,
    tier: str = "primary",
) -> ToolConfig:
    return ToolConfig.model_validate({
        "tool_meta": {
            "name": name,
            "title": title,
            "description": description,
            "tags": tags or [],
            "category": category,
            "tier": tier,
        },
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
        "http_target": {
            "method": "GET",
            "base_url": "http://localhost",
            "path": f"/{name}",
        },
    })


def _default_settings(**overrides) -> Settings:
    defaults = {
        "discovery_default_top_k": 5,
        "discovery_max_top_k": 20,
        "discovery_score_threshold": 0.01,
        "discovery_tfidf_max_features": 5000,
        "discovery_fallback_on_empty": True,
        "discovery_rate_limit_max": 10,
        "discovery_rate_limit_window": 60,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _sample_tools() -> list[ToolConfig]:
    return [
        _make_tool(
            "get_user", "查询用户信息",
            "根据用户ID查询用户的基本信息，包括姓名、邮箱、部门和角色。",
            tags=["用户", "查询"], category="user_management", tier="primary",
        ),
        _make_tool(
            "create_order", "创建订单",
            "在下游系统中创建一笔新订单，需提供商品名称、金额和客户姓名。",
            tags=["订单", "创建"], category="order", tier="primary",
        ),
        _make_tool(
            "update_user_email", "修改用户邮箱",
            "修改指定用户的邮箱地址。",
            tags=["用户", "修改"], category="user_management", tier="secondary",
        ),
        _make_tool(
            "get_order_status", "查询订单状态",
            "根据订单号查询订单当前状态和物流信息。",
            tags=["订单", "查询"], category="order", tier="secondary",
        ),
        _make_tool(
            "probe_slow", "慢响应探测",
            "触发一个响应缓慢但最终成功的下游接口，用于测试超时与延迟场景。",
            tags=["实验", "延迟"], category="system", tier="utility",
        ),
    ]


# ---------------------------------------------------------------------------
# Unit Tests: Index Construction
# ---------------------------------------------------------------------------

class TestIndexConstruction:
    def test_rebuild_index_with_all_tiers(self):
        engine = ToolDiscoveryEngine(_default_settings())
        tools = _sample_tools()
        engine.rebuild_index(tools)
        assert engine._indexed_tools is not None
        assert len(engine._indexed_tools) == 5

    def test_rebuild_index_with_empty_list(self):
        engine = ToolDiscoveryEngine(_default_settings())
        engine.rebuild_index([])
        assert engine._vectorizer is None
        assert engine._indexed_tools == []

    def test_rebuild_index_replaces_previous(self):
        engine = ToolDiscoveryEngine(_default_settings())
        tools = _sample_tools()
        engine.rebuild_index(tools)
        first_tools = engine._indexed_tools

        engine.rebuild_index(tools[:2])
        assert len(engine._indexed_tools) == 2
        assert engine._indexed_tools is not first_tools

    def test_last_index_built_at_is_set(self):
        engine = ToolDiscoveryEngine(_default_settings())
        assert engine._last_index_built_at is None
        engine.rebuild_index(_sample_tools())
        assert engine._last_index_built_at is not None


# ---------------------------------------------------------------------------
# Unit Tests: Semantic Scoring
# ---------------------------------------------------------------------------

class TestSemanticScoring:
    def test_exact_name_match_scores_highest(self):
        engine = ToolDiscoveryEngine(_default_settings())
        engine.rebuild_index(_sample_tools())
        result = engine.search("get_user")
        matched = result["structuredContent"]["matched_tools"]
        assert len(matched) > 0
        assert matched[0]["name"] == "get_user"

    def test_chinese_keyword_match(self):
        engine = ToolDiscoveryEngine(_default_settings())
        engine.rebuild_index(_sample_tools())
        result = engine.search("查询用户信息")
        matched = result["structuredContent"]["matched_tools"]
        assert len(matched) > 0
        names = [t["name"] for t in matched]
        assert "get_user" in names

    def test_order_keyword_match(self):
        engine = ToolDiscoveryEngine(_default_settings())
        engine.rebuild_index(_sample_tools())
        result = engine.search("创建订单")
        matched = result["structuredContent"]["matched_tools"]
        assert len(matched) > 0
        names = [t["name"] for t in matched]
        assert "create_order" in names

    def test_unrelated_query_returns_empty_or_low_scores(self):
        engine = ToolDiscoveryEngine(
            _default_settings(discovery_score_threshold=0.3, discovery_fallback_on_empty=False)
        )
        engine.rebuild_index(_sample_tools())
        result = engine.search("视频转码压缩")
        matched = result["structuredContent"]["matched_tools"]
        assert len(matched) == 0


# ---------------------------------------------------------------------------
# Unit Tests: Category Filtering
# ---------------------------------------------------------------------------

class TestCategoryFiltering:
    def test_filter_by_existing_category(self):
        engine = ToolDiscoveryEngine(_default_settings())
        engine.rebuild_index(_sample_tools())
        result = engine.search("用户", category="user_management")
        matched = result["structuredContent"]["matched_tools"]
        for tool in matched:
            assert tool["name"] in ("get_user", "update_user_email")

    def test_filter_by_nonexistent_category_returns_empty(self):
        engine = ToolDiscoveryEngine(
            _default_settings(discovery_fallback_on_empty=False)
        )
        engine.rebuild_index(_sample_tools())
        result = engine.search("用户", category="nonexistent_category")
        matched = result["structuredContent"]["matched_tools"]
        assert len(matched) == 0

    def test_nonexistent_category_does_not_trigger_fallback(self):
        engine = ToolDiscoveryEngine(_default_settings())
        engine.rebuild_index(_sample_tools())
        primary = [t for t in _sample_tools() if t.tool_meta.tier == "primary"]
        result = engine.search("用户", category="nonexistent_category", primary_tools=primary)
        sc = result["structuredContent"]
        assert sc["fallback_triggered"] is False
        assert "primary_tools" not in sc

    def test_null_category_tools_excluded_from_category_filter(self):
        tools = [
            _make_tool("no_cat", "无分类工具", "没有分类的工具", category=None),
            _make_tool("has_cat", "有分类工具", "有分类的工具", category="finance"),
        ]
        engine = ToolDiscoveryEngine(_default_settings())
        engine.rebuild_index(tools)
        result = engine.search("工具", category="finance")
        matched = result["structuredContent"]["matched_tools"]
        names = [t["name"] for t in matched]
        assert "no_cat" not in names

    def test_no_category_searches_all(self):
        engine = ToolDiscoveryEngine(_default_settings())
        engine.rebuild_index(_sample_tools())
        result = engine.search("查询", category=None)
        matched = result["structuredContent"]["matched_tools"]
        assert len(matched) >= 1


# ---------------------------------------------------------------------------
# Unit Tests: Fallback Behavior
# ---------------------------------------------------------------------------

class TestFallbackBehavior:
    def test_fallback_returns_primary_tools(self):
        engine = ToolDiscoveryEngine(
            _default_settings(discovery_score_threshold=0.99)
        )
        tools = _sample_tools()
        engine.rebuild_index(tools)
        primary = [t for t in tools if t.tool_meta.tier == "primary"]
        result = engine.search("完全无关的量子计算", primary_tools=primary)
        sc = result["structuredContent"]
        assert sc["fallback_triggered"] is True
        assert "get_user" in sc["primary_tools"]
        assert "create_order" in sc["primary_tools"]

    def test_fallback_text_contains_tool_descriptions(self):
        engine = ToolDiscoveryEngine(
            _default_settings(discovery_score_threshold=0.99)
        )
        tools = _sample_tools()
        engine.rebuild_index(tools)
        primary = [t for t in tools if t.tool_meta.tier == "primary"]
        result = engine.search("完全无关的量子计算", primary_tools=primary)
        text = result["content"][0]["text"]
        assert "get_user: " in text
        assert "查询用户" in text

    def test_fallback_disabled_returns_no_primary(self):
        engine = ToolDiscoveryEngine(
            _default_settings(discovery_score_threshold=0.99, discovery_fallback_on_empty=False)
        )
        engine.rebuild_index(_sample_tools())
        result = engine.search("完全无关的量子计算")
        sc = result["structuredContent"]
        assert sc["fallback_triggered"] is False
        assert "primary_tools" not in sc

    def test_fallback_triggered_flag_correct(self):
        engine = ToolDiscoveryEngine(_default_settings())
        engine.rebuild_index(_sample_tools())
        result = engine.search("查询用户")
        sc = result["structuredContent"]
        assert sc["fallback_triggered"] is False


# ---------------------------------------------------------------------------
# Unit Tests: Result Formatting
# ---------------------------------------------------------------------------

class TestResultFormatting:
    def test_content_text_contains_tool_info(self):
        engine = ToolDiscoveryEngine(_default_settings())
        engine.rebuild_index(_sample_tools())
        result = engine.search("查询用户")
        text = result["content"][0]["text"]
        assert "get_user" in text
        assert "相关度" in text

    def test_structured_content_has_input_schema(self):
        engine = ToolDiscoveryEngine(_default_settings())
        engine.rebuild_index(_sample_tools())
        result = engine.search("查询用户")
        matched = result["structuredContent"]["matched_tools"]
        assert len(matched) > 0
        assert "inputSchema" in matched[0]
        assert matched[0]["inputSchema"]["type"] == "object"

    def test_fallback_text_contains_primary_tools(self):
        engine = ToolDiscoveryEngine(
            _default_settings(discovery_score_threshold=0.99)
        )
        tools = _sample_tools()
        engine.rebuild_index(tools)
        primary = [t for t in tools if t.tool_meta.tier == "primary"]
        result = engine.search("视频转码", primary_tools=primary)
        text = result["content"][0]["text"]
        assert "未找到" in text
        assert "get_user" in text

    def test_top_k_limits_results(self):
        engine = ToolDiscoveryEngine(_default_settings(discovery_score_threshold=0.001))
        engine.rebuild_index(_sample_tools())
        result = engine.search("查询", top_k=2)
        matched = result["structuredContent"]["matched_tools"]
        assert len(matched) <= 2

    def test_top_k_clamped_to_max(self):
        engine = ToolDiscoveryEngine(_default_settings(discovery_max_top_k=3))
        engine.rebuild_index(_sample_tools())
        result = engine.search("查询", top_k=100)
        matched = result["structuredContent"]["matched_tools"]
        assert len(matched) <= 3


# ---------------------------------------------------------------------------
# Unit Tests: Engine Statistics
# ---------------------------------------------------------------------------

class TestEngineStatistics:
    def test_describe_returns_stats(self):
        engine = ToolDiscoveryEngine(_default_settings())
        engine.rebuild_index(_sample_tools())
        engine.search("用户")
        stats = engine.describe()
        assert stats["total_searches"] == 1
        assert stats["index_tool_count"] == 5
        assert stats["last_index_built_at"] is not None

    def test_describe_includes_avg_top_score(self):
        engine = ToolDiscoveryEngine(_default_settings())
        engine.rebuild_index(_sample_tools())
        engine.search("查询用户")
        engine.search("创建订单")
        stats = engine.describe()
        assert "avg_top_score" in stats
        assert stats["avg_top_score"] > 0

    def test_rate_limited_count_incremented(self):
        engine = ToolDiscoveryEngine(_default_settings())
        engine.increment_rate_limited()
        engine.increment_rate_limited()
        assert engine.describe()["rate_limited_count"] == 2


# ---------------------------------------------------------------------------
# Unit Tests: Empty Index Safety
# ---------------------------------------------------------------------------

class TestEmptyIndex:
    def test_search_on_empty_index_triggers_fallback(self):
        engine = ToolDiscoveryEngine(_default_settings())
        result = engine.search("查询用户")
        sc = result["structuredContent"]
        assert sc["matched_tools"] == []
        assert sc["fallback_triggered"] is True

    def test_search_on_empty_index_no_exception(self):
        engine = ToolDiscoveryEngine(_default_settings())
        result = engine.search("anything")
        assert "content" in result


# ---------------------------------------------------------------------------
# Integration Tests: Full MCP Flow with discovery
# ---------------------------------------------------------------------------

def _build_integration_client(tmp_path: Path) -> TestClient:
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
        discovery_rate_limit_max=10,
        discovery_rate_limit_window=60,
    )
    app = create_app(settings, connector=RestConnector(client=mock_http_client))
    return TestClient(app)


def _do_initialize(client: TestClient) -> str:
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


def test_tools_list_returns_primary_plus_search_apis(tmp_path: Path) -> None:
    with _build_integration_client(tmp_path) as client:
        session_id = _do_initialize(client)
        response = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        tool_names = {t["name"] for t in response.json()["result"]["tools"]}
        assert "search_apis" in tool_names
        assert "get_user" in tool_names
        assert "create_order" in tool_names
        assert "probe_slow" not in tool_names
        assert "probe_failure" not in tool_names
        assert "probe_retry" not in tool_names


def test_search_apis_returns_matched_tools(tmp_path: Path) -> None:
    with _build_integration_client(tmp_path) as client:
        session_id = _do_initialize(client)
        response = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "search_apis",
                    "arguments": {"query": "查询用户信息"},
                },
            },
        )
        payload = response.json()
        result = payload["result"]
        assert "content" in result
        assert "structuredContent" in result
        matched = result["structuredContent"]["matched_tools"]
        assert len(matched) > 0
        names = [t["name"] for t in matched]
        assert "get_user" in names


def test_search_apis_with_category_filter(tmp_path: Path) -> None:
    with _build_integration_client(tmp_path) as client:
        session_id = _do_initialize(client)
        response = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "search_apis",
                    "arguments": {"query": "订单", "category": "order"},
                },
            },
        )
        matched = response.json()["result"]["structuredContent"]["matched_tools"]
        for tool in matched:
            assert "order" in tool["name"].lower() or "order" in tool["description"]


def test_search_apis_empty_query_returns_validation_error(tmp_path: Path) -> None:
    with _build_integration_client(tmp_path) as client:
        session_id = _do_initialize(client)
        response = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "search_apis",
                    "arguments": {"query": ""},
                },
            },
        )
        payload = response.json()
        assert payload["error"]["code"] == -32602


def test_search_apis_missing_query_returns_validation_error(tmp_path: Path) -> None:
    with _build_integration_client(tmp_path) as client:
        session_id = _do_initialize(client)
        response = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "search_apis",
                    "arguments": {},
                },
            },
        )
        payload = response.json()
        assert payload["error"]["code"] == -32602


def test_search_apis_rate_limiting(tmp_path: Path) -> None:
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
        rate_limit_max_requests=200,
        session_ttl_seconds=300,
        discovery_rate_limit_max=3,
        discovery_rate_limit_window=60,
    )
    app = create_app(settings, connector=RestConnector(client=mock_http_client))

    with TestClient(app) as client:
        session_id = _do_initialize(client)
        for i in range(3):
            resp = client.post(
                "/mcp",
                headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
                json={
                    "jsonrpc": "2.0",
                    "id": 100 + i,
                    "method": "tools/call",
                    "params": {
                        "name": "search_apis",
                        "arguments": {"query": f"test query {i}"},
                    },
                },
            )
            assert "error" not in resp.json()

        limited_resp = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 200,
                "method": "tools/call",
                "params": {
                    "name": "search_apis",
                    "arguments": {"query": "should be limited"},
                },
            },
        )
        result = limited_resp.json()["result"]
        assert result["isError"] is True
        assert "RATE_LIMITED" in result["content"][0]["text"]


def test_normal_tool_call_unaffected(tmp_path: Path) -> None:
    with _build_integration_client(tmp_path) as client:
        session_id = _do_initialize(client)
        response = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "get_user",
                    "arguments": {"user_id": 1, "verbose": True},
                },
            },
        )
        result = response.json()["result"]
        assert result["structuredContent"]["id"] == 1
        assert result["meta"]["status_code"] == 200


def test_search_apis_discover_then_call(tmp_path: Path) -> None:
    """End-to-end: discover a tool via search_apis, then call it."""
    with _build_integration_client(tmp_path) as client:
        session_id = _do_initialize(client)

        search_resp = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "search_apis",
                    "arguments": {"query": "查询用户"},
                },
            },
        )
        matched = search_resp.json()["result"]["structuredContent"]["matched_tools"]
        assert any(t["name"] == "get_user" for t in matched)

        call_resp = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "get_user",
                    "arguments": {"user_id": 7, "verbose": True},
                },
            },
        )
        assert call_resp.json()["result"]["structuredContent"]["id"] == 7


def test_hot_reload_updates_discovery_index(tmp_path: Path) -> None:
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
        sqlite_path=tmp_path / "gateway.db",
        rate_limit_max_requests=100,
        session_ttl_seconds=300,
    )
    app = create_app(settings, connector=RestConnector(client=mock_http_client))

    with TestClient(app) as client:
        session_id = _do_initialize(client)

        extra_tool = """
tool_meta:
  name: query_balance
  title: 查询余额
  description: 查询用户账户余额信息。
  tags: ["财务", "查询"]
  category: finance
  tier: secondary
input_schema:
  type: object
  properties:
    account_id:
      type: string
  required: ["account_id"]
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
        (config_dir / "query_balance.yaml").write_text(extra_tool, encoding="utf-8")
        client.post("/admin/reload", headers={"x-api-key": "dev-admin-key"})

        search_resp = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "search_apis",
                    "arguments": {"query": "查询余额"},
                },
            },
        )
        matched = search_resp.json()["result"]["structuredContent"]["matched_tools"]
        names = [t["name"] for t in matched]
        assert "query_balance" in names


def test_rollback_restores_discovery_index(tmp_path: Path) -> None:
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
        sqlite_path=tmp_path / "gateway.db",
        rate_limit_max_requests=100,
        session_ttl_seconds=300,
    )
    app = create_app(settings, connector=RestConnector(client=mock_http_client))

    with TestClient(app) as client:
        session_id = _do_initialize(client)
        admin_headers = {"x-api-key": "dev-admin-key"}

        extra_tool = """
tool_meta:
  name: temp_tool_for_rollback
  title: 临时工具
  description: 回滚测试用临时工具。
  category: system
  tier: secondary
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
        (config_dir / "temp_tool_for_rollback.yaml").write_text(extra_tool, encoding="utf-8")
        client.post("/admin/reload", headers=admin_headers)
        client.post("/admin/rollback", headers=admin_headers)

        search_resp = client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {
                    "name": "search_apis",
                    "arguments": {"query": "临时工具"},
                },
            },
        )
        matched = search_resp.json()["result"]["structuredContent"]["matched_tools"]
        names = [t["name"] for t in matched]
        assert "temp_tool_for_rollback" not in names


def test_admin_status_includes_discovery_stats(tmp_path: Path) -> None:
    with _build_integration_client(tmp_path) as client:
        session_id = _do_initialize(client)

        client.post(
            "/mcp",
            headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {
                    "name": "search_apis",
                    "arguments": {"query": "用户"},
                },
            },
        )

        status = client.get("/admin/status", headers={"x-api-key": "dev-admin-key"}).json()
        assert "discovery" in status
        assert status["discovery"]["total_searches"] >= 1
        assert status["discovery"]["index_tool_count"] >= 1


def test_engine_internal_error_degrades_to_fallback(tmp_path: Path) -> None:
    """When the discovery engine raises an unexpected error, McpService
    should degrade gracefully and return a fallback response instead of
    a JSON-RPC INTERNAL_ERROR."""
    with _build_integration_client(tmp_path) as client:
        session_id = _do_initialize(client)

        with patch.object(
            client.app.state.discovery_engine,
            "search",
            side_effect=RuntimeError("simulated TF-IDF crash"),
        ):
            resp = client.post(
                "/mcp",
                headers={"x-api-key": "dev-api-key", "mcp-session-id": session_id},
                json={
                    "jsonrpc": "2.0",
                    "id": 99,
                    "method": "tools/call",
                    "params": {
                        "name": "search_apis",
                        "arguments": {"query": "查询用户"},
                    },
                },
            )
        payload = resp.json()
        assert "error" not in payload
        result = payload["result"]
        assert "content" in result
        assert result["structuredContent"]["fallback_triggered"] is True


def test_search_apis_rate_limit_works_without_api_key(tmp_path: Path) -> None:
    """Rate limiting should still work when client_key is None (anonymous)."""
    mock_app = create_mock_app()
    mock_http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app),
        base_url="http://127.0.0.1:9001",
    )
    settings = Settings(
        api_key="",
        admin_api_key="dev-admin-key",
        config_dir=Path("configs/tools"),
        openapi_dir=Path("configs/openapi"),
        sqlite_path=tmp_path / "gateway.db",
        rate_limit_max_requests=200,
        session_ttl_seconds=300,
        discovery_rate_limit_max=2,
        discovery_rate_limit_window=60,
    )
    app = create_app(settings, connector=RestConnector(client=mock_http_client))

    with TestClient(app) as client:
        session_id = _do_initialize(client)
        for i in range(2):
            client.post(
                "/mcp",
                headers={"mcp-session-id": session_id},
                json={
                    "jsonrpc": "2.0",
                    "id": 300 + i,
                    "method": "tools/call",
                    "params": {
                        "name": "search_apis",
                        "arguments": {"query": f"anon query {i}"},
                    },
                },
            )

        limited_resp = client.post(
            "/mcp",
            headers={"mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 400,
                "method": "tools/call",
                "params": {
                    "name": "search_apis",
                    "arguments": {"query": "should be limited"},
                },
            },
        )
        result = limited_resp.json()["result"]
        assert result["isError"] is True
        assert "RATE_LIMITED" in result["content"][0]["text"]
