from __future__ import annotations

import argparse
import json
import shutil
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx


def percentile(values: list[int], ratio: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return ordered[index]


def rpc_request(
    client: httpx.Client,
    *,
    request_id: int,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    started = time.perf_counter()
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    return {
        "payload": response.json(),
        "latency_ms": latency_ms,
        "http_status": response.status_code,
        "headers": {
            "x-request-id": response.headers.get("x-request-id"),
            "x-rate-limit-limit": response.headers.get("x-rate-limit-limit"),
            "x-rate-limit-remaining": response.headers.get("x-rate-limit-remaining"),
            "x-rate-limit-reset": response.headers.get("x-rate-limit-reset"),
        },
    }


def admin_request(
    client: httpx.Client,
    *,
    method: str,
    path: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    response = client.request(method, path)
    latency_ms = int((time.perf_counter() - started) * 1000)
    return {
        "payload": response.json(),
        "latency_ms": latency_ms,
        "http_status": response.status_code,
    }


def summarize_rpc_case(case: dict[str, Any]) -> dict[str, Any]:
    payload = case["payload"]
    error = payload.get("error")
    result = payload.get("result")
    is_tool_error = isinstance(result, dict) and result.get("isError") is True
    ok = error is None and not is_tool_error

    error_code: int | None = None
    error_category: str | None = None

    if error:
        error_code = error.get("code")
        error_category = (error.get("data") or {}).get("category")
    elif is_tool_error:
        content = result.get("content") or []
        text = content[0].get("text", "") if content and isinstance(content[0], dict) else ""
        if text.startswith("[") and "]" in text:
            error_category = text[1 : text.index("]")]

    attempts_made = None
    if result and not is_tool_error:
        attempts_made = (result.get("meta") or {}).get("attempts_made")

    return {
        "ok": ok,
        "latency_ms": case["latency_ms"],
        "http_status": case["http_status"],
        "request_id": payload.get("id"),
        "error_code": error_code,
        "error_category": error_category,
        "attempts_made": attempts_made,
        "payload": payload,
    }


def run_concurrency_case(
    client: httpx.Client,
    concurrency: int,
    *,
    rounds: int = 1,
) -> dict[str, Any]:
    request_count = concurrency * rounds

    def task(index: int) -> tuple[bool, int]:
        case = rpc_request(
            client,
            request_id=1000 + index,
            method="tools/call",
            params={"name": "get_user", "arguments": {"user_id": index + 1}},
        )
        result = case["payload"].get("result")
        ok = isinstance(result, dict) and not result.get("isError", False)
        return (ok, case["latency_ms"])

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        results = list(executor.map(task, range(request_count)))

    success_count = sum(1 for ok, _ in results if ok)
    failure_count = request_count - success_count
    latencies = [latency for _, latency in results]
    return {
        "concurrency": concurrency,
        "rounds": rounds,
        "request_count": request_count,
        "success_count": success_count,
        "failure_count": failure_count,
        "success_rate": round(success_count / request_count, 4),
        "avg_latency_ms": round(statistics.mean(latencies), 2),
        "p95_latency_ms": percentile(latencies, 0.95),
        "max_latency_ms": max(latencies),
    }


def build_reload_experiment(
    admin_client: httpx.Client,
    *,
    config_dir: Path | None,
) -> dict[str, Any]:
    if config_dir is None:
        return {"skipped": True, "reason": "No config directory provided."}

    config_dir = config_dir.resolve()
    if not config_dir.exists():
        return {"skipped": True, "reason": f"Config directory '{config_dir}' does not exist."}

    temp_tool_name = "probe_reload_temp"
    temp_config_path = config_dir / f"{temp_tool_name}.yaml"
    temp_config = """
tool_meta:
  name: probe_reload_temp
  title: Probe reload temp tool
  description: Temporary tool used by the experiment runner.
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
  include_http_meta: true
error_mapping: {}
""".strip()

    cleanup_result: dict[str, Any] | None = None
    try:
        temp_config_path.write_text(temp_config, encoding="utf-8")
        added_result = admin_request(admin_client, method="POST", path="/admin/reload")
        return_payload = added_result["payload"]

        temp_config_path.unlink(missing_ok=True)
        cleanup_result = admin_request(admin_client, method="POST", path="/admin/reload")

        return {
            "skipped": False,
            "added_tool_name": temp_tool_name,
            "add_result": added_result,
            "remove_result": cleanup_result,
            "change_summary": return_payload.get("changes"),
        }
    finally:
        if temp_config_path.exists():
            temp_config_path.unlink()
            cleanup_result = admin_request(admin_client, method="POST", path="/admin/reload")


def build_report_summary(report: dict[str, Any]) -> dict[str, Any]:
    functional_cases = report["functional_cases"]
    concurrency_cases = report["concurrency_cases"]
    success_cases = sum(1 for case in functional_cases.values() if case["ok"])
    failure_cases = len(functional_cases) - success_cases
    concurrency_success_rates = [case["success_rate"] for case in concurrency_cases]

    return {
        "generated_at": report["generated_at"],
        "functional_success_count": success_cases,
        "functional_failure_count": failure_cases,
        "concurrency_case_count": len(concurrency_cases),
        "min_concurrency_success_rate": min(concurrency_success_rates) if concurrency_success_rates else 0.0,
        "max_concurrency_p95_latency_ms": max(
            (case["p95_latency_ms"] for case in concurrency_cases), default=0
        ),
    }


def format_markdown_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    functional_lines = []
    for name, case in report["functional_cases"].items():
        status = "PASS" if case["ok"] else "FAIL"
        extra = (
            f"error_code={case['error_code']}, category={case['error_category']}"
            if not case["ok"]
            else f"attempts={case['attempts_made'] or 1}"
        )
        functional_lines.append(
            f"- `{name}`: {status}, latency={case['latency_ms']}ms, {extra}"
        )

    concurrency_lines = [
        f"- 并发 `{case['concurrency']}` x 轮次 `{case['rounds']}`: success_rate={case['success_rate']}, avg={case['avg_latency_ms']}ms, p95={case['p95_latency_ms']}ms"
        for case in report["concurrency_cases"]
    ]

    reload_experiment = report["reload_experiment"]
    if reload_experiment.get("skipped"):
        reload_text = f"- 跳过：{reload_experiment['reason']}"
    else:
        change_summary = reload_experiment.get("change_summary") or {}
        reload_text = (
            f"- 新增工具 `{reload_experiment['added_tool_name']}`，"
            f"reload changes={json.dumps(change_summary, ensure_ascii=False)}"
        )

    return "\n".join(
        [
            "# MCP 网关实验报告",
            "",
            f"- 生成时间：`{summary['generated_at']}`",
            f"- 功能验证成功数：`{summary['functional_success_count']}`",
            f"- 功能验证失败数：`{summary['functional_failure_count']}`",
            f"- 并发场景数：`{summary['concurrency_case_count']}`",
            f"- 最低并发成功率：`{summary['min_concurrency_success_rate']}`",
            f"- 最大 P95 时延：`{summary['max_concurrency_p95_latency_ms']}ms`",
            "",
            "## 功能验证",
            *functional_lines,
            "",
            "## 并发验证",
            *concurrency_lines,
            "",
            "## 热更新验证",
            reload_text,
        ]
    )


def parse_concurrency_values(raw_values: str) -> list[int]:
    return [int(value.strip()) for value in raw_values.split(",") if value.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run gateway experiment scenarios.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", default="dev-api-key")
    parser.add_argument("--admin-api-key", default="dev-admin-key")
    parser.add_argument("--output", default="experiment_report.json")
    parser.add_argument("--markdown-output", default="experiment_report.md")
    parser.add_argument("--config-dir", default="configs/tools")
    parser.add_argument("--concurrency", default="5,10,20")
    parser.add_argument("--rounds", type=int, default=1)
    args = parser.parse_args()

    output_path = Path(args.output)
    markdown_output_path = Path(args.markdown_output)
    config_dir = Path(args.config_dir) if args.config_dir else None

    gateway_headers = {"x-api-key": args.api_key}
    admin_headers = {"x-api-key": args.admin_api_key}

    with httpx.Client(base_url=args.base_url, headers=gateway_headers, timeout=15.0) as client:
        with httpx.Client(base_url=args.base_url, headers=admin_headers, timeout=15.0) as admin_client:
            report: dict[str, Any] = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "initialize": rpc_request(client, request_id=1, method="initialize", params={}),
                "tools_list": rpc_request(client, request_id=2, method="tools/list", params={}),
            }

            functional_raw = {
                "get_user": rpc_request(
                    client,
                    request_id=3,
                    method="tools/call",
                    params={"name": "get_user", "arguments": {"user_id": 1, "verbose": True}},
                ),
                "create_order": rpc_request(
                    client,
                    request_id=4,
                    method="tools/call",
                    params={
                        "name": "create_order",
                        "arguments": {
                            "item_name": "keyboard",
                            "amount": 99.5,
                            "customer_name": "Alice",
                            "request_source": "experiment-script",
                        },
                    },
                ),
                "probe_slow": rpc_request(
                    client,
                    request_id=5,
                    method="tools/call",
                    params={"name": "probe_slow", "arguments": {}},
                ),
                "probe_failure": rpc_request(
                    client,
                    request_id=6,
                    method="tools/call",
                    params={"name": "probe_failure", "arguments": {}},
                ),
                "probe_retry": rpc_request(
                    client,
                    request_id=7,
                    method="tools/call",
                    params={"name": "probe_retry", "arguments": {}},
                ),
                "validation_error": rpc_request(
                    client,
                    request_id=8,
                    method="tools/call",
                    params={"name": "get_user", "arguments": {}},
                ),
            }
            report["functional_cases"] = {
                name: summarize_rpc_case(case) for name, case in functional_raw.items()
            }
            report["concurrency_cases"] = [
                run_concurrency_case(client, concurrency=value, rounds=args.rounds)
                for value in parse_concurrency_values(args.concurrency)
            ]
            report["admin_status"] = admin_request(admin_client, method="GET", path="/admin/status")
            report["reload_experiment"] = build_reload_experiment(
                admin_client,
                config_dir=config_dir,
            )
            report["summary"] = build_report_summary(report)

    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_output_path.write_text(format_markdown_report(report), encoding="utf-8")
    print(f"Experiment report saved to {output_path}")
    print(f"Markdown summary saved to {markdown_output_path}")


if __name__ == "__main__":
    main()
