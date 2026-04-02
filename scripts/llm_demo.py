"""
LLM + MCP Gateway 端到端演示

完整调用链：用户提问 → 大模型推理 → 工具选择 → MCP 网关 → REST API → 大模型生成回答

用法:
    set OPENAI_API_KEY=sk-xxx
    python scripts/llm_demo.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

GATEWAY_BASE_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8000")
GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY", "dev-api-key")

SYSTEM_PROMPT = (
    "你是一个智能助手。你可以通过工具查询用户信息、创建订单等。"
    "当用户的问题需要调用后端服务时，请使用可用的工具来获取数据，然后用自然语言回答用户。"
    "回答请使用中文。"
)

# ---------------------------------------------------------------------------
# MCP 网关交互
# ---------------------------------------------------------------------------

def mcp_request(client: httpx.Client, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    body = {"jsonrpc": "2.0", "id": int(time.time() * 1000), "method": method, "params": params or {}}
    response = client.post(
        f"{GATEWAY_BASE_URL}/mcp",
        json=body,
        headers={"x-api-key": GATEWAY_API_KEY, "Content-Type": "application/json"},
    )
    return response.json()


def discover_tools(client: httpx.Client) -> list[dict[str, Any]]:
    result = mcp_request(client, "tools/list")
    return result.get("result", {}).get("tools", [])


def call_tool(client: httpx.Client, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = mcp_request(client, "tools/call", {"name": tool_name, "arguments": arguments})
    if "error" in result:
        return {"error": result["error"]["message"], "detail": result["error"].get("data", {})}
    return result.get("result", {})

# ---------------------------------------------------------------------------
# MCP 工具 → OpenAI function calling 格式转换
# ---------------------------------------------------------------------------

def mcp_tools_to_openai_functions(mcp_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    functions = []
    for tool in mcp_tools:
        schema = tool.get("inputSchema", {})
        parameters = {
            "type": schema.get("type", "object"),
            "properties": schema.get("properties", {}),
            "required": schema.get("required", []),
        }
        if schema.get("additionalProperties") is not None:
            parameters["additionalProperties"] = schema["additionalProperties"]
        functions.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": parameters,
            },
        })
    return functions

# ---------------------------------------------------------------------------
# OpenAI API 调用
# ---------------------------------------------------------------------------

def chat_completion(
    client: httpx.Client,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": OPENAI_MODEL,
        "messages": messages,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    response = client.post(
        f"{OPENAI_BASE_URL}/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        timeout=60.0,
    )
    if response.status_code != 200:
        print(f"\n[错误] OpenAI API 返回 {response.status_code}: {response.text}")
        sys.exit(1)
    return response.json()

# ---------------------------------------------------------------------------
# 主交互循环
# ---------------------------------------------------------------------------

def print_separator():
    print("─" * 60)


def print_step(icon: str, text: str):
    print(f"  {icon} {text}")


def run_conversation(user_input: str, messages: list[dict[str, Any]], openai_tools: list[dict[str, Any]], client: httpx.Client):
    messages.append({"role": "user", "content": user_input})
    print_separator()
    print_step("👤", f"用户: {user_input}")
    print_separator()

    while True:
        print_step("🤖", "大模型推理中...")
        response = chat_completion(client, messages, openai_tools)
        choice = response["choices"][0]
        message = choice["message"]

        if message.get("tool_calls"):
            messages.append(message)
            for tool_call in message["tool_calls"]:
                fn = tool_call["function"]
                tool_name = fn["name"]
                arguments = json.loads(fn["arguments"])

                print_step("🔧", f"大模型决定调用工具: {tool_name}")
                print_step("📋", f"参数: {json.dumps(arguments, ensure_ascii=False)}")
                print_step("🌐", "通过 MCP 网关转发到下游 REST API...")

                t0 = time.perf_counter()
                tool_result = call_tool(client, tool_name, arguments)
                elapsed = int((time.perf_counter() - t0) * 1000)

                if "error" in tool_result:
                    result_text = json.dumps(tool_result, ensure_ascii=False, indent=2)
                    print_step("❌", f"工具调用失败 ({elapsed}ms)")
                else:
                    content = tool_result.get("content", [])
                    result_text = content[0]["text"] if content else json.dumps(tool_result, ensure_ascii=False)
                    meta = tool_result.get("meta", {})
                    print_step("✅", f"工具返回成功 ({elapsed}ms, 下游 {meta.get('status_code', '?')}, 重试 {meta.get('attempts_made', 1)} 次)")

                print_step("📦", f"结果: {result_text[:200]}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": result_text,
                })

            print_step("🤖", "大模型根据工具结果生成回答...")
            continue

        assistant_text = message.get("content", "")
        messages.append({"role": "assistant", "content": assistant_text})
        print_separator()
        print_step("💬", f"助手: {assistant_text}")
        print_separator()
        break


def main():
    if not OPENAI_API_KEY:
        print("请先设置 OPENAI_API_KEY 环境变量:")
        print("  PowerShell:  $env:OPENAI_API_KEY = 'sk-xxx'")
        print("  CMD:         set OPENAI_API_KEY=sk-xxx")
        sys.exit(1)

    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║        MCP Smart API Gateway - LLM 端到端演示           ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()
    print(f"  大模型:    {OPENAI_MODEL} ({OPENAI_BASE_URL})")
    print(f"  MCP 网关:  {GATEWAY_BASE_URL}")
    print()

    with httpx.Client() as client:
        print_step("🔍", "从 MCP 网关发现可用工具...")
        mcp_tools = discover_tools(client)
        openai_tools = mcp_tools_to_openai_functions(mcp_tools)
        tool_names = [t["function"]["name"] for t in openai_tools]
        print_step("📦", f"已加载 {len(openai_tools)} 个工具: {', '.join(tool_names)}")
        print()

        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]

        print("输入你的问题（输入 q 退出，输入 clear 清除对话历史）:")
        print()

        while True:
            try:
                user_input = input(">>> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见!")
                break

            if not user_input:
                continue
            if user_input.lower() == "q":
                print("再见!")
                break
            if user_input.lower() == "clear":
                messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                print("对话历史已清除。")
                continue

            run_conversation(user_input, messages, openai_tools, client)
            print()


if __name__ == "__main__":
    main()
