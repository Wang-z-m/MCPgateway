# 智能工具发现与渐进式披露 — 设计与实现文档

## 1. 文档目的

本文档用于定义并指导 `MCP Smart API Gateway` 的"智能工具发现与渐进式披露"能力落地。

核心机制：网关注入一个名为 `search_apis` 的系统内置 Meta-Tool，LLM 在对话中通过标准 `tools/call` 调用它来触发 TF-IDF 语义检索，从全量工具库中发现最相关的 API。`tools/list` 仅返回核心高频工具 + `search_apis`，实现渐进式披露。

---

## 2. 背景与问题定义

### 2.1 现状

当前网关 `tools/list` 返回注册表中的全部工具。当工具数量较少时这是合理的，但随着工具规模增长，这一行为会成为瓶颈。

### 2.2 问题

当工具数量增加后，扁平工具列表会导致：

- LLM 接收过多工具描述，消耗大量 token
- 工具选择准确率随数量增长而下降
- 无法按业务领域或重要性分层展示

### 2.3 关键约束：MCP 客户端的实际行为

在标准 MCP 生态中（如 Claude Desktop），客户端仅在初始化阶段静默调用一次无参数的 `tools/list`，LLM 在后续对话中**不会**主动再次调用 `tools/list`。LLM 只通过 `tools/call` 与服务端交互。

因此，任何依赖"LLM 在对话中调用 `tools/list(query=...)` 触发检索"的设计在实际运行中无法生效。智能发现的入口必须是 `tools/call`。

### 2.4 方案定位

在遵循标准 MCP 协议调用模式的前提下，实现智能工具发现：

- 将发现能力包装为 `search_apis` Meta-Tool，LLM 通过标准 `tools/call` 主动触发
- `tools/list` 仅返回 `tier="primary"` 的核心工具 + 注入的 `search_apis`，实现渐进式披露
- 语义检索基于 TF-IDF + jieba 中文分词，纯本地计算，无外部依赖
- 工具选择的最终决策权保留在 LLM 侧
- `search_apis` 响应中附加 `structuredContent` 扩展字段供高级客户端解析，该字段不属于 MCP 2025-03-26 核心规范，但不影响标准客户端对 `content` 的消费

---

## 3. 设计目标与非目标

### 3.1 设计目标

1. **语义智能**：基于 TF-IDF 信息检索技术，根据自然语言查询对工具进行语义相关性评分与排序。
2. **渐进式披露**：`tools/list` 首次仅暴露核心工具 + 发现入口；LLM 按需通过 `search_apis` 获取更多工具。
3. **协议合规**：核心交互完全基于标准 MCP `tools/list` 和 `tools/call`，不依赖任何非标准客户端行为。响应中的 `structuredContent` 扩展字段为可选增强，不影响标准客户端消费。
4. **可解释**：`search_apis` 返回的每个工具附带 `relevance_score`，LLM 和开发者可理解排序依据。
5. **中文友好**：集成 jieba 分词，正确处理中文工具描述与中文查询。
6. **安全降级**：当匹配结果为空时，降级返回 primary 工具列表，确保 LLM 不会拿到空结果。
7. **防滥用**：对 `search_apis` 施加独立限流，防止 LLM 陷入检索死循环。
8. **可观测**：记录发现请求日志，统计查询命中率与平均候选数。

### 3.2 非目标（本阶段不做）

- 不实现基于深度学习 Embedding 的语义检索（如 sentence-transformers）
- 不实现网关侧自动选择并执行工具（决策权保留给 LLM）
- 不实现工具推荐的个性化（基于历史调用偏好）
- 不实现调用失败时的替代工具推荐
- 不实现大 Schema 动态截断（当前工具规模下无此需求）

---

## 4. 总体架构

### 4.1 逻辑分层

1. **MCP 协议层**：`/mcp` 接口处理 `tools/list` 和 `tools/call`。
2. **Meta-Tool 拦截层**（新增）：`McpService` 识别 `search_apis` 调用并路由到发现引擎。
3. **发现决策层**（新增）：`ToolDiscoveryEngine` 执行 TF-IDF 语义评分与过滤。
4. **执行层**（复用）：`AdaptationEngine` 处理普通业务工具调用。
5. **注册层**（复用）：`ToolRegistry` 提供工具快照与元数据。

### 4.2 核心数据流

**初始化阶段**（`tools/list`）：

```
MCP Client -> tools/list (无参数)
           -> McpService
                ├── 从 ToolRegistry 获取 tier="primary" 的工具
                ├── 注入 search_apis Meta-Tool 定义
                └── 返回 [primary 工具 + search_apis]
           -> LLM 缓存工具列表
```

**发现阶段**（`tools/call search_apis`）：

```
LLM -> tools/call(name="search_apis", arguments={query: "查询用户信息"})
    -> McpService 识别 Meta-Tool，拦截
    -> ToolDiscoveryEngine.search(query, category?, top_k?)
        ├── 1. 分类过滤 (category)
        ├── 2. TF-IDF 语义评分 (query)
        ├── 3. 阈值过滤 + Top-K 截断
        └── 4. 空结果降级 → 返回 primary 工具摘要
    -> 返回匹配工具的名称、描述、参数 Schema（作为 MCP content）
    -> LLM 阅读结果，决定调用哪个工具
```

**调用阶段**（`tools/call 业务工具`）：

```
LLM -> tools/call(name="get_user", arguments={user_id: 1})
    -> McpService 识别为普通工具
    -> AdaptationEngine -> RestConnector -> 下游 REST（不变）
```

### 4.3 与现有架构的关系

```
                              McpService
                           /      |       \
              tools/list      tools/call     tools/call
             (primary +     (search_apis)   (业务工具)
             search_apis)       |                |
                          ToolDiscovery     AdaptationEngine
                            Engine               |
                               |            RestConnector
                          ToolRegistry
                               |
                          ConfigLoader
```

核心原则：`search_apis` 的拦截发生在 `McpService` 内部，**在 `AdaptationEngine` 之前**。普通业务工具的执行链路完全不受影响。

---

## 5. 核心对象与配置设计

### 5.1 工具配置扩展

在现有 `ToolMeta` 模型中新增两个可选字段：

```yaml
tool_meta:
  name: get_user
  title: 查询用户信息
  description: 根据用户ID查询用户的基本信息，包括姓名、邮箱、部门和角色。
  version: "1.0.0"
  tags: ["用户", "查询"]
  category: "user_management"   # 新增：业务分类
  tier: "primary"               # 新增：层级
```

#### 分类（category）

业务领域标识，用于 `search_apis` 的可选过滤条件。值为自由字符串，由工具配置者自行定义：

- `user_management`：用户管理
- `order`：订单相关
- `finance`：财务相关
- `system`：系统运维
- 自定义扩展...

未配置时默认 `null`，表示不属于任何特定分类。分类过滤时，仅返回 category 与请求值**大小写敏感**精确匹配的工具（即 `"User_Management"` 与 `"user_management"` 视为不同分类）；`category=null` 的工具**不会**被包含在任何分类过滤结果中。不传 `category` 参数则搜索全部工具。建议工具配置者统一使用 `snake_case` 命名分类。

#### 层级（tier）

工具重要性/使用频率标识，用于渐进式披露：

| 层级 | 含义 | 行为 |
|------|------|------|
| `primary` | 核心高频工具 | 出现在 `tools/list` 初始响应中，LLM 可直接调用 |
| `secondary` | 辅助工具 | 不出现在初始列表中，需通过 `search_apis` 发现后才能调用 |
| `utility` | 低频/调试工具 | 同上，仅在精确查询时被检索到 |

未配置时默认 `"primary"`。

### 5.2 `search_apis` Meta-Tool 定义

`search_apis` 是网关内置的虚拟工具，不对应任何 YAML 配置文件或下游 REST API。它由 `McpService` 在 `tools/list` 响应中自动注入。

#### 工具 Schema（暴露给 LLM）

```json
{
  "name": "search_apis",
  "title": "搜索 API 工具库",
  "description": "【系统内置工具】当现有的核心 API 无法满足用户需求时，请调用此工具。传入用户的自然语言需求，系统将从企业 API 库中检索并返回最相关的 API 工具名称、描述和参数格式。注意：最多尝试 3 次不同的关键词。如果 3 次后仍未找到合适工具，请停止搜索并告知用户当前系统不支持该功能。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "用自然语言描述需要的 API 功能，例如"查询用户邮箱"或"创建订单""
      },
      "category": {
        "type": "string",
        "description": "可选：按业务分类过滤，如 user_management、order、finance"
      },
      "top_k": {
        "type": "integer",
        "description": "可选：最多返回几个工具，默认 5",
        "minimum": 1,
        "maximum": 20
      }
    },
    "required": ["query"],
    "additionalProperties": false
  }
}
```

#### Prompt Engineering 要点

工具描述中的关键设计：

1. **"当现有的核心 API 无法满足用户需求时"**：引导 LLM 先尝试已有工具，只在不够用时才搜索。
2. **"最多尝试 3 次"**：Prompt 级熔断，减少无效检索（LLM 不一定遵守，但有引导作用）。
3. **"传入用户的自然语言需求"**：明确告诉 LLM 应该传什么样的 query。

### 5.3 发现引擎配置

在 `app/settings.py` 的 `Settings` 中新增发现相关参数（通过环境变量配置）：

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `MCP_GATEWAY_DISCOVERY_DEFAULT_TOP_K` | `5` | `search_apis` 默认返回工具数 |
| `MCP_GATEWAY_DISCOVERY_MAX_TOP_K` | `20` | top_k 最大允许值 |
| `MCP_GATEWAY_DISCOVERY_SCORE_THRESHOLD` | `0.05` | 最低相关性评分阈值 |
| `MCP_GATEWAY_DISCOVERY_TFIDF_MAX_FEATURES` | `5000` | TF-IDF 最大特征数 |
| `MCP_GATEWAY_DISCOVERY_FALLBACK_ON_EMPTY` | `true` | 空结果时是否降级返回 primary 工具摘要 |
| `MCP_GATEWAY_DISCOVERY_RATE_LIMIT_MAX` | `10` | `search_apis` 每窗口最大调用次数 |
| `MCP_GATEWAY_DISCOVERY_RATE_LIMIT_WINDOW` | `60` | `search_apis` 限流窗口（秒） |

---

## 6. TF-IDF 语义评分算法设计

### 6.1 核心思路

将每个工具的文本特征（名称、标题、描述、标签）合并为一个"文档"，使用 TF-IDF 向量化后与用户查询计算余弦相似度，以此作为相关性评分。

### 6.2 中文分词

使用 jieba 分词库进行中文分词。自定义 analyzer 函数传入 scikit-learn 的 `TfidfVectorizer`。

**重要**：当 `TfidfVectorizer` 接收自定义 `analyzer` 时，`ngram_range` 等参数会被忽略（scikit-learn 的设计行为）。因此 bigram 必须在 analyzer 内部自行生成：

```python
import re
import jieba

_WORD_RE = re.compile(r"\w")

def chinese_analyzer(text: str) -> list[str]:
    raw_tokens = jieba.cut(text)
    tokens = [t for t in raw_tokens if _WORD_RE.search(t)]
    bigrams = [f"{tokens[i]}{tokens[i+1]}" for i in range(len(tokens) - 1)]
    return tokens + bigrams
```

`_WORD_RE.search(t)` 过滤掉空白和标点 token（如 `"，"` `"、"` `" "`），避免生成无意义的 bigram。

分词效果示例：
- `"查询用户信息"` → `["查询", "用户", "信息", "查询用户", "用户信息"]`
- `"根据用户ID查询基本信息，包括姓名、邮箱"` → tokens: `["根据", "用户", "ID", "查询", "基本", "信息", "包括", "姓名", "邮箱"]`，bigrams: `["根据用户", "用户ID", "ID查询", "查询基本", "基本信息", "信息包括", "包括姓名", "姓名邮箱"]`（标点 `"，"` `"、"` 已被过滤）
- `"创建订单"` → `["创建", "订单", "创建订单"]`
- `"get_user"` → `["get_user"]`（英文保留完整 token）

### 6.3 工具文档构建

每个工具生成一个文本文档用于索引：

```python
def build_document(tool: ToolConfig) -> str:
    meta = tool.tool_meta
    parts = [
        meta.name,
        meta.name,       # 名称重复一次，增加名称匹配权重
        meta.title,
        meta.description,
        " ".join(meta.tags),
    ]
    if meta.category:
        parts.append(meta.category)
    return " ".join(parts)
```

名称重复出现一次以提供名称匹配的权重加成。

**语言一致性要求**：TF-IDF 基于精确词汇匹配计算相似度，不具备跨语言能力。工具的 `title` 和 `description` 必须使用与预期查询相同的语言书写。本项目面向中文用户，因此工具描述应使用中文。工具的 `name` 字段（如 `get_user`）保持英文，因为它是程序标识符而非自然语言描述。

**索引范围**：TF-IDF 索引包含**全部**注册工具（所有 tier），不仅限于 primary。这样 `search_apis` 可以检索到 secondary 和 utility 层级的工具。

### 6.4 TF-IDF 索引构建

使用 scikit-learn 的 `TfidfVectorizer`：

```python
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

vectorizer = TfidfVectorizer(
    analyzer=chinese_analyzer,
    max_features=5000,
    sublinear_tf=True,
)
tfidf_matrix = vectorizer.fit_transform(tool_documents)
```

**参数说明**：

| 参数 | 值 | 作用 |
|------|---|------|
| `analyzer` | `chinese_analyzer` | 使用 jieba 自定义分词 + bigram 生成（传入自定义 analyzer 后 `ngram_range` 等参数不生效，因此 bigram 由 analyzer 内部生成） |
| `max_features` | `5000` | 限制词汇表大小，防止内存膨胀 |
| `sublinear_tf` | `True` | 使用 `1+log(tf)` 平滑，避免高频词过度主导 |

**索引时机**：

- 启动时 `load_initial_snapshot()` 后构建
- 每次 `reload()` / `rollback()` 后重建
- 采用"先构建新索引，再原子替换引用"策略，避免并发查询读到半成品索引

### 6.5 查询评分

```python
query_vector = vectorizer.transform([query])
scores = cosine_similarity(query_vector, tfidf_matrix).flatten()
```

返回值为 `[0.0, 1.0]` 区间的相关性分数。

### 6.6 评分特性

| 特性 | 说明 |
|------|------|
| 中文支持 | jieba 分词，正确切分"查询用户"为"查询"+"用户" |
| 标点过滤 | `_WORD_RE.search(t)` 在 bigram 生成前过滤空白与标点 token，避免产生无意义特征 |
| Bigram | 由 `chinese_analyzer` 内部基于过滤后的 token 列表生成，捕获"查询用户""创建订单"等短语 |
| 子线性 TF | `sublinear_tf=True` 避免高频词过度主导 |
| 名称加权 | 工具名重复出现，提升精确名称匹配的权重 |

---

## 7. 详细流程设计

### 7.1 `tools/list` 流程（初始化阶段）

1. MCP 客户端在初始化完成后调用 `tools/list`（无参数）。
2. `McpService` 从 `ToolRegistry` 获取全部工具，过滤出 `tier="primary"` 的工具。
3. 将 `search_apis` Meta-Tool 的 schema 追加到列表末尾。
4. 返回 `[primary 工具 + search_apis]`。

LLM 缓存此列表，在后续对话中可直接调用 primary 工具或 `search_apis`。

### 7.2 `search_apis` 调用流程（发现阶段）

1. LLM 判断现有 primary 工具无法满足需求，调用 `tools/call(name="search_apis", arguments={query: "..."})`。
2. `McpService` 识别 `name="search_apis"`，拦截请求，**不转发给 `AdaptationEngine`**。
3. 检查 `search_apis` 独立限流计数器。若超限，返回限流错误。
4. 提取参数 `query`（必填）、`category`（可选）、`top_k`（可选，默认 5）。
5. 调用 `ToolDiscoveryEngine.search(query, category, top_k)`：
   a. 若指定 `category`，先从全量工具中过滤出该分类的工具。
   b. 使用 TF-IDF 计算各工具与 `query` 的相关性评分。
   c. 过滤掉低于 `score_threshold` 的工具。
   d. 按评分降序排列，截取 `top_k` 个。
   e. **若结果为空且 `fallback_on_empty=true`，降级返回 primary 工具摘要**。
6. 将匹配结果格式化为 MCP `content`（包含工具名称、描述、参数 Schema、相关性评分）。
7. 返回标准 `tools/call` 成功响应。

### 7.3 业务工具调用流程（执行阶段，不变）

1. LLM 通过 `search_apis` 发现了目标工具（如 `get_user`），发起 `tools/call(name="get_user", arguments={...})`。
2. `McpService` 识别 `name != "search_apis"`，走正常的 `tools/call` 分支。
3. `ToolRegistry.get_tool()` 查找工具（从全量注册表中查找，不限 tier）。
4. `AdaptationEngine.execute_tool()` → `RestConnector` → 下游 REST。
5. 响应映射后返回。

**重要**：`tools/call` 对业务工具的调用**不限制 tier**。即使某工具不在 `tools/list` 的初始响应中（如 secondary/utility 工具），只要 LLM 知道它的名字（通过 `search_apis` 获得），就可以直接调用。

---

## 8. 响应格式设计

### 8.1 `tools/list` 响应（初始化阶段）

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "tools": [
      {
        "name": "get_user",
        "title": "查询用户信息",
        "description": "根据用户ID查询用户的基本信息，包括姓名、邮箱、部门和角色。",
        "inputSchema": { "..." : "..." },
        "annotations": {
          "version": "1.0.0",
          "tags": ["用户", "查询"]
        }
      },
      {
        "name": "create_order",
        "title": "创建订单",
        "description": "在下游系统中创建一笔新订单，需提供商品名称、金额和客户姓名。",
        "inputSchema": { "..." : "..." },
        "annotations": { "..." : "..." }
      },
      {
        "name": "search_apis",
        "title": "搜索 API 工具库",
        "description": "【系统内置工具】当现有的核心 API 无法满足用户需求时，请调用此工具。...",
        "inputSchema": { "..." : "..." },
        "annotations": {
          "version": "1.0.0",
          "tags": ["system", "discovery"]
        }
      }
    ]
  }
}
```

仅包含 `tier="primary"` 的业务工具 + `search_apis`。

### 8.2 `search_apis` 调用结果（发现阶段）

`search_apis` 的返回基于 MCP `tools/call` 结果格式，核心信息通过标准 `content` 字段传递，同时附加 `structuredContent` 扩展字段提供机器可解析的结构化数据（`structuredContent` 不属于 MCP 2025-03-26 核心规范，标准客户端会忽略该字段，不影响功能）：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "找到 2 个相关 API 工具：\n\n1. get_user (相关度: 0.82)\n   描述: 根据用户ID查询用户的基本信息，包括姓名、邮箱、部门和角色。\n   参数: {\"user_id\": \"integer (必填)\", \"verbose\": \"boolean\"}\n\n2. update_user_email (相关度: 0.65)\n   描述: 修改指定用户的邮箱地址。\n   参数: {\"user_id\": \"integer (必填)\", \"new_email\": \"string (必填)\"}"
      }
    ],
    "structuredContent": {
      "matched_tools": [
        {
          "name": "get_user",
          "title": "查询用户信息",
          "description": "根据用户ID查询用户的基本信息，包括姓名、邮箱、部门和角色。",
          "relevance_score": 0.82,
          "inputSchema": { "..." : "..." }
        },
        {
          "name": "update_user_email",
          "title": "修改用户邮箱",
          "description": "修改指定用户的邮箱地址。",
          "relevance_score": 0.65,
          "inputSchema": { "..." : "..." }
        }
      ],
      "query": "查询用户信息",
      "total_indexed": 20,
      "category_filter": null,
      "fallback_triggered": false
    }
  }
}
```

**格式设计要点**：

- `content[0].text`：人类/LLM 可读的纯文本摘要，包含工具名、描述和简化参数格式。LLM 据此决定调用哪个工具。这是 MCP 标准字段，所有客户端均可消费。
- `structuredContent`：机器可解析的结构化数据，包含完整 `inputSchema`，供高级客户端使用。此字段为本项目的扩展（非 MCP 2025-03-26 核心规范），标准客户端会忽略多余字段，不影响正常交互。

### 8.3 `search_apis` 降级响应（无匹配结果）

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "未找到与"视频转码"相关的 API 工具。当前系统可能不支持此功能。\n\n以下是当前可用的核心 API：\n- get_user: 查询用户信息\n- create_order: 创建订单\n- get_baidu_suggestion: 百度搜索建议"
      }
    ],
    "structuredContent": {
      "matched_tools": [],
      "query": "视频转码",
      "total_indexed": 20,
      "category_filter": null,
      "fallback_triggered": true,
      "primary_tools": ["get_user", "create_order", "get_baidu_suggestion"]
    }
  }
}
```

降级时列出 primary 工具名和描述作为兜底，帮助 LLM 回归已知能力范围。

### 8.4 `search_apis` 限流响应

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "[RATE_LIMITED] API 搜索调用过于频繁，请稍后再试。如果多次搜索未找到合适工具，当前系统可能不支持该功能，请直接告知用户。"
      }
    ],
    "isError": true
  }
}
```

限流错误以 `isError: true` 的 MCP result 返回（而非 JSON-RPC error），使 LLM 能读到错误消息并据此停止重试。

---

## 9. 检索死循环防御设计

### 9.1 问题场景

用户提出系统不支持的需求时，LLM 可能反复调用 `search_apis`，不断更换关键词重试，陷入死循环。这会消耗大量 token 和算力。

### 9.2 双层防御

#### 第一层：Prompt 级引导（软限制）

在 `search_apis` 的工具描述中嵌入行为约束：

> "注意：最多尝试 3 次不同的关键词。如果 3 次后仍未找到合适工具，请停止搜索并告知用户当前系统不支持该功能。"

LLM 不一定遵守，但有统计层面的引导效果，成本为零。

#### 第二层：网关级限流（硬限制）

为 `search_apis` 设置独立的滑动窗口限流，与全局限流分开：

- **限流维度**：按 client key（`x-api-key` 或客户端 IP）隔离
- **默认配置**：每 60 秒最多 10 次
- **超限行为**：返回 `isError: true` 的限流提示（见 8.4 节），而非 JSON-RPC error，确保 LLM 能理解并停止
- **实现**：复用现有 `SlidingWindowRateLimiter` 的逻辑，`McpService` 在路由到发现引擎前检查

### 9.3 无匹配时的自然终止

`search_apis` 在结果为空时返回明确的"系统不支持"提示（见 8.3 节降级响应），帮助 LLM 自行判断应该停止搜索。这是第三道非强制性防线。

---

## 10. 索引并发安全设计

工具热重载/回滚时需要重建 TF-IDF 索引。采用**"构建新索引，原子替换引用"**策略：

```python
class ToolDiscoveryEngine:
    def rebuild_index(self, tools: list[ToolConfig]) -> None:
        # 1. 在临时变量中构建新索引
        new_vectorizer = TfidfVectorizer(analyzer=chinese_analyzer, ...)
        new_matrix = new_vectorizer.fit_transform(documents)
        new_tool_list = list(tools)

        # 2. 原子替换引用（Python 的引用赋值是原子的）
        self._vectorizer = new_vectorizer
        self._tfidf_matrix = new_matrix
        self._indexed_tools = new_tool_list

    def search(self, query: str, ...) -> ...:
        # 查询开头先将引用读到局部变量，确保本次查询全程使用同一版本索引
        vectorizer = self._vectorizer
        matrix = self._tfidf_matrix
        tools = self._indexed_tools
        # 后续全部使用局部变量 vectorizer / matrix / tools
        ...
```

查询时先将 `self._vectorizer` / `self._tfidf_matrix` / `self._indexed_tools` 读到局部变量，后续全程使用局部变量。这样即使查询执行中途发生了索引重建（async 上下文中经过 await 让出控制权），本次查询也始终使用同一版本的完整索引。

---

## 11. 代码改造清单

### 11.1 新增文件

| 文件 | 职责 |
|------|------|
| `app/core/discovery_engine.py` | `ToolDiscoveryEngine` 类：索引构建、TF-IDF 评分、分类过滤、结果格式化、发现主逻辑 |
| `app/models/discovery_models.py` | `SearchApisParams`（search_apis 参数模型）、`SearchResult`（检索结果模型） |
| `tests/test_discovery_engine.py` | 发现引擎单元测试与集成测试 |

### 11.2 修改文件

| 文件 | 修改内容 | 影响范围 |
|------|---------|---------|
| `app/models/tool_config.py` | `ToolMeta` 新增 `category: str \| None = None` 和 `tier: str = "primary"` | 模型层 |
| `app/core/mcp_service.py` | ① `tools/list` 分支改为：仅返回 primary 工具 + 注入 `search_apis` schema。② `tools/call` 分支新增：识别 `name="search_apis"` 并**在 `get_tool()` 之前**拦截路由到 `ToolDiscoveryEngine`，其余工具走原流程。③ 构造函数新增 `discovery_engine` 和 `discovery_rate_limiter` 参数。④ `handle()` 方法签名新增可选参数 `client_key: str \| None = None`，用于 `search_apis` 独立限流的按客户端隔离。⑤ 对 `search_apis` 参数的 Pydantic `ValidationError` 进行显式捕获，转换为 `VALIDATION_ERROR`（-32602）而非默认的 `INTERNAL_ERROR`（-32603）。 | 协议层（核心改动） |
| `app/core/tool_registry.py` | `load_initial_snapshot()` / `reload()` / `rollback()` 成功后通知 `ToolDiscoveryEngine` 重建索引；新增 `list_primary_tools()` 辅助方法 | 注册层 |
| `app/main.py` | lifespan 中按以下顺序初始化发现相关组件（详见 11.4 节） | 应用入口 |
| `app/settings.py` | 新增发现相关配置字段（含 `search_apis` 限流配置）；**注意 `with_base_dir()` 方法逐字段拷贝，新增字段必须同步添加** | 配置层 |
| `app/api/mcp_routes.py` | `mcp_endpoint` 中提取的 `client_key` 作为新参数传入 `McpService.handle()`，使 `McpService` 内部能对 `search_apis` 执行按客户端隔离的独立限流 | 路由层（最小改动） |
| `app/api/admin_routes.py` | `/admin/status` 新增发现统计输出（可选） | 管理接口 |
| `pyproject.toml` | 新增 `scikit-learn` 和 `jieba` 依赖 | 依赖管理 |
| `configs/tools/*.yaml` | 现有工具补充 `category` 和 `tier` 字段 | 工具配置 |

### 11.3 不修改的文件

以下文件**不需要任何改动**，确保执行链路零影响：

- `app/core/adaptation_engine.py`
- `app/core/rest_connector.py`
- `app/core/response_mapper.py`
- `app/core/error_mapper.py`
- `app/core/auth.py`
- `app/core/rate_limit.py`（复用逻辑，但不修改源码；`search_apis` 限流器在 `main.py` 中另外实例化）
- `app/core/session_manager.py`

### 11.4 初始化顺序与依赖装配

`ToolDiscoveryEngine` 需要从 `ToolRegistry` 获取工具列表来构建 TF-IDF 索引，而 `ToolRegistry` 需要在 `reload()` / `rollback()` 后通知 `ToolDiscoveryEngine` 重建索引。为避免循环依赖，采用**后绑定**模式：

```python
# main.py lifespan 中的初始化顺序：

# 1. 创建 ToolRegistry（此时不持有 discovery_engine 引用）
registry = ToolRegistry(config_loader, config_audit)
await registry.load_initial_snapshot()

# 2. 创建 ToolDiscoveryEngine，用当前工具快照构建初始索引
discovery_engine = ToolDiscoveryEngine(app_settings)
discovery_engine.rebuild_index(registry.list_tools())

# 3. 后绑定：将 discovery_engine 注入 registry
#    ToolRegistry 持有 Optional[ToolDiscoveryEngine]，
#    reload() / rollback() 成功后检查该引用是否存在再调用 rebuild_index()
registry.set_discovery_engine(discovery_engine)

# 4. 创建 search_apis 独立限流器
discovery_rate_limiter = SlidingWindowRateLimiter(
    app_settings.discovery_rate_limit_max,
    app_settings.discovery_rate_limit_window,
)

# 5. 创建 McpService，注入 discovery_engine 和独立限流器
mcp_service = McpService(
    registry,
    adaptation_engine,
    discovery_engine=discovery_engine,
    discovery_rate_limiter=discovery_rate_limiter,
    server_name=app_settings.project_name,
    server_version=app_settings.project_version,
)
```

**关键设计**：

- `ToolRegistry` 内部持有 `_discovery_engine: ToolDiscoveryEngine | None = None`，通过 `set_discovery_engine()` 方法注入
- `load_initial_snapshot()` 执行时 `_discovery_engine` 尚未绑定（为 `None`），初始索引构建在步骤 2 中由 `main.py` 显式完成
- 后续的 `reload()` / `rollback()` 在成功切换快照后，检查 `_discovery_engine is not None`，若存在则调用 `rebuild_index()`
- 这样保证依赖方向为单向：`ToolRegistry` → `ToolDiscoveryEngine`（可选依赖），无循环

---

## 12. 错误处理设计

| 场景 | 处理方式 |
|------|---------|
| `search_apis` 缺少 `query` 参数 | `McpService` 显式捕获 `SearchApisParams` 的 Pydantic `ValidationError`，转换为 `VALIDATION_ERROR` 类别（JSON-RPC -32602），而非由通用 `Exception` 处理器返回 `INTERNAL_ERROR`（-32603） |
| `search_apis` 的 `query` 为空字符串 | 同上，`SearchApisParams.query` 设置 `min_length=1`，空字符串触发 `ValidationError` 后转为 -32602 |
| `search_apis` 超过独立限流 | 返回 `isError: true` 的 MCP result，附带限流提示（见 8.4 节） |
| `category` 不存在 | 返回空匹配列表（不报错） |
| `top_k` 超出范围 | 截断到 `max_top_k`，不报错 |
| TF-IDF 索引未就绪 | 降级返回 primary 工具摘要 |
| 所有工具评分低于阈值 | 若 `fallback_on_empty=true`，降级返回 primary 工具摘要 |
| 发现引擎内部异常 | 捕获后降级返回 primary 工具摘要，记录错误日志 |
| `tools/call` 调用不存在的工具（幻觉/热更新） | 现有 `TOOL_NOT_FOUND` 错误，附带 `available_tools` |

---

## 13. 观测与日志设计

### 13.1 结构化日志

每次 `search_apis` 调用记录：

```json
{
  "event": "search_apis_called",
  "request_id": "abc123",
  "query": "查询用户",
  "category_filter": null,
  "top_k": 5,
  "total_indexed": 20,
  "matched_tools": 3,
  "top_score": 0.82,
  "fallback_triggered": false,
  "rate_limited": false,
  "latency_ms": 2
}
```

### 13.2 管理接口指标（`/admin/status` 扩展，可选）

```json
{
  "discovery": {
    "total_searches": 156,
    "avg_matched_tools": 3.2,
    "avg_top_score": 0.65,
    "fallback_count": 12,
    "rate_limited_count": 3,
    "index_tool_count": 20,
    "last_index_built_at": "2026-04-14T10:30:00Z"
  }
}
```

---

## 14. 测试方案

### 14.1 单元测试

1. **TF-IDF 索引构建**：
   - 索引包含全部 tier 的工具
   - 工具列表变更后索引正确重建
   - 空工具列表不抛异常

2. **语义评分**：
   - 精确名称匹配得分最高
   - 中文关键词匹配正确排序（如"查询用户"命中 get_user）
   - 完全不相关查询返回空列表或触发降级

3. **分类过滤**：
   - 按 category 过滤正确
   - category 为 null 的工具在过滤时不被包含
   - 不存在的 category 返回空列表

4. **降级行为**：
   - 全部评分低于阈值时降级返回 primary 工具摘要
   - `fallback_triggered` 标记正确

5. **结果格式化**：
   - `content[0].text` 包含工具名、描述和简化参数
   - `structuredContent` 包含完整 inputSchema
   - 降级时 text 包含 primary 工具列表

### 14.2 集成测试

1. **`tools/list` 初始化**：
   - 无参数调用只返回 primary 工具 + `search_apis`
   - `search_apis` 的 inputSchema 格式正确
   - secondary/utility 工具不出现在列表中

2. **`search_apis` 完整调用链**：
   - 调用 `search_apis(query="查询用户")` 返回匹配工具
   - 返回结果中包含工具的 inputSchema
   - LLM 据此调用业务工具成功（先发现、再调用闭环）

3. **`search_apis` 限流**：
   - 超限后返回 `isError: true` 限流提示
   - 限流不影响普通业务工具调用

4. **热重载联动**：
   - 工具配置变更后索引自动重建
   - 新增工具可被 `search_apis` 检索到
   - 回滚后索引恢复

5. **现有功能不回退**：
   - 普通业务工具的 `tools/call` 行为完全不变
   - 鉴权、全局限流、会话管理等不受影响

### 14.3 验收指标

| 指标 | 目标 |
|------|------|
| `tools/list` 初始列表 | 仅包含 primary 工具 + search_apis |
| `search_apis` 延迟 | 单次查询 < 5ms（20 个工具规模） |
| 相关性准确率 | Top-3 中包含正确工具的概率 > 80%（基于预设测试查询） |
| 索引重建时间 | < 50ms |
| 降级可靠性 | 任何异常情况下至少返回 primary 工具摘要 |
| 限流有效性 | 超限后明确阻断，不影响业务工具调用 |

---

## 15. 实验设计

为论文提供量化数据，设计以下对比实验：

### 15.1 实验前提

创建 15~20 个模拟工具配置（覆盖 4~5 个 category、3 个 tier），使工具规模足以体现渐进式披露和 TF-IDF 筛选的价值。模拟工具仅需 YAML 定义，不需要真实下游 API。

### 15.2 实验一：初始 Token 消耗对比

| 组别 | `tools/list` 行为 | 观测 |
|------|------------------|------|
| 对照组 | 返回全部 20 个工具 | 响应体大小 (bytes)、工具描述总 token 数 |
| 实验组 | 返回 primary 工具 + search_apis（约 8 个） | 响应体大小 (bytes)、工具描述总 token 数 |

预期结论：初始化阶段 token 消耗降低 50%~65%。

### 15.3 实验二：`search_apis` 检索准确率

准备 10~15 条预设自然语言查询，每条有明确的"正确工具"：

| 查询 | 正确工具 |
|------|---------|
| "查询用户邮箱" | get_user |
| "创建一个新订单" | create_order |
| ... | ... |

| 指标 | 观测 |
|------|------|
| Top-3 命中率 | 正确工具出现在 Top-3 的比例 |
| Top-5 命中率 | 正确工具出现在 Top-5 的比例 |

预期结论：Top-3 命中率 > 80%，Top-5 命中率 > 95%。

### 15.4 实验三：渐进式披露端到端效果

模拟 LLM 对话流程：

| 步骤 | 操作 | 可见工具数 |
|------|------|-----------|
| 1. 初始化 | `tools/list` | ~8 个（primary + search_apis） |
| 2. 发现 | `tools/call(search_apis, query="...")` | 额外获得 Top-3 工具 |
| 3. 调用 | `tools/call(具体工具)` | 成功执行 |

验证："初始化 → 发现 → 调用"三阶段完整闭环可以工作。

### 15.5 实验四：限流防护效果

连续快速发送 15 次 `search_apis` 调用（默认限流 10 次/60 秒）：

| 指标 | 预期 |
|------|------|
| 前 10 次 | 正常返回检索结果 |
| 第 11~15 次 | 返回 `isError: true` 限流提示 |
| 同期普通工具调用 | 不受影响 |

---

## 16. 依赖变更

### 16.1 新增运行时依赖

| 包名 | 版本要求 | 用途 | 大小 |
|------|---------|------|------|
| `scikit-learn` | >= 1.3.0 | TF-IDF 向量化 + 余弦相似度 | ~30MB（含传递依赖 numpy、scipy 等合计约 150-200MB） |
| `jieba` | >= 0.42.0 | 中文分词 | ~2MB |

### 16.2 环境要求

- 无外部 API 依赖
- 无模型文件下载
- 无向量数据库
- 无网络要求（jieba 使用内置词典）

---

## 17. 实施计划（建议 1.5~2 周）

### 第 1~2 天：核心引擎

- 实现 `ToolDiscoveryEngine`（jieba 分词 + TF-IDF 索引构建 + 评分 + 过滤 + 降级 + 结果格式化）
- 实现 `SearchApisParams` / `SearchResult` 模型
- 单元测试覆盖评分、过滤、降级、格式化逻辑

### 第 3~4 天：协议集成

- `ToolMeta` 新增 `category` / `tier` 字段
- 修改 `McpService`：`tools/list` 返回 primary + search_apis；`tools/call` 拦截 search_apis；`handle()` 新增 `client_key` 参数；显式捕获 `search_apis` 参数 `ValidationError` 转为 -32602
- 修改 `mcp_routes.py`：将已提取的 `client_key` 传入 `McpService.handle()`
- 修改 `ToolRegistry`：联动索引重建 + `list_primary_tools()` + `set_discovery_engine()` 后绑定接口
- 修改 `main.py`：按 §11.4 初始化顺序装配发现引擎和独立限流器
- 更新现有工具配置文件
- 集成测试

### 第 5~6 天：限流与防御

- 实现 `search_apis` 独立限流逻辑
- 限流集成测试
- 补充边界测试用例（空 query、非法参数、降级、限流）

### 第 7~8 天：实验与完善

- 创建 15~20 个模拟工具配置
- 编写实验脚本，运行对比实验，生成实验数据
- 更新 `README.md` 和管理接口指标
- 更新现有测试以适配新行为（详见下方清单）

### 第 7~8 天附：需要更新的现有测试用例清单

以下测试因 `tools/list` 行为变更或 `McpService` 接口变更而需要适配：

**`tests/test_gateway.py`**：

| 测试函数 | 需要的改动 |
|---------|-----------|
| `test_initialize_and_tools_list` | 增加断言：`"search_apis" in tool_names`；若存在 secondary/utility 工具，验证它们**不在**列表中 |
| `test_tools_call_unknown_tool_returns_error` | 确认 `available_tools` 中不包含 `"search_apis"`（它是虚拟工具，不在注册表中） |
| `build_client` / `_do_initialize` | `McpService` 构造函数签名变化（新增 `discovery_engine`、`discovery_rate_limiter`），需更新 `create_app` 或确保默认值兼容 |

**`tests/test_config_registry.py`**：

| 测试函数 | 需要的改动 |
|---------|-----------|
| `test_registry_reload_*` / `test_registry_rollback_*` | 若 `ToolRegistry` 内部调用 `_discovery_engine.rebuild_index()`，需在测试中设置 `registry.set_discovery_engine(...)` 或确认 `_discovery_engine is None` 时跳过重建不抛异常 |

**`tests/test_models.py`**：

| 测试函数 | 需要的改动 |
|---------|-----------|
| `ToolMeta` 相关测试 | 验证 `category` 和 `tier` 的默认值（`None` 和 `"primary"`）；验证显式赋值后的序列化正确性 |

**其他测试文件**（`test_adaptation_engine.py`、`test_response_error_mapper.py`、`test_security_components.py`、`test_sqlite_repositories.py`、`test_session_manager.py`、`test_runtime_features.py`）预期**不需要改动**，因为它们不涉及 `tools/list` 行为或 `McpService` 构造函数。

### 第 9~10 天：缓冲

- 修复测试中发现的问题
- 优化分词效果（如有必要添加 jieba 自定义词典）
- 确保现有测试适配后全部通过

---

## 18. 风险与应对

| 风险 | 严重度 | 应对 |
|------|--------|------|
| 工具数量过少时 TF-IDF 区分度不高 | 中 | 创建模拟工具扩大规模；空结果时降级返回 primary 摘要 |
| 中文分词精度影响匹配质量 | 低 | 使用 jieba 分词；必要时可添加自定义词典 |
| `tools/list` 行为变更导致现有测试失败 | 中 | 预留时间集中更新测试；变更是有意设计，非 regression |
| LLM 不遵守 Prompt 中的重试限制 | 中 | 网关级限流作为硬限制兜底，不依赖 LLM 自律 |
| 部分 MCP 客户端可能限制只能调用 `tools/list` 中的工具 | 低 | 本项目演示使用 HTTP 客户端和 `llm_demo.py`，不受此限；论文中标注为已知限制 |
| 索引重建与查询并发 | 低 | 原子替换引用策略 + 局部变量读取 |
| jieba 首次加载慢（~1-2s） | 低 | 仅在服务启动时发生一次 |
| scikit-learn 及传递依赖（numpy、scipy 等）合计约 150-200MB | 低 | 服务端项目，磁盘开销可接受 |

---

## 19. 答辩表述建议

本方案通过引入 `search_apis` Meta-Tool，将智能工具发现能力融入标准 MCP 协议的 `tools/call` 流程中。初始化阶段，网关仅向 LLM 暴露核心高频工具和搜索入口，实现渐进式披露；当现有工具无法满足需求时，LLM 主动调用 `search_apis`，网关基于 TF-IDF 语义检索从全量 API 库中匹配最相关的工具并返回其名称、描述和参数格式，LLM 据此发起后续调用。整个过程的核心交互完全基于标准 MCP 协议的 `tools/list` 和 `tools/call`，无需客户端做任何适配。系统集成 jieba 中文分词以正确处理中文语义，单次检索延迟低于 5ms。同时设置独立限流防止检索死循环，无匹配结果时自动降级返回已知工具列表。这一设计使网关从"被动执行器"升级为"主动推荐型智能网关"——LLM 不再需要预先知道所有 API，而是按需发现、即时调用。
