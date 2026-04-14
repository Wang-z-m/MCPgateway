# MCP Smart API Gateway

基于 MCP（Model Context Protocol）协议的智能 API 适配网关系统，作为 AI 客户端与传统 RESTful API 之间的"低代码"转换中间层。通过 YAML/JSON 配置驱动，将存量 REST API 快速封装为符合 MCP 规范的标准工具，供大模型调用。

## 技术栈

Python 3.11+ · FastAPI · Pydantic · httpx · aiosqlite · PyYAML · watchfiles · uvicorn

## 系统架构

```
┌──────────────────────────────────────────────────────────────────────┐
│                       MCP Smart API Gateway                          │
│                                                                      │
│  ┌─────────────┐    ┌────────────────────────────┐                   │
│  │  MCP Route   │───▶│        McpService           │                  │
│  │  (Session +  │    │  initialize / tools/list    │                  │
│  │   Auth +     │    │  / tools/call               │                  │
│  │  RateLimit)  │    └─────────────┬──────────────┘                  │
│  └─────────────┘                  │                                  │
│                                    │                                  │
│  ┌────────────────┐   ┌───────────▼──────────────┐                   │
│  │  ToolRegistry   │   │    AdaptationEngine       │                  │
│  │ (config-driven) │   │  validate → map → call    │                  │
│  └───────┬────────┘   │  → shape response          │                  │
│          │            └───────────┬──────────────┘                   │
│  ┌───────▼────────┐               │                                  │
│  │  ConfigLoader   │   ┌──────────▼───────────┐                      │
│  │ YAML/JSON/OAS   │   │    RestConnector      │                     │
│  └────────────────┘   │  httpx + 幂等重试      │                     │
│                        └──────────┬───────────┘                      │
│                                   │                                  │
├───────────────────────────────────┼──────────────────────────────────┤
│  辅助组件:                         │                                  │
│  · ApiKeyAuth (双 Key 鉴权)        │                                  │
│  · SlidingWindowRateLimiter       │                                  │
│  · SessionManager (会话生命周期)   │                                  │
│  · SQLite (access_log / audit)    │                                  │
│  · watchfiles (配置自动重载)       │                                  │
│  · Dashboard (前端管理界面)        │                                  │
└───────────────────────────────────┼──────────────────────────────────┘
                                    │
                                    ▼
                         下游 REST API 服务

MCP Client / LLM ──POST /mcp (JSON-RPC 2.0)──▶ Gateway ──REST──▶ 下游 API
```

## 项目结构

```
MCPgateway/
├── app/                          # 应用主包
│   ├── __main__.py               # CLI 入口 (python -m app)
│   ├── main.py                   # FastAPI 应用工厂、lifespan 组装、中间件、watchfiles
│   ├── settings.py               # 配置类，读取 .env 和环境变量
│   ├── api/                      # 路由层
│   │   ├── mcp_routes.py         # POST /mcp (JSON-RPC)、DELETE /mcp (终止会话)
│   │   ├── admin_routes.py       # /admin/reload、/admin/rollback、/admin/tools、/admin/status
│   │   └── health_routes.py      # GET /health
│   ├── core/                     # 核心业务逻辑
│   │   ├── mcp_service.py        # JSON-RPC 方法分发 (initialize / tools/list / tools/call)
│   │   ├── tool_registry.py      # 工具快照管理、reload / rollback、变更摘要
│   │   ├── config_loader.py      # YAML/JSON 配置文件加载、OpenAPI 预览生成
│   │   ├── schema_generator.py   # OpenAPI 3.x → ToolConfig 自动转换
│   │   ├── adaptation_engine.py  # 参数校验 → 请求映射 → REST 调用 → 响应整形
│   │   ├── rest_connector.py     # httpx 异步 HTTP 客户端，支持幂等重试
│   │   ├── response_mapper.py    # 响应路径提取、字段白名单、文本模板
│   │   ├── error_mapper.py       # 统一错误分类与 JSON-RPC 错误码映射
│   │   ├── errors.py             # GatewayError 数据类
│   │   ├── auth.py               # API Key 鉴权（网关 Key + 管理 Key）
│   │   ├── rate_limit.py         # 滑动窗口限流器
│   │   └── session_manager.py    # MCP 会话管理（创建/初始化/过期清理/终止）
│   ├── db/                       # 持久化层
│   │   ├── database.py           # aiosqlite 初始化、WAL 模式、建表与迁移
│   │   └── repositories.py       # ConfigAuditRepository / AccessLogRepository
│   ├── models/                   # 数据模型
│   │   ├── jsonrpc.py            # JSON-RPC 2.0 请求/响应 Pydantic 模型
│   │   └── tool_config.py        # ToolConfig / ToolMeta / HttpTarget / 映射模型
│   └── utils/
│       └── logging.py            # 结构化 JSON 日志、请求 ID 上下文、敏感字段脱敏
├── configs/                      # 配置文件目录
│   ├── tools/                    # 工具配置（YAML/JSON，每文件一个工具）
│   │   ├── get_user.yaml         # 查询用户信息
│   │   ├── create_order.yaml     # 创建订单（嵌套 body 映射）
│   │   ├── get_baidu_suggestion.yaml  # 百度搜索建议（外部 API）
│   │   ├── probe_slow.yaml       # 慢响应探针
│   │   ├── probe_failure.yaml    # 失败探针
│   │   └── probe_retry.yaml      # 重试探针
│   └── openapi/
│       └── mock_api.yaml         # OpenAPI 3.x 文档（管理接口预览用）
├── static/
│   └── index.html                # 前端管理界面（仪表盘 + MCP 调试工具）
├── scripts/
│   ├── mock_rest_api.py          # 模拟下游 REST 服务 (:9001)
│   ├── run_experiments.py        # 自动化实验脚本
│   ├── llm_demo.py               # LLM 端到端演示（OpenAI 兼容 API + MCP 网关）
│   └── verify_dashboard.py       # Playwright 仪表盘截图验证
├── tests/                        # 测试套件（10 个文件，100+ 测试用例）
├── pyproject.toml                # 项目元数据与依赖
├── .env.example                  # 环境变量模板
└── HANDOFF.md                    # 项目交接文档
```

## 从零开始：手把手启动教程

> 以下以 **Windows** 环境为例，每一步都有预期输出，照着复制粘贴即可。Linux/macOS 用户看括号里的替代命令。

### 第 0 步：确认 Python 版本

打开终端（PowerShell / CMD / Terminal），输入：

```bash
python --version
```

预期输出类似 `Python 3.11.x` 或更高版本。如果提示找不到命令或版本低于 3.11，请先安装 Python（https://www.python.org/downloads/）。

### 第 1 步：进入项目目录

```bash
cd 你的项目路径\MCPgateway
```

确认当前目录下有 `pyproject.toml` 文件：

```bash
dir pyproject.toml
# Linux/macOS: ls pyproject.toml
```

### 第 2 步：创建并激活虚拟环境

```bash
python -m venv .venv
```

激活虚拟环境：

```bash
# Windows PowerShell
.venv\Scripts\Activate.ps1

# Windows CMD
.venv\Scripts\activate.bat

# Linux/macOS
source .venv/bin/activate
```

激活成功后，终端提示符前面会出现 `(.venv)` 标识。

> **常见问题**：PowerShell 报错"无法加载文件，因为在此系统上禁止运行脚本"？执行 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` 后重试。

### 第 3 步：安装依赖

```bash
pip install -e .[dev]
```

等待安装完成，看到 `Successfully installed ...` 字样即可。

### 第 4 步：创建配置文件

```bash
copy .env.example .env
# Linux/macOS: cp .env.example .env
```

默认配置无需修改，开箱即用。如需自定义，编辑 `.env` 文件（参见下方"环境变量"章节）。

### 第 5 步：启动服务（需要两个终端窗口）

**终端 1** — 启动模拟下游 REST API（端口 9001）：

```bash
cd 你的项目路径\MCPgateway
.venv\Scripts\Activate.ps1
python scripts/mock_rest_api.py
```

看到以下输出说明下游服务已就绪：

```
INFO:     Uvicorn running on http://127.0.0.1:9001 (Press CTRL+C to quit)
```

**终端 2** — 启动 MCP 网关（端口 8000）：

```bash
cd 你的项目路径\MCPgateway
.venv\Scripts\Activate.ps1
python -m app
```

看到以下输出说明网关已就绪：

```
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

### 第 6 步：验证启动成功

打开浏览器，依次访问：

| 地址 | 预期结果 |
|------|---------|
| http://127.0.0.1:8000/health | 返回 JSON，包含 `"status": "ok"` |
| http://127.0.0.1:8000/dashboard | 看到深色主题的管理界面 |

或者在新终端中使用 curl 验证：

```bash
curl http://127.0.0.1:8000/health
```

预期返回：

```json
{"status":"ok","gateway":{"status":"ready","version":"...","tool_count":6,...}}
```

### 第 7 步（可选）：运行实验脚本

在两个服务都运行的前提下，**打开第 3 个终端**：

```bash
cd 你的项目路径\MCPgateway
.venv\Scripts\Activate.ps1
python scripts/run_experiments.py
```

脚本执行完成后，项目目录下会生成：
- `experiment_report.json` — 完整实验数据
- `experiment_report.md` — 简要实验摘要

### 第 8 步（可选）：运行测试

```bash
pytest
```

预期所有测试通过，看到绿色的 `passed` 输出。

### 启动顺序速查

```
┌─────────────────────────────────────────────────────┐
│  终端 1                终端 2              终端 3     │
│  ────────              ────────            ────────  │
│  mock_rest_api.py  →   python -m app  →   实验/测试  │
│  (端口 9001)           (端口 8000)        (可选)     │
│                                                      │
│  ⚠ 顺序很重要：先启动终端 1，再启动终端 2              │
└─────────────────────────────────────────────────────┘
```

### 如何停止

在每个终端中按 `Ctrl + C` 即可停止对应服务。

## API 端点

| 端点 | 方法 | 鉴权 | 说明 |
|------|------|------|------|
| `/mcp` | POST | Gateway Key | MCP JSON-RPC 入口（`initialize`、`notifications/initialized`、`tools/list`、`tools/call`） |
| `/mcp` | DELETE | Session ID | 终止 MCP 会话（需 `mcp-session-id` 头） |
| `/health` | GET | 无 | 健康检查，返回工具注册表状态 |
| `/admin/reload` | POST | Admin Key | 手动触发配置热重载 |
| `/admin/rollback` | POST | Admin Key | 回滚到前一版本配置快照 |
| `/admin/tools` | GET | Admin Key | 查看已注册工具列表与 OpenAPI 预览工具 |
| `/admin/status` | GET | Admin Key | 运行状态、调用指标、审计日志、会话信息 |
| `/dashboard` | GET | 无 | 前端管理界面（仪表盘 + MCP 调试工具） |

默认 API Key：

- **Gateway Key**：`dev-api-key`（通过 `x-api-key` 请求头传递）
- **Admin Key**：`dev-admin-key`（通过 `x-api-key` 请求头传递）

## 核心功能

### 1. MCP 协议接入（JSON-RPC 2.0）

采用 HTTP POST 同步响应模式（Streamable HTTP 的合法子集），支持完整的 MCP 会话生命周期：

```
Client                          Gateway
  │                                │
  ├──POST /mcp {initialize}───────▶│  创建会话，返回 mcp-session-id
  │◀──200 {serverInfo, caps}───────┤
  │                                │
  ├──POST /mcp {notifications/     │  标记会话已初始化
  │    initialized}────────────────▶│
  │◀──202────────────────────────┤
  │                                │
  ├──POST /mcp {tools/list}────────▶│  获取工具列表
  │◀──200 {tools:[...]}───────────┤
  │                                │
  ├──POST /mcp {tools/call}────────▶│  调用工具 → 转发到下游 REST API
  │◀──200 {content, meta}─────────┤
  │                                │
  ├──DELETE /mcp───────────────────▶│  终止会话
  │◀──204────────────────────────┤
```

`initialize` 响应声明的能力（capabilities）：
- `tools.listChanged: true` — 支持工具列表变更通知
- `logging` — 支持日志

### 2. 配置驱动的工具注册

每个 REST API 封装为一个 YAML/JSON 配置文件，放入 `configs/tools/` 即可自动注册为 MCP 工具，无需编写代码。

工具配置结构示例（`configs/tools/get_user.yaml`）：

```yaml
tool_meta:
  name: get_user
  title: 查询用户信息
  description: 根据用户ID查询用户的基本信息，包括姓名、邮箱、部门和角色。
  version: "1.0.0"
  tags: ["用户", "查询"]

input_schema:
  type: object
  properties:
    user_id:
      type: integer
      description: Numeric user identifier.
    verbose:
      type: boolean
      description: Whether to request verbose output.
  required: ["user_id"]
  additionalProperties: false

http_target:
  method: GET
  base_url: http://127.0.0.1:9001
  path: /users/{user_id}
  timeout_seconds: 5
  retry_count: 1

request_mapping:
  path_map:
    user_id: user_id
  query_map:
    verbose: verbose

response_mapping:
  result_path: data
  field_whitelist: ["id", "name", "email", "role"]
  include_http_meta: true
  text_template: "用户 {name} 的邮箱是 {email}，角色为 {role}。"

error_mapping:
  status_map:
    "404": DOWNSTREAM_ERROR
    "500": DOWNSTREAM_ERROR
  default_code: DOWNSTREAM_ERROR
  expose_downstream_body: true
```

**配置各节说明**：

| 配置节 | 功能 |
|--------|------|
| `tool_meta` | 工具元数据：名称、标题、描述、版本、标签 |
| `input_schema` | JSON Schema 格式的输入参数定义，用于参数校验和 MCP 工具发现 |
| `http_target` | 下游 REST 目标：HTTP 方法、基础 URL、路径模板、超时、重试次数、幂等标记 |
| `request_mapping` | 参数映射规则：路径参数（`path_map`）、查询参数（`query_map`）、请求头（`header_map`）、请求体（`body_map`，支持嵌套点号路径）、常量头（`constant_headers`） |
| `response_mapping` | 响应整形：JSON 路径提取（`result_path`）、字段白名单（`field_whitelist`）、人类可读模板（`text_template`）、下游 HTTP 元数据附加（`include_http_meta`） |
| `error_mapping` | 错误映射：HTTP 状态码到错误分类（`status_map`）、默认错误码、是否暴露下游响应体 |

### 3. 请求适配与转换

`AdaptationEngine` 负责完整的请求/响应转换流水线：

1. **参数校验** — 使用 jsonschema 验证入参合法性
2. **请求构建** — 根据 `request_mapping` 将 MCP 参数映射到 REST 路径、查询、头部、请求体（支持嵌套 JSON body，如 `item.name` → `{"item": {"name": ...}}`）
3. **REST 调用** — 通过 `RestConnector`（httpx AsyncClient）发送请求
4. **响应整形** — 根据 `response_mapping` 提取、过滤、格式化响应数据
5. **访问日志** — 记录调用结果到 SQLite

### 4. 南向 REST 连接器

- 基于 httpx 的异步 HTTP 客户端
- **幂等重试**：GET/DELETE 方法自动标记为幂等，对传输错误和 5xx 响应自动重试
- 可配置超时时间和重试次数
- 记录实际重试次数（`attempts_made`）

### 5. 配置热重载与回滚

- **文件监听自动重载**：基于 watchfiles 监听 `configs/tools/` 目录变更，1 秒防抖后自动触发重载
- **手动重载**：`POST /admin/reload`
- **配置回滚**：`POST /admin/rollback`，内存中保留上一版本快照，active/previous 互换，支持反复切换
- **变更摘要**：每次重载/回滚记录工具的新增、删除、更新、未变更数量
- **审计日志**：所有配置变更事件持久化到 SQLite `config_audit` 表

### 6. MCP 会话管理

- `initialize` 时创建会话，返回 `mcp-session-id` 响应头
- 必须发送 `notifications/initialized` 才能调用 `tools/list` / `tools/call`
- 会话自动过期（默认 TTL 1800 秒），每 60 秒清理一次
- `DELETE /mcp` 主动终止会话
- 每次请求自动刷新会话活跃时间

### 7. 安全机制

- **双层 API Key 鉴权**：网关调用和管理接口使用独立 Key，通过 `hmac.compare_digest` 比对防时序攻击
- **滑动窗口限流**：按 `x-api-key` 或客户端 IP 隔离，响应头返回 `x-rate-limit-limit`、`x-rate-limit-remaining`、`x-rate-limit-reset`
- **敏感字段脱敏**：结构化日志自动脱敏 `authorization`、`api_key`、`x-api-key`、`token`、`password` 等字段

### 8. 可观测性

- **结构化 JSON 日志**：每条日志包含 `event`、`request_id` 等结构化字段
- **请求追踪**：中间件自动注入/传递 `x-request-id`，贯穿完整调用链
- **SQLite 持久化**：`access_log` 表记录每次工具调用（工具名、成功/失败、错误类型、下游状态码、延迟、重试次数），`config_audit` 表记录配置变更审计
- **管理状态接口**：`GET /admin/status` 聚合返回调用指标、审计摘要、近期调用与审计事件、会话统计、安全配置

### 9. OpenAPI 预览

将 `configs/openapi/` 目录下的 OpenAPI 3.x 文档自动解析为 MCP 工具预览，可通过 `GET /admin/tools` 查看，用于评估哪些 API 可快速接入。

### 10. 前端管理界面

访问 `/dashboard` 可使用内嵌的单页管理界面（深色主题），提供：

- **仪表盘**：网关状态、工具数量、调用指标、近期审计事件
- **工具管理**：查看已注册工具详情、OpenAPI 预览工具
- **操作按钮**：刷新数据、触发重载、触发回滚
- **MCP 调试工具**：可视化执行 MCP 协议流程（initialize → tools/list → tools/call）

### 11. 统一错误处理

所有错误归类为标准分类并映射到 JSON-RPC 错误码：

| 错误分类 | JSON-RPC Code | 说明 |
|----------|---------------|------|
| `PARSE_ERROR` | -32700 | JSON 解析失败 |
| `INVALID_REQUEST` | -32600 | 无效的 JSON-RPC 请求 |
| `TOOL_NOT_FOUND` | -32601 | 工具不存在 |
| `VALIDATION_ERROR` | -32602 | 参数校验失败 |
| `INTERNAL_ERROR` | -32603 | 网关内部错误 |
| `DOWNSTREAM_ERROR` | -32050 | 下游服务错误 |
| `TIMEOUT_ERROR` | -32060 | 下游服务超时 |
| `UNAUTHORIZED` | -32001 | 鉴权失败 |
| `RATE_LIMITED` | -32029 | 触发限流 |

工具执行类错误（DOWNSTREAM_ERROR、TIMEOUT_ERROR、INTERNAL_ERROR）以 `result + isError: true` 形式返回，使 MCP 客户端能区分协议错误与工具执行失败。

## 环境变量

项目启动时自动读取根目录 `.env` 文件；未设置的变量回退到内置默认值。

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `MCP_GATEWAY_HOST` | `127.0.0.1` | 服务监听地址 |
| `MCP_GATEWAY_PORT` | `8000` | 服务监听端口 |
| `MCP_GATEWAY_API_KEY` | `dev-api-key` | MCP 调用鉴权 Key |
| `MCP_GATEWAY_ADMIN_API_KEY` | `dev-admin-key` | 管理接口鉴权 Key |
| `MCP_GATEWAY_CONFIG_DIR` | `configs/tools` | 工具配置目录 |
| `MCP_GATEWAY_OPENAPI_DIR` | `configs/openapi` | OpenAPI 文档目录 |
| `MCP_GATEWAY_SQLITE_PATH` | `data/gateway.db` | SQLite 数据库路径 |
| `MCP_GATEWAY_LOG_LEVEL` | `INFO` | 日志级别 |
| `MCP_GATEWAY_RATE_LIMIT_WINDOW_SECONDS` | `60` | 限流时间窗口（秒） |
| `MCP_GATEWAY_RATE_LIMIT_MAX_REQUESTS` | `60` | 窗口内最大请求数 |
| `MCP_GATEWAY_SESSION_TTL_SECONDS` | `1800` | MCP 会话过期时间（秒） |
| `MCP_GATEWAY_NAME` | `mcp-smart-api-gateway` | 项目名称（显示于 MCP initialize 响应） |
| `MCP_GATEWAY_VERSION` | `0.1.0` | 项目版本 |

## 示例请求

> 以下 curl 命令使用 `\` 续行符（Linux/macOS/Git Bash）。Windows PowerShell 请将 `\` 替换为 `` ` ``，Windows CMD 请替换为 `^`。也可以将命令合并为一行使用。

### MCP 会话初始化

```bash
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "x-api-key: dev-api-key" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-03-26\",\"clientInfo\":{\"name\":\"my-client\",\"version\":\"1.0.0\"}}}"
```

响应示例（注意 `mcp-session-id` 响应头，后续请求需携带）：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2025-03-26",
    "serverInfo": {"name": "mcp-smart-api-gateway", "version": "0.1.0"},
    "capabilities": {"tools": {"listChanged": true}, "logging": {}}
  }
}
```

### 发送 notifications/initialized

```bash
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "x-api-key: dev-api-key" \
  -H "mcp-session-id: <上一步返回的 session id>" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}"
```

成功返回 HTTP 202（无响应体）。

### 获取工具列表

```bash
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "x-api-key: dev-api-key" \
  -H "mcp-session-id: <session id>" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\"}"
```

### 调用工具

```bash
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "x-api-key: dev-api-key" \
  -H "mcp-session-id: <session id>" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"get_user\",\"arguments\":{\"user_id\":1,\"verbose\":true}}}"
```

### 管理接口

```bash
# 查看网关状态
curl -H "x-api-key: dev-admin-key" http://127.0.0.1:8000/admin/status

# 查看已注册工具
curl -H "x-api-key: dev-admin-key" http://127.0.0.1:8000/admin/tools

# 手动重载配置
curl -X POST -H "x-api-key: dev-admin-key" http://127.0.0.1:8000/admin/reload

# 回滚到前一版本
curl -X POST -H "x-api-key: dev-admin-key" http://127.0.0.1:8000/admin/rollback
```

> **推荐**：使用 `/dashboard` 页面的 MCP 调试工具可以在浏览器中可视化完成上述全部操作，无需手动拼 curl。

## 实验脚本

先分别启动模拟下游服务和网关，然后运行实验脚本：

```bash
python scripts/mock_rest_api.py    # 终端 1
python -m app                      # 终端 2
python scripts/run_experiments.py  # 终端 3
```

脚本会自动执行以下实验并输出报告：

- `initialize`、`tools/list` 与多个 `tools/call` 功能验证
- 慢响应、下游 500、参数错误与幂等重试场景
- 多组并发请求压测
- 管理接口状态采集
- 临时新增工具并触发 `/admin/reload` 的热更新验证

输出文件：

- `experiment_report.json`：完整实验明细（适合统计分析和论文附录）
- `experiment_report.md`：简要实验摘要（适合整理到论文初稿）

## LLM 端到端演示

`scripts/llm_demo.py` 实现了完整的 LLM + MCP 网关闭环调用：

```
用户提问 → 大模型推理 → 工具选择 → MCP 网关 → 下游 REST API → 大模型生成回答
```

演示流程自动完成：
1. 从 MCP 网关发现可用工具（`tools/list`）
2. 将 MCP 工具格式转换为 OpenAI function calling 格式
3. 用户输入问题后，大模型自主决定是否调用工具
4. 工具调用通过 MCP 网关转发到下游 REST API
5. 大模型根据工具返回结果生成自然语言回答

前提条件：需要先启动模拟下游服务和 MCP 网关（参见"从零开始"教程的第 5 步）。

> **注意**：此演示脚本采用简化的 JSON-RPC 调用方式（直接发送 `tools/list` / `tools/call`），未实现完整的 MCP 会话握手流程（`initialize` + `notifications/initialized`）。这是为了演示 LLM 工具调用闭环的概念验证脚本，生产环境应使用完整的会话流程。

运行方式：

```bash
# 设置 OpenAI API Key（支持任何 OpenAI 兼容 API）
# PowerShell
$env:OPENAI_API_KEY = "sk-xxx"
# 可选：自定义 API 地址和模型
$env:OPENAI_BASE_URL = "https://api.openai.com/v1"
$env:OPENAI_MODEL = "gpt-4o-mini"

python scripts/llm_demo.py
```

## 测试

```bash
pytest
```

测试套件包含 100+ 个测试用例，覆盖：

| 测试文件 | 覆盖范围 |
|----------|----------|
| `test_gateway.py` | 完整 MCP 协议流程、管理接口、鉴权、回滚、健康检查 |
| `test_runtime_features.py` | 回滚恢复、工具删除重载、审计记录 |
| `test_session_manager.py` | 会话创建/过期/初始化/终止/清理 |
| `test_adaptation_engine.py` | 重试、映射、校验失败、缺失参数 |
| `test_config_registry.py` | 重复工具名检测、回滚、变更摘要 |
| `test_response_error_mapper.py` | 白名单、模板、HTTP 元数据、错误映射 |
| `test_security_components.py` | Key 验证、空 Key 跳过、多客户端隔离限流 |
| `test_sqlite_repositories.py` | 指标聚合、空库、limit 参数 |
| `test_models.py` | 模型构造、版本校验、幂等推断、mcp_tool_schema |
| `test_experiment_scripts.py` | 实验脚本纯函数 |

## 核心模块对应关系

| 功能模块 | 代码文件 |
|---------|---------|
| 协议接入 | `app/api/mcp_routes.py`、`app/core/mcp_service.py`、`app/models/jsonrpc.py` |
| 工具注册与配置管理 | `app/core/tool_registry.py`、`app/core/config_loader.py`、`app/core/schema_generator.py`、`app/models/tool_config.py` |
| 请求转换执行 | `app/core/adaptation_engine.py`、`app/core/response_mapper.py` |
| 南向连接 | `app/core/rest_connector.py` |
| 运行保障 | `app/core/auth.py`、`app/core/rate_limit.py`、`app/core/error_mapper.py`、`app/core/session_manager.py`、`app/utils/logging.py`、`app/db/`、`app/api/admin_routes.py`、`app/api/health_routes.py` |

## 依赖

运行时依赖（见 `pyproject.toml`）：

| 包 | 用途 |
|----|------|
| `fastapi` | Web 框架 |
| `uvicorn[standard]` | ASGI 服务器 |
| `pydantic` | 数据模型与校验 |
| `httpx` | 异步 HTTP 客户端 |
| `aiosqlite` | SQLite 异步驱动 |
| `PyYAML` | YAML 配置解析 |
| `jsonschema` | 工具输入参数校验 |
| `watchfiles` | 配置文件变更监听 |

开发依赖：`pytest`、`pytest-asyncio`

## License

本项目为本科毕业设计作品。
