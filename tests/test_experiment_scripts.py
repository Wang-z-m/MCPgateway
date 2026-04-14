from __future__ import annotations

from pathlib import Path

from scripts.run_experiments import (
    build_report_summary,
    format_markdown_report,
    parse_concurrency_values,
    percentile,
    summarize_rpc_case,
)


def test_parse_concurrency_values() -> None:
    assert parse_concurrency_values("5, 10,20") == [5, 10, 20]


def test_parse_concurrency_values_empty_string() -> None:
    assert parse_concurrency_values("") == []


def test_parse_concurrency_values_single_value() -> None:
    assert parse_concurrency_values("10") == [10]


def test_percentile_basic() -> None:
    values = [10, 20, 30, 40, 50]
    assert percentile(values, 0.5) == 30
    assert percentile(values, 0.0) == 10
    assert percentile(values, 1.0) == 50


def test_percentile_empty_list() -> None:
    assert percentile([], 0.5) == 0


def test_summarize_rpc_case_success() -> None:
    case = {
        "payload": {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "ok"}],
                "meta": {"attempts_made": 2},
            },
        },
        "latency_ms": 15,
        "http_status": 200,
    }
    summary = summarize_rpc_case(case)

    assert summary["ok"] is True
    assert summary["latency_ms"] == 15
    assert summary["error_code"] is None
    assert summary["attempts_made"] == 2


def test_summarize_rpc_case_error() -> None:
    case = {
        "payload": {
            "jsonrpc": "2.0",
            "id": 2,
            "error": {"code": -32050, "message": "fail", "data": {"category": "DOWNSTREAM_ERROR"}},
        },
        "latency_ms": 8,
        "http_status": 200,
    }
    summary = summarize_rpc_case(case)

    assert summary["ok"] is False
    assert summary["error_code"] == -32050
    assert summary["error_category"] == "DOWNSTREAM_ERROR"
    assert summary["attempts_made"] is None


def test_summarize_rpc_case_tool_error_via_is_error() -> None:
    case = {
        "payload": {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {
                "content": [{"type": "text", "text": "[DOWNSTREAM_ERROR] HTTP 500.\ndownstream_status: 500"}],
                "isError": True,
            },
        },
        "latency_ms": 12,
        "http_status": 200,
    }
    summary = summarize_rpc_case(case)

    assert summary["ok"] is False
    assert summary["error_code"] is None
    assert summary["error_category"] == "DOWNSTREAM_ERROR"
    assert summary["attempts_made"] is None


def _make_sample_report() -> dict:
    return {
        "generated_at": "2026-03-10T00:00:00+00:00",
        "functional_cases": {
            "get_user": {
                "ok": True,
                "latency_ms": 20,
                "http_status": 200,
                "request_id": 1,
                "error_code": None,
                "error_category": None,
                "attempts_made": 1,
                "payload": {},
            },
            "probe_failure": {
                "ok": False,
                "latency_ms": 12,
                "http_status": 200,
                "request_id": 2,
                "error_code": None,
                "error_category": "DOWNSTREAM_ERROR",
                "attempts_made": None,
                "payload": {},
            },
        },
        "concurrency_cases": [
            {
                "concurrency": 5,
                "rounds": 1,
                "request_count": 5,
                "success_count": 5,
                "failure_count": 0,
                "success_rate": 1.0,
                "avg_latency_ms": 30.0,
                "p95_latency_ms": 45,
                "max_latency_ms": 50,
            },
            {
                "concurrency": 10,
                "rounds": 1,
                "request_count": 10,
                "success_count": 9,
                "failure_count": 1,
                "success_rate": 0.9,
                "avg_latency_ms": 40.0,
                "p95_latency_ms": 80,
                "max_latency_ms": 100,
            },
        ],
        "reload_experiment": {"skipped": True, "reason": "No config directory provided."},
    }


def test_build_report_summary_computes_correct_values() -> None:
    report = _make_sample_report()
    summary = build_report_summary(report)

    assert summary["functional_success_count"] == 1
    assert summary["functional_failure_count"] == 1
    assert summary["concurrency_case_count"] == 2
    assert summary["min_concurrency_success_rate"] == 0.9
    assert summary["max_concurrency_p95_latency_ms"] == 80


def test_markdown_report_contains_key_sections() -> None:
    report = _make_sample_report()
    report["summary"] = build_report_summary(report)

    markdown = format_markdown_report(report)

    assert "# MCP 网关实验报告" in markdown
    assert "## 功能验证" in markdown
    assert "`get_user`" in markdown
    assert "PASS" in markdown
    assert "FAIL" in markdown
    assert "## 并发验证" in markdown
    assert "## 热更新验证" in markdown


def test_markdown_report_shows_reload_changes_when_not_skipped() -> None:
    report = _make_sample_report()
    report["reload_experiment"] = {
        "skipped": False,
        "added_tool_name": "new_tool",
        "add_result": {},
        "remove_result": {},
        "change_summary": {"added": ["new_tool"], "removed": [], "updated": []},
    }
    report["summary"] = build_report_summary(report)

    markdown = format_markdown_report(report)

    assert "`new_tool`" in markdown
