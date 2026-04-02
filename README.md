# MCP Smart API Gateway

一个基于 `FastAPI + SQLite + YAML` 的配置驱动型 MCP 到 REST 智能 API 适配网关。

## 启动

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
copy .env.example .env
python scripts/mock_rest_api.py
python -m app
```

默认配置：

- MCP 入口：`POST /mcp`
- 健康检查：`GET /health`
- 管理接口：`/admin/reload`、`/admin/tools`、`/admin/status`
- 网关 API Key：`dev-api-key`
- 管理 API Key：`dev-admin-key`

## 环境变量

项目启动时会自动读取根目录下的 `.env` 文件；如果不存在，则回退到内置默认值。

常用配置项：

- `MCP_GATEWAY_HOST` / `MCP_GATEWAY_PORT`：服务监听地址
- `MCP_GATEWAY_API_KEY`：MCP 调用鉴权 Key
- `MCP_GATEWAY_ADMIN_API_KEY`：管理接口鉴权 Key
- `MCP_GATEWAY_CONFIG_DIR`：工具配置目录
- `MCP_GATEWAY_OPENAPI_DIR`：OpenAPI 文档目录
- `MCP_GATEWAY_SQLITE_PATH`：SQLite 数据库文件路径
- `MCP_GATEWAY_LOG_LEVEL`：日志级别
- `MCP_GATEWAY_RATE_LIMIT_WINDOW_SECONDS`：限流时间窗口
- `MCP_GATEWAY_RATE_LIMIT_MAX_REQUESTS`：窗口内最大请求数

## 示例请求

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "get_user",
    "arguments": {
      "user_id": 1,
      "verbose": true
    }
  }
}
```

## 实验脚本

先分别启动 mock 下游服务和网关：

```bash
python scripts/mock_rest_api.py
python -m app
```

然后运行实验脚本：

```bash
python scripts/run_experiments.py
```

脚本会输出两个文件：

- `experiment_report.json`：完整实验明细，适合后续统计和论文附录
- `experiment_report.md`：简要实验摘要，适合直接整理到周报或论文初稿

默认实验内容包括：

- `initialize`、`tools/list` 与多个 `tools/call` 功能验证
- 慢响应、下游 500、参数错误与幂等重试场景
- 多组并发请求压测
- 管理接口状态采集
- 临时新增工具并触发 `/admin/reload` 的热更新验证
