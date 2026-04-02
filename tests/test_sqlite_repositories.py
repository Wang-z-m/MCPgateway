from __future__ import annotations

import pytest

from app.db.database import Database
from app.db.repositories import AccessLogRepository, ConfigAuditRepository


@pytest.mark.asyncio
async def test_access_log_repository_returns_metrics_and_recent_calls(tmp_path) -> None:
    database = Database(tmp_path / "gateway.db")
    await database.initialize()
    access_logs = AccessLogRepository(database)

    await access_logs.record_call(
        request_id="req-1",
        tool_name="get_user",
        success=True,
        error_type=None,
        downstream_status=200,
        latency_ms=18,
        attempts_made=2,
    )
    await access_logs.record_call(
        request_id="req-2",
        tool_name="get_user",
        success=False,
        error_type="DOWNSTREAM_ERROR",
        downstream_status=500,
        latency_ms=31,
    )

    snapshot = await access_logs.build_metrics_snapshot()
    recent_calls = await access_logs.latest_calls()

    assert snapshot["total_calls"] == 2
    assert snapshot["success_calls"] == 1
    assert snapshot["failure_calls"] == 1
    assert snapshot["by_tool"][0]["tool_name"] == "get_user"
    assert snapshot["by_tool"][0]["success_rate"] == 0.5
    assert snapshot["error_distribution"][0]["error_type"] == "DOWNSTREAM_ERROR"
    assert recent_calls[0]["request_id"] == "req-2"
    assert recent_calls[1]["attempts_made"] == 2


@pytest.mark.asyncio
async def test_config_audit_repository_returns_summary(tmp_path) -> None:
    database = Database(tmp_path / "gateway.db")
    await database.initialize()
    audits = ConfigAuditRepository(database)

    await audits.record_event(version="v1", config_hash="hash1", status="loaded")
    await audits.record_event(
        version="v2",
        config_hash="hash2",
        status="reload_failed",
        error_message="broken yaml",
        rollback_from="v1",
    )

    summary = await audits.build_summary()
    events = await audits.latest_events()

    assert summary["total_events"] == 2
    assert summary["failed_events"] == 1
    assert summary["last_published_at"] is not None
    assert events[0]["status"] == "reload_failed"
    assert events[0]["rollback_from"] == "v1"


@pytest.mark.asyncio
async def test_empty_access_log_returns_zero_metrics(tmp_path) -> None:
    database = Database(tmp_path / "gateway.db")
    await database.initialize()
    access_logs = AccessLogRepository(database)

    snapshot = await access_logs.build_metrics_snapshot()
    recent = await access_logs.latest_calls()

    assert snapshot["total_calls"] == 0
    assert snapshot["success_calls"] == 0
    assert snapshot["failure_calls"] == 0
    assert snapshot["avg_latency_ms"] == 0
    assert snapshot["success_rate"] == 0.0
    assert snapshot["by_tool"] == []
    assert snapshot["error_distribution"] == []
    assert recent == []


@pytest.mark.asyncio
async def test_empty_config_audit_returns_zero_summary(tmp_path) -> None:
    database = Database(tmp_path / "gateway.db")
    await database.initialize()
    audits = ConfigAuditRepository(database)

    summary = await audits.build_summary()
    events = await audits.latest_events()

    assert summary["total_events"] == 0
    assert summary["failed_events"] == 0
    assert summary["last_published_at"] is None
    assert events == []


@pytest.mark.asyncio
async def test_latest_calls_respects_limit(tmp_path) -> None:
    database = Database(tmp_path / "gateway.db")
    await database.initialize()
    access_logs = AccessLogRepository(database)

    for i in range(5):
        await access_logs.record_call(
            request_id=f"req-{i}",
            tool_name="tool_a",
            success=True,
            error_type=None,
            downstream_status=200,
            latency_ms=10,
        )

    recent_2 = await access_logs.latest_calls(limit=2)
    recent_all = await access_logs.latest_calls(limit=10)

    assert len(recent_2) == 2
    assert len(recent_all) == 5
    assert recent_2[0]["request_id"] == "req-4"
    assert recent_2[1]["request_id"] == "req-3"


@pytest.mark.asyncio
async def test_latest_events_respects_limit(tmp_path) -> None:
    database = Database(tmp_path / "gateway.db")
    await database.initialize()
    audits = ConfigAuditRepository(database)

    for i in range(5):
        await audits.record_event(version=f"v{i}", config_hash=f"h{i}", status="loaded")

    recent_2 = await audits.latest_events(limit=2)
    recent_all = await audits.latest_events(limit=10)

    assert len(recent_2) == 2
    assert len(recent_all) == 5
    assert recent_2[0]["version"] == "v4"


@pytest.mark.asyncio
async def test_access_log_metrics_by_tool_aggregation(tmp_path) -> None:
    database = Database(tmp_path / "gateway.db")
    await database.initialize()
    access_logs = AccessLogRepository(database)

    await access_logs.record_call(
        request_id="r1", tool_name="tool_a", success=True,
        error_type=None, downstream_status=200, latency_ms=10,
    )
    await access_logs.record_call(
        request_id="r2", tool_name="tool_a", success=False,
        error_type="TIMEOUT_ERROR", downstream_status=None, latency_ms=5000,
    )
    await access_logs.record_call(
        request_id="r3", tool_name="tool_b", success=True,
        error_type=None, downstream_status=200, latency_ms=20,
    )

    snapshot = await access_logs.build_metrics_snapshot()

    assert snapshot["total_calls"] == 3
    assert snapshot["success_calls"] == 2
    assert snapshot["failure_calls"] == 1

    tool_a = next(t for t in snapshot["by_tool"] if t["tool_name"] == "tool_a")
    assert tool_a["call_count"] == 2
    assert tool_a["success_rate"] == 0.5

    tool_b = next(t for t in snapshot["by_tool"] if t["tool_name"] == "tool_b")
    assert tool_b["call_count"] == 1
    assert tool_b["success_rate"] == 1.0

    assert snapshot["error_distribution"][0]["error_type"] == "TIMEOUT_ERROR"
    assert snapshot["error_distribution"][0]["count"] == 1
