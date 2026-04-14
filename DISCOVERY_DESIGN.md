# 智能工具发现与渐进式披露 — 设计与实现文档

## 1. 文档目的

本文档用于定义并指导 `MCP Smart API Gateway` 的"智能工具发现与渐进式披露"能力落地。
目标是在保持现有 `tools/list` / `tools/call` 完全兼容的前提下，为 `tools/list` 引入基于 TF-IDF 的语义检索与分层过滤能力，使网关能够根据自然语言上下文智能筛选并排序工具，减少 LLM 的工具选择负担，提升调用准确率。

---

## 2. 背景与问题定义

### 2.1 现状

当前网关 `tools/list` 返回注册表中的全部工具，不接受任何过滤参数。当工具数量较少时这是合理的，但随着工具规模增长，这一行为会成为瓶颈。

### 2.2 问题

当工具数量增加后，扁平工具列表会导致：

- LLM 接收过多工具描述，消耗大量 token
- 工具选择准确率随数量增长而下降
- 无法按业务领域或重要性分层展示

### 2.3 方案定位

在不改变 MCP 协议调用模式的前提下，增强 `tools/list` 的智能筛选能力：

- 支持自然语言查询，基于 TF-IDF 语义评分返回最相关工具
- 支持 category（业务分类）与 tier（重要性层级）多维度过滤
- 工具选择的最终决策权保留在 LLM 侧，网关只负责"推荐"
- 无查询参数时行为不变，确保完全向后兼容

---

## 3. 设计目标与非目标

### 3.1 设计目标

1. **语义智能**：基于 TF-IDF 信息检索技术，根据自然语言查询对工具进行语义相关性评分与排序。
2. **渐进式披露**：支持按 `category`（业务域）和 `tier`（重要性层级）逐步暴露工具子集。
3. **兼容现网**：`tools/list` 无参数时返回全部工具，行为与当前完全一致。
4. **可解释**：每个返回工具附带 `_relevance_score`，客户端和开发者可理解排序依据。
5. **中文友好**：集成 jieba 分词，正确处理中文工具描述与中文查询。
6. **安全降级**：当匹配结果为空时，降级返回全部工具，确保不比不用更差。
7. **可观测**：记录发现请求日志，统计查询命中率与平均候选数。

### 3.2 非目标（本阶段不做）

- 不实现基于深度学习 Embedding 的语义检索（如 sentence-transformers）
- 不实现网关侧自动选择并执行工具（决策权保留给 LLM）
- 不实现工具推荐的个性化（基于历史调用偏好）
- 不实现调用失败时的替代工具推荐
- 不改变 `tools/call` 的任何行为

---

## 4. 总体架构

### 4.1 逻辑分层

1. **MCP 协议层**：`/mcp` 接口接收 `tools/list`，解析可选的 `params`。
2. **发现决策层**（新增）：`ToolDiscoveryEngine` 执行语义评分与过滤。
3. **注册层**（复用）：`ToolRegistry` 提供工具快照与元数据。
4. **观测层**（扩展）：记录发现日志与统计指标。

### 4.2 数据流

```
LLM -> tools/list(query?, category?, tier?, top_k?)
     -> McpService
     -> ToolDiscoveryEngine.discover(...)
         ├── 1. 分类过滤 (category)
         ├── 2. 层级过滤 (tier)
         ├── 3. TF-IDF 语义评分 (query)
         ├── 4. 阈值过滤 + Top-K 截断
         └── 5. 空结果降级 → 返回全部工具
     -> 返回排序后的工具子集 (附 relevance_score)
     -> LLM 基于子集自行决定调用哪个工具
     -> tools/call(具体工具名)
```

### 4.3 与现有架构的关系

```
                         McpService
                        /          \
            tools/list (增强)    tools/call (不变)
                  |                    |
        ToolDiscoveryEngine (新增)  AdaptationEngine
                  |                    |
             ToolRegistry          RestConnector
                  |
             ConfigLoader
```

核心原则：`ToolDiscoveryEngine` 由 `McpService` 在 `tools/list` 分支中调用，**与 `tools/call` 的执行链路完全解耦**。`AdaptationEngine`、`RestConnector` 等现有模块不受任何影响。

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

业务领域标识，用于粗粒度过滤。值为自由字符串，由工具配置者自行定义：

- `user_management`：用户管理
- `order`：订单相关
- `finance`：财务相关
- `system`：系统运维
- 自定义扩展...

未配置时默认 `null`，表示不属于任何特定分类。分类过滤时，仅返回 category 与请求值精确匹配的工具；`category=null` 的工具**不会**被包含在任何分类过滤结果中。若要查看所有工具，不传 `category` 参数即可。

#### 层级（tier）

工具重要性/使用频率标识，用于渐进式披露：

| 层级 | 含义 | 典型场景 |
|------|------|---------|
| `primary` | 核心高频工具 | 默认首批展示 |
| `secondary` | 辅助工具 | 需指定或查询触发 |
| `utility` | 低频/调试工具 | 仅精确查询时展示 |

未配置时默认 `"primary"`。

### 5.2 发现引擎配置

在 `app/settings.py` 的 `Settings` 中新增发现相关参数（通过环境变量配置）：

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `MCP_GATEWAY_DISCOVERY_DEFAULT_TOP_K` | `10` | 默认返回工具数上限 |
| `MCP_GATEWAY_DISCOVERY_MAX_TOP_K` | `50` | top_k 的最大允许值 |
| `MCP_GATEWAY_DISCOVERY_SCORE_THRESHOLD` | `0.05` | 最低相关性评分阈值 |
| `MCP_GATEWAY_DISCOVERY_TFIDF_MAX_FEATURES` | `5000` | TF-IDF 最大特征数 |
| `MCP_GATEWAY_DISCOVERY_FALLBACK_ON_EMPTY` | `true` | 空结果时是否降级返回全部工具 |

### 5.3 `tools/list` 扩展参数

在 MCP JSON-RPC `tools/list` 的 `params` 中支持以下可选字段：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/list",
  "params": {
    "query": "查询用户信息",
    "category": "user_management",
    "tier": "primary",
    "top_k": 5
  }
}
```

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `query` | string | 否 | null | 自然语言查询，触发 TF-IDF 语义评分 |
| `category` | string | 否 | null | 按业务分类过滤 |
| `tier` | string 或 string[] | 否 | 见下文 | 按层级过滤 |
| `top_k` | integer | 否 | 配置默认值 | 最多返回工具数 |

**触发条件**：当 `params` 中包含 `query`、`category`、`tier`、`top_k` 中的**任意一个**时，触发发现逻辑。`params` 为 `null`、`{}` 或仅包含 MCP 标准字段（如 `cursor`）时不触发，返回全部工具，行为与当前完全一致。

**tier 默认值逻辑**（仅在发现逻辑触发时生效）：
- 无 `query` 时，默认仅返回 `["primary"]` 层级的工具
- 有 `query` 时，默认搜索所有层级 `["primary", "secondary", "utility"]`

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

### 7.1 正常流程（带 query 参数）

1. 客户端调用 `tools/list`，`params` 中包含 `query`。
2. `McpService` 解析参数，构造 `DiscoveryParams`，转交 `ToolDiscoveryEngine.discover()`。
3. 从 `ToolRegistry` 获取当前全部工具列表。
4. 若指定 `category`，先过滤出 category 精确匹配的工具（category 为 null 的工具不会被包含）。
5. 若指定 `tier`，再过滤出该层级的工具。
6. 使用 TF-IDF 计算各工具与 `query` 的相关性评分。
7. 过滤掉低于 `score_threshold` 的工具。
8. 按评分降序排列，截取 `top_k` 个。
9. **若结果为空且 `fallback_on_empty=true`，降级返回全部工具**（不附带评分）。
10. 返回工具列表，每个工具附带 `_relevance_score` 字段。

### 7.2 无参数流程（向后兼容）

1. 客户端调用 `tools/list`，`params` 为 `null` 或 `{}`。
2. 不触发任何发现逻辑。
3. 返回全部工具，行为与当前完全一致。

### 7.3 仅分类/层级过滤流程

1. 客户端调用 `tools/list`，仅指定 `category` 和/或 `tier`，不包含 `query`。
2. 执行确定性过滤，不触发 TF-IDF 评分。
3. 返回过滤后的完整工具列表（不附带 `_relevance_score`）。

---

## 8. 响应格式设计

### 8.1 标准响应（无参数，完全向后兼容）

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
      }
    ]
  }
}
```

### 8.2 增强响应（带查询参数）

发现引擎先调用 `tool.mcp_tool_schema()` 获取基础 schema dict，再向 `annotations` 中注入 `_relevance_score`、`_category`、`_tier` 扩展字段。`ToolConfig` 类的 `mcp_tool_schema()` 方法本身不做任何修改。

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
          "tags": ["用户", "查询"],
          "_relevance_score": 0.82,
          "_category": "user_management",
          "_tier": "primary"
        }
      }
    ],
    "_discovery": {
      "query": "查询用户信息",
      "total_tools": 20,
      "matched_tools": 3,
      "filters_applied": {
        "category": null,
        "tier": ["primary", "secondary", "utility"]
      },
      "top_k": 10,
      "score_threshold": 0.05,
      "fallback_triggered": false
    }
  }
}
```

### 8.3 降级响应（查询无匹配，回退全量）

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "tools": [
      { "..." : "全部工具，与无参数时一致" }
    ],
    "_discovery": {
      "query": "帮我处理一下",
      "total_tools": 20,
      "matched_tools": 0,
      "filters_applied": {
        "category": null,
        "tier": ["primary", "secondary", "utility"]
      },
      "top_k": 10,
      "score_threshold": 0.05,
      "fallback_triggered": true
    }
  }
}
```

`fallback_triggered: true` 向客户端表明本次返回的是降级全量结果而非语义匹配结果。

---

## 9. 代码改造清单

### 9.1 新增文件

| 文件 | 职责 |
|------|------|
| `app/core/discovery_engine.py` | `ToolDiscoveryEngine` 类：索引构建、TF-IDF 评分、分类/层级过滤、发现主逻辑 |
| `app/models/discovery_models.py` | `DiscoveryParams`（请求参数模型）、`DiscoveryMeta`（响应元信息模型） |
| `tests/test_discovery_engine.py` | 发现引擎单元测试与集成测试 |

### 9.2 修改文件

| 文件 | 修改内容 | 影响范围 |
|------|---------|---------|
| `app/models/tool_config.py` | `ToolMeta` 新增 `category: str \| None = None` 和 `tier: str = "primary"` | 模型层 |
| `app/core/mcp_service.py` | `tools/list` 分支：检测 params 是否包含发现参数，有则调用 `ToolDiscoveryEngine.discover()`，无则保持原有行为 | 协议层 |
| `app/core/tool_registry.py` | `load_initial_snapshot()` / `reload()` / `rollback()` 成功后通知 `ToolDiscoveryEngine` 重建索引 | 注册层 |
| `app/main.py` | lifespan 中初始化 `ToolDiscoveryEngine`，注入 `app.state.discovery_engine` | 应用入口 |
| `app/settings.py` | 新增发现相关配置字段；**注意 `with_base_dir()` 方法逐字段拷贝，新增字段必须同步添加到该方法中，否则会被静默丢失** | 配置层 |
| `app/api/admin_routes.py` | `/admin/status` 新增发现统计输出（可选） | 管理接口 |
| `pyproject.toml` | 新增 `scikit-learn` 和 `jieba` 依赖 | 依赖管理 |
| `configs/tools/*.yaml` | 现有工具补充 `category` 和 `tier` 字段 | 工具配置 |

### 9.3 不修改的文件

以下文件**不需要任何改动**，确保执行链路零影响：

- `app/core/adaptation_engine.py`
- `app/core/rest_connector.py`
- `app/core/response_mapper.py`
- `app/core/error_mapper.py`
- `app/core/auth.py`
- `app/core/rate_limit.py`
- `app/core/session_manager.py`
- `app/api/mcp_routes.py`

---

## 10. 错误处理设计

无需新增 JSON-RPC 错误码。发现过程中的异常处理策略：

| 场景 | 处理方式 |
|------|---------|
| `params` 为 `null` 或 `{}` | 不触发发现逻辑，返回全部工具（向后兼容） |
| `query` 为空字符串 | 等同于无 `query`，仅执行分类/层级过滤 |
| `category` 不存在 | 返回空工具列表（不报错，与"该分类下无工具"语义一致） |
| `tier` 值非法 | 返回 `INVALID_PARAMS` 错误（-32602） |
| `top_k` 超出范围 | 截断到 `max_top_k`，不报错 |
| TF-IDF 索引未就绪 | 降级返回全部工具，不阻塞服务 |
| 所有工具评分低于阈值 | 若 `fallback_on_empty=true` 返回全部工具，否则返回空列表 |
| 发现引擎内部异常 | 捕获后降级返回全部工具，记录错误日志 |

---

## 11. 观测与日志设计

### 11.1 结构化日志

每次带 `query` 的 `tools/list` 请求记录：

```json
{
  "event": "tool_discovery",
  "request_id": "abc123",
  "query": "查询用户",
  "category_filter": null,
  "tier_filter": ["primary", "secondary", "utility"],
  "total_tools": 20,
  "candidates_after_filter": 15,
  "matched_tools": 3,
  "top_score": 0.82,
  "fallback_triggered": false,
  "latency_ms": 2
}
```

### 11.2 管理接口指标（`/admin/status` 扩展，可选）

```json
{
  "discovery": {
    "total_queries": 156,
    "avg_matched_tools": 3.2,
    "avg_top_score": 0.65,
    "fallback_count": 12,
    "index_tool_count": 20,
    "last_index_built_at": "2026-04-14T10:30:00Z"
  }
}
```

---

## 12. 索引并发安全设计

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

    def _score(self, query: str) -> list[tuple[ToolConfig, float]]:
        # 查询开头先将引用读到局部变量，确保本次查询全程使用同一版本索引
        vectorizer = self._vectorizer
        matrix = self._tfidf_matrix
        tools = self._indexed_tools
        # 后续全部使用局部变量 vectorizer / matrix / tools
        ...
```

查询时先将 `self._vectorizer` / `self._tfidf_matrix` / `self._indexed_tools` 读到局部变量，后续全程使用局部变量。这样即使查询执行中途发生了索引重建（async 上下文中经过 await 让出控制权），本次查询也始终使用同一版本的完整索引。

---

## 13. 测试方案

### 13.1 单元测试

1. **TF-IDF 索引构建**：
   - 工具列表变更后索引正确重建
   - 空工具列表不抛异常

2. **语义评分**：
   - 精确名称匹配得分最高
   - 中文关键词匹配正确排序（如"查询用户"命中 get_user）
   - 完全不相关查询返回空列表或触发降级

3. **分类过滤**：
   - 按 category 过滤正确
   - category 为 null 的工具在分类过滤时不被包含
   - 不存在的 category 返回空列表

4. **层级过滤**：
   - 按 tier 过滤正确
   - 多层级组合过滤

5. **向后兼容**：
   - 无参数时返回全部工具，响应格式不变
   - 不触发任何 TF-IDF 计算

6. **降级行为**：
   - 全部评分低于阈值时降级返回全量
   - `fallback_triggered` 标记正确

7. **边界情况**：
   - `top_k=1` 只返回最相关的 1 个
   - 查询为空字符串
   - `top_k` 超过 `max_top_k` 时被截断

### 13.2 集成测试

1. `tools/list` 带 `query` 参数完整 JSON-RPC 流程
2. `tools/list` 带 `category` / `tier` 参数过滤流程
3. 配置热重载后索引自动重建，查询结果反映新工具
4. 回滚后索引恢复到前一版本
5. 与 `tools/call` 配合的完整调用链（先发现、再调用）

### 13.3 验收指标

| 指标 | 目标 |
|------|------|
| 向后兼容 | 无参数 `tools/list` 行为完全不变，现有测试全部通过 |
| 评分延迟 | 单次查询 < 5ms（20 个工具规模） |
| 相关性准确率 | Top-3 中包含正确工具的概率 > 80%（基于预设测试查询） |
| 索引重建时间 | < 50ms |
| 降级可靠性 | 任何异常情况下至少返回全部工具，不返回错误 |

---

## 14. 实验设计

为论文提供量化数据，设计以下对比实验：

### 14.1 实验前提

创建 15~20 个模拟工具配置（覆盖 4~5 个 category、3 个 tier），使工具规模足以体现 TF-IDF 的筛选价值。模拟工具仅需 YAML 定义，不需要真实下游 API。

### 14.2 实验一：Token 消耗对比

| 组别 | 方式 | 观测 |
|------|------|------|
| 对照组 | `tools/list` 无参数，返回全部 20 个工具 | 响应体大小 (bytes)、工具描述总 token 数 |
| 实验组 | `tools/list(query=...)` 返回 Top-3 | 响应体大小 (bytes)、工具描述总 token 数 |

预期结论：实验组 token 消耗降低 70%~85%。

### 14.3 实验二：工具选择准确率

准备 10~15 条预设自然语言查询，每条有明确的"正确工具"：

| 查询 | 正确工具 |
|------|---------|
| "查询用户邮箱" | get_user |
| "创建一个新订单" | create_order |
| ... | ... |

对比两组的 Top-K 命中率（正确工具出现在返回列表中的比例）：

| 组别 | 方式 | 观测 |
|------|------|------|
| 对照组 | 全量返回，命中率恒为 100%（但 LLM 选择负担大） | — |
| 实验组 | TF-IDF Top-3 / Top-5 | Top-3 命中率、Top-5 命中率 |

预期结论：Top-3 命中率 > 80%，Top-5 命中率 > 95%。

### 14.4 实验三：渐进式披露效果

对比不同 tier 设置下返回的工具数量：

| 场景 | tier 设置 | 预期返回数 |
|------|----------|-----------|
| 首次对话 | `["primary"]` | 5~8 个核心工具 |
| 有上下文 | `["primary", "secondary"]` | 12~15 个 |
| 精确查询 | 全部层级 + query | Top-3 |

---

## 15. 依赖变更

### 15.1 新增运行时依赖

| 包名 | 版本要求 | 用途 | 大小 |
|------|---------|------|------|
| `scikit-learn` | >= 1.3.0 | TF-IDF 向量化 + 余弦相似度 | ~30MB |
| `jieba` | >= 0.42.0 | 中文分词 | ~2MB |

### 15.2 环境要求

- 无外部 API 依赖
- 无模型文件下载
- 无向量数据库
- 无网络要求（jieba 使用内置词典）

---

## 16. 实施计划（建议 1.5 周）

### 第 1~2 天：核心引擎

- 实现 `ToolDiscoveryEngine`（jieba 分词 + TF-IDF 索引构建 + 评分 + 过滤 + 降级）
- 实现 `DiscoveryParams` / `DiscoveryMeta` 模型
- 单元测试覆盖评分、过滤、降级逻辑

### 第 3~4 天：协议集成

- `ToolMeta` 新增 `category` / `tier` 字段
- 修改 `McpService` 处理 `tools/list` 参数
- 修改 `ToolRegistry` 联动索引重建
- 修改 `main.py` 初始化发现引擎
- 更新现有工具配置文件
- 集成测试

### 第 5~6 天：实验与完善

- 创建 15~20 个模拟工具配置
- 编写实验脚本，运行对比实验，生成实验数据
- 补充边界测试用例
- 更新 `README.md` 和管理接口指标

### 第 7 天：缓冲

- 修复测试中发现的问题
- 优化分词效果（如有必要添加 jieba 自定义词典）

---

## 17. 风险与应对

| 风险 | 严重度 | 应对 |
|------|--------|------|
| 工具数量过少时 TF-IDF 区分度不高 | 中 | 创建模拟工具扩大规模；空结果时降级返回全量，确保不比不用更差 |
| 中文分词精度影响匹配质量 | 低 | 使用 jieba 分词；必要时可添加自定义词典 |
| `tools/list` 自定义参数非 MCP 标准 | 低 | 无参数时行为完全不变；论文中说明为兼容性扩展 |
| 索引重建与查询并发 | 低 | 原子替换引用策略，查询读到的总是完整索引 |
| jieba 首次加载慢（~1-2s） | 低 | 仅在服务启动时发生一次，对长驻服务无影响 |
| scikit-learn 安装体积 | 低 | 服务端项目，磁盘开销可忽略 |

---

## 18. 答辩表述建议

本方案在不改变 MCP 协议调用模式的前提下，为 `tools/list` 引入了基于 TF-IDF 的语义检索能力与多维度渐进式披露机制。当工具数量增长时，系统能够根据自然语言上下文智能筛选最相关的工具子集返回给 LLM，减少 token 消耗并提升工具选择准确率。同时支持按业务分类（category）和重要性层级（tier）进行分层过滤，兼顾智能性与可控性。系统集成 jieba 中文分词以正确处理中文语义，整个评分过程纯本地计算，无外部依赖，单次查询延迟低于 5ms。当检索无匹配结果时自动降级为全量返回，确保可用性。这一增强使网关从"被动列举型"升级为"主动推荐型"的智能工具发现平台。
