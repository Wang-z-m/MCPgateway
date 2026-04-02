from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.core.config_loader import ConfigLoader
from app.core.tool_registry import ToolRegistry
from app.db.database import Database
from app.db.repositories import ConfigAuditRepository


def test_config_loader_rejects_missing_directory(tmp_path: Path) -> None:
    loader = ConfigLoader(tmp_path / "missing-tools")

    with pytest.raises(FileNotFoundError):
        loader.load_tools()


def test_config_loader_rejects_non_object_payload(tmp_path: Path) -> None:
    config_dir = tmp_path / "tools"
    config_dir.mkdir()
    (config_dir / "invalid.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")

    loader = ConfigLoader(config_dir)

    with pytest.raises(ValueError, match="top level"):
        loader.load_tools()


@pytest.mark.asyncio
async def test_registry_reload_keeps_previous_snapshot_on_failure(tmp_path: Path) -> None:
    workspace_root = Path(__file__).resolve().parents[1]
    source_configs = workspace_root / "configs" / "tools"
    config_dir = tmp_path / "tools"
    shutil.copytree(source_configs, config_dir)

    database = Database(tmp_path / "gateway.db")
    await database.initialize()
    registry = ToolRegistry(ConfigLoader(config_dir), ConfigAuditRepository(database))
    await registry.load_initial_snapshot()

    original_status = registry.status()
    original_tool_names = {tool.tool_meta.name for tool in registry.list_tools()}

    # Introduce an invalid config update and verify the active snapshot remains unchanged.
    (config_dir / "get_user.yaml").write_text("[]\n", encoding="utf-8")

    with pytest.raises(ValueError):
        await registry.reload()

    assert registry.status()["version"] == original_status["version"]
    assert registry.status()["tool_count"] == original_status["tool_count"]
    assert {tool.tool_meta.name for tool in registry.list_tools()} == original_tool_names


@pytest.mark.asyncio
async def test_registry_status_exposes_previous_version_after_reload(tmp_path: Path) -> None:
    workspace_root = Path(__file__).resolve().parents[1]
    source_configs = workspace_root / "configs" / "tools"
    config_dir = tmp_path / "tools"
    shutil.copytree(source_configs, config_dir)

    database = Database(tmp_path / "gateway.db")
    await database.initialize()
    registry = ToolRegistry(ConfigLoader(config_dir), ConfigAuditRepository(database))
    await registry.load_initial_snapshot()
    first_version = registry.status()["version"]

    create_order_path = config_dir / "create_order.yaml"
    create_order_content = create_order_path.read_text(encoding="utf-8")
    create_order_path.write_text(
        create_order_content.replace("Create order", "Create order v2"),
        encoding="utf-8",
    )

    reload_result = await registry.reload()
    status = registry.status()

    assert reload_result["status"] == "ok"
    assert reload_result["tool_count"] == status["tool_count"]
    assert status["previous_version"] == first_version
    assert status["version"] != first_version
    assert status["loaded_at"]


@pytest.mark.asyncio
async def test_registry_rejects_duplicate_tool_names(tmp_path: Path) -> None:
    config_dir = tmp_path / "tools"
    config_dir.mkdir()

    tool_yaml = """
tool_meta:
  name: duplicate_tool
  title: Dup
  description: Duplicate test.
input_schema:
  type: object
  properties: {}
  additionalProperties: false
http_target:
  method: GET
  base_url: http://localhost
  path: /test
""".strip()
    (config_dir / "dup1.yaml").write_text(tool_yaml, encoding="utf-8")
    (config_dir / "dup2.yaml").write_text(tool_yaml, encoding="utf-8")

    database = Database(tmp_path / "gateway.db")
    await database.initialize()
    registry = ToolRegistry(ConfigLoader(config_dir), ConfigAuditRepository(database))

    with pytest.raises(ValueError, match="Duplicate"):
        await registry.load_initial_snapshot()


@pytest.mark.asyncio
async def test_registry_rollback_restores_previous_snapshot(tmp_path: Path) -> None:
    workspace_root = Path(__file__).resolve().parents[1]
    config_dir = tmp_path / "tools"
    shutil.copytree(workspace_root / "configs" / "tools", config_dir)

    database = Database(tmp_path / "gateway.db")
    await database.initialize()
    registry = ToolRegistry(ConfigLoader(config_dir), ConfigAuditRepository(database))
    await registry.load_initial_snapshot()

    v1 = registry.status()["version"]
    v1_hash = registry.status()["config_hash"]

    (config_dir / "get_user.yaml").write_text(
        (config_dir / "get_user.yaml").read_text(encoding="utf-8").replace("Get user profile", "Get user v2"),
        encoding="utf-8",
    )
    await registry.reload()
    v2 = registry.status()["version"]
    assert v2 != v1

    rollback_result = await registry.rollback()
    assert rollback_result["status"] == "ok"
    assert rollback_result["rolled_back_from"] == v2
    assert rollback_result["version"] == v1
    assert rollback_result["config_hash"] == v1_hash
    assert registry.status()["version"] == v1


@pytest.mark.asyncio
async def test_registry_rollback_fails_without_previous_snapshot(tmp_path: Path) -> None:
    workspace_root = Path(__file__).resolve().parents[1]
    config_dir = tmp_path / "tools"
    shutil.copytree(workspace_root / "configs" / "tools", config_dir)

    database = Database(tmp_path / "gateway.db")
    await database.initialize()
    registry = ToolRegistry(ConfigLoader(config_dir), ConfigAuditRepository(database))
    await registry.load_initial_snapshot()

    with pytest.raises(ValueError, match="No previous snapshot"):
        await registry.rollback()


@pytest.mark.asyncio
async def test_registry_reload_change_summary_detects_updates(tmp_path: Path) -> None:
    workspace_root = Path(__file__).resolve().parents[1]
    config_dir = tmp_path / "tools"
    shutil.copytree(workspace_root / "configs" / "tools", config_dir)

    database = Database(tmp_path / "gateway.db")
    await database.initialize()
    registry = ToolRegistry(ConfigLoader(config_dir), ConfigAuditRepository(database))
    await registry.load_initial_snapshot()

    (config_dir / "get_user.yaml").write_text(
        (config_dir / "get_user.yaml").read_text(encoding="utf-8").replace(
            "timeout_seconds: 5", "timeout_seconds: 8"
        ),
        encoding="utf-8",
    )

    result = await registry.reload()

    assert "get_user" in result["changes"]["updated"]
    assert result["changes"]["added"] == []
    assert result["changes"]["removed"] == []
