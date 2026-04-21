---
name: 代码提交评审报告
overview: 对提交 5965e89（"第一版渐进式披露"）进行严格的设计文档符合性审查和代码质量审核，涵盖 20 个修改文件、1292 行新增代码。
todos:
  - id: fix-exception-fallback
    content: "[中] 在 _handle_search_apis 中为 search() 调用增加 try-except，捕获异常后降级返回 primary 工具摘要（设计文档 12 节要求）"
    status: completed
  - id: fix-rate-limit-bypass
    content: "[中] 修复 client_key 为 None 时 search_apis 限流被跳过的问题，使用全局默认 key 兜底"
    status: completed
  - id: fix-fallback-description
    content: "[低] 降级文本增加工具描述，primary_tools 从 list[str] 改为 list[dict]，存储 name+description（设计文档 8.3 节）"
    status: completed
  - id: fix-category-miss-behavior
    content: "[低] 分类不存在时直接返回空匹配列表，不触发 fallback（设计文档 12 节）"
    status: completed
  - id: fix-log-request-id
    content: "[低] 将 request_id 传入 search() 或 _log_search()，补全日志中的请求追踪字段（设计文档 13.1 节）"
    status: completed
  - id: fix-avg-top-score
    content: "[低] describe() 中增加 avg_top_score 统计（设计文档 13.2 节）"
    status: completed
  - id: fix-tier-validation
    content: "[低] 为 tier 字段增加 Literal 类型约束或 field_validator 告警"
    status: completed
isProject: false
---

# 提交 5965e89 代码评审报告

## 一、总体评价

本次提交整体完成度较高，代码结构清晰，测试覆盖全面（新增 31 个测试用例，全部 161 个测试通过）。设计文档中约 90% 的要求在代码中正确落地。以下从**文档符合性**和**代码质量/隐患**两个维度逐项审核。

---

## 二、文档符合性审核（逐章节对照）

### 2.1 已正确实现的部分

| 设计文档章节 | 对应代码 | 状态 |
|---|---|---|
| 5.1 ToolMeta 新增 category / tier | [app/models/tool_config.py](app/models/tool_config.py) L14-15 | 通过 |
| 5.2 search_apis Schema 定义 | [app/core/discovery_engine.py](app/core/discovery_engine.py) L24-58 | 通过 |
| 5.3 发现引擎配置（7 个环境变量） | [app/settings.py](app/settings.py) + [.env.example](.env.example) | 通过 |
| 6.2 中文分词 + bigram | [app/core/discovery_engine.py](app/core/discovery_engine.py) L61-65 `_chinese_analyzer` | 通过 |
| 6.3 工具文档构建（name 重复） | [app/core/discovery_engine.py](app/core/discovery_engine.py) L68-79 `_build_document` | 通过 |
| 6.4 TF-IDF 索引构建参数 | `TfidfVectorizer(analyzer=..., max_features=5000, sublinear_tf=True)` | 通过 |
| 7.1 tools/list 仅返回 primary + search_apis | [app/core/mcp_service.py](app/core/mcp_service.py) L107-114 `_build_tools_list` | 通过 |
| 7.2 search_apis 拦截在 AdaptationEngine 之前 | [app/core/mcp_service.py](app/core/mcp_service.py) L77-78 | 通过 |
| 7.3 业务工具调用不限 tier | `get_tool()` 从全量注册表查找 | 通过 |
| 8.1 tools/list 响应格式 | 集成测试验证 | 通过 |
| 8.2 search_apis 正常响应格式（content + structuredContent） | [app/core/discovery_engine.py](app/core/discovery_engine.py) L229-264 | 通过 |
| 8.4 限流响应（isError: true） | [app/core/mcp_service.py](app/core/mcp_service.py) L134-144 | 通过 |
| 9.1 Prompt 级 3 次重试引导 | SEARCH_APIS_SCHEMA description 中包含 | 通过 |
| 9.2 网关级独立限流 | 独立 SlidingWindowRateLimiter 实例 | 通过 |
| 10 索引并发安全（局部变量读取 + 原子替换） | [app/core/discovery_engine.py](app/core/discovery_engine.py) L152-154 | 通过 |
| 11.3 不修改的文件清单 | adaptation_engine / rest_connector / rate_limit 等均未改动 | 通过 |
| 11.4 初始化顺序（后绑定模式） | [app/main.py](app/main.py) 中 registry -> engine -> set_discovery_engine -> limiter -> McpService | 通过 |
| 12 ValidationError 显式捕获为 -32602 | [app/core/mcp_service.py](app/core/mcp_service.py) L146-153 | 通过 |
| 12 top_k 超出范围截断不报错 | `min(top_k or default, max_top_k)` | 通过 |
| 16.1 新增依赖 scikit-learn / jieba | [pyproject.toml](pyproject.toml) | 通过 |
| configs 补充 category / tier | 全部 6 个 yaml 文件 | 通过 |

### 2.2 与设计文档存在偏差的部分

#### 偏差 1：降级文本缺少工具描述（对照 8.3 节）

设计文档 8.3 节明确要求降级响应中展示工具的名称**和描述**：

```
- get_user: 查询用户信息
- create_order: 创建订单
```

但代码实现中 `DiscoveryResult.primary_tools` 仅存储了工具名称列表（`list[str]`），降级文本仅显示：

```
- get_user
- create_order
```

**影响**：LLM 在降级场景下拿到的信息不够丰富，可能无法准确判断现有工具的功能。

**对应代码**：

```229:247:app/core/discovery_engine.py
    def _format_response(self, result: DiscoveryResult) -> dict[str, Any]:
        # ...
        elif result.fallback_triggered and result.primary_tools:
            tool_list = "\n".join(f"- {name}" for name in result.primary_tools)
```

以及：

```213:227:app/core/discovery_engine.py
    def _build_fallback(self, ...):
        # ...
        primary_tools=[t.tool_meta.name for t in primary_tools],
```

#### 偏差 2：分类不存在时触发了完整降级（对照 12 节）

设计文档 12 节明确规定：

> `category` 不存在 --> 返回空匹配列表（不报错）

但代码中当 category 过滤得到空列表时，走的是 `_build_fallback()` 逻辑，会设置 `fallback_triggered=True` 并附带 primary 工具列表。这意味着即使只是分类名拼写错误，也会把 primary 工具全部兜出来，与文档描述不符。

**对应代码**：

```168:171:app/core/discovery_engine.py
        if not filtered_indices:
            result = self._build_fallback(query, category, primary_tools or [])
            self._log_search(query, category, effective_top_k, result, start)
            return self._format_response(result)
```

#### 偏差 3：搜索日志缺少 `request_id`（对照 13.1 节）

设计文档 13.1 节定义的结构化日志包含 `"request_id": "abc123"`，但 `_log_search()` 方法既不接收也不记录 `request_id`。MCP 请求的 `request.id` 在 `McpService._handle_search_apis()` 中可用但没有传递给 discovery engine。

**影响**：调用链追踪断裂，无法通过日志将发现请求与具体的 MCP 请求关联。

#### 偏差 4：管理统计缺少 `avg_top_score`（对照 13.2 节）

设计文档 13.2 节定义的管理接口输出包含 `"avg_top_score": 0.65`，但 `describe()` 方法未追踪此指标。

**对应代码**：

```297:309:app/core/discovery_engine.py
    def describe(self) -> dict[str, Any]:
        return {
            "total_searches": self._total_searches,
            "avg_matched_tools": ...,
            "fallback_count": ...,
            "rate_limited_count": ...,
            "index_tool_count": ...,
            "last_index_built_at": ...,
        }
```

缺少 `avg_top_score` 的累计和计算逻辑。

---

## 三、代码质量与隐患审核

### 隐患 1（中等）：发现引擎内部异常未做降级保护

设计文档 12 节明确要求：

> 发现引擎内部异常 --> 捕获后降级返回 primary 工具摘要，记录错误日志

但 `McpService._handle_search_apis()` 中，`self.discovery_engine.search()` 调用**没有 try-except 包裹**。如果 TF-IDF 计算过程中发生未预期的异常（如 scipy 矩阵运算错误、数据类型不匹配等），异常会沿调用栈上抛到 `handle()` 的通用 `except Exception` 处理器，返回 `-32603 INTERNAL_ERROR`，而非文档要求的降级到 primary 工具摘要。

**对应代码**：

```155:162:app/core/mcp_service.py
        primary_tools = self.registry.list_primary_tools()
        result = self.discovery_engine.search(
            query=search_params.query,
            category=search_params.category,
            top_k=search_params.top_k,
            primary_tools=primary_tools,
        )
        return self._success(request_id, result)
```

**建议修复**：在 `_handle_search_apis` 中为 `search()` 调用增加 try-except，捕获 Exception 后手动构建 fallback 响应。

### 隐患 2（中等）：client_key 为 None 时限流被完全跳过

```128:130:app/core/mcp_service.py
        if self.discovery_rate_limiter is not None and client_key:
            try:
                self.discovery_rate_limiter.enforce(client_key)
```

当 `client_key` 为 `None`（例如 API Key 鉴权被禁用时），`search_apis` 的独立限流将**完全失效**。设计文档 9.2 节明确限流为"硬限制"兜底手段，不应有绕过路径。

**建议修复**：当 `client_key` 为 None 时，使用一个全局默认 key（如 `"__anonymous__"`）进行限流，确保无鉴权模式下限流仍生效。

### 隐患 3（低）：tier 字段无枚举校验

```15:15:app/models/tool_config.py
    tier: str = "primary"
```

`tier` 定义为自由字符串，没有 `Literal["primary", "secondary", "utility"]` 约束。用户配置 `tier: "praimry"`（拼写错误）时不会报错，但工具会静默消失于 `tools/list`（因为不是 "primary"），且行为不易排查。

**建议修复**：使用 `Literal` 类型约束或添加 `field_validator` 给出警告。

### 隐患 4（低）：`_handle_search_apis` 为同步方法，阻塞事件循环

`_handle_search_apis` 和 `ToolDiscoveryEngine.search()` 均为同步方法，其中包含 TF-IDF 矩阵计算（`cosine_similarity`）。在当前 20 个工具的规模下（<5ms）这不成问题，但如果工具数量增长到数百个以上，同步计算可能阻塞 asyncio 事件循环，影响其他并发请求的处理。

**建议**：当前规模下可接受，但未来如果工具规模增长，应考虑使用 `asyncio.to_thread()` 将 CPU 密集型计算移至线程池。

### 隐患 5（低）：统计计数器非原子操作

`_total_searches += 1`、`_total_matched += len(matched)` 等操作在严格意义上非原子。在单线程 asyncio 环境下，因为 `search()` 是同步方法（无 await 点），这些操作在一次事件循环迭代中完成，实际上是安全的。但如果未来引入 `asyncio.to_thread()` 或多线程，可能出现竞争条件。

**当前状态**：不影响正确性，但可标记为技术债。

### 隐患 6（极低）：空工具列表重建索引时 `_last_index_built_at` 被置为 None

```112:117:app/core/discovery_engine.py
        if not tools:
            self._vectorizer = None
            self._tfidf_matrix = None
            self._indexed_tools = []
            self._last_index_built_at = None
            return
```

如果 `reload()` 后工具列表为空（所有 YAML 被删除），之前的 `last_index_built_at` 时间戳会丢失。这只是信息丢失，不影响功能。

---

## 四、测试覆盖评审

### 已覆盖的测试场景（与文档 14 节对照）

| 测试类别 | 测试场景 | 对应测试 |
|---|---|---|
| 索引构建 | 全部 tier 工具都被索引 | `test_rebuild_index_with_all_tiers` |
| 索引构建 | 空列表不抛异常 | `test_rebuild_index_with_empty_list` |
| 索引构建 | 变更后正确重建 | `test_rebuild_index_replaces_previous` |
| 语义评分 | 精确名称匹配得分最高 | `test_exact_name_match_scores_highest` |
| 语义评分 | 中文关键词正确匹配 | `test_chinese_keyword_match` |
| 语义评分 | 不相关查询返回空 | `test_unrelated_query_returns_empty_or_low_scores` |
| 分类过滤 | 按 category 过滤正确 | `test_filter_by_existing_category` |
| 分类过滤 | 不存在的 category | `test_filter_by_nonexistent_category_returns_empty` |
| 降级 | primary 工具摘要 | `test_fallback_returns_primary_tools` |
| 降级 | fallback_triggered 标记 | `test_fallback_triggered_flag_correct` |
| 格式化 | content text 包含工具信息 | `test_content_text_contains_tool_info` |
| 格式化 | structuredContent 包含 inputSchema | `test_structured_content_has_input_schema` |
| 集成 | tools/list 初始化 | `test_tools_list_returns_primary_plus_search_apis` |
| 集成 | 完整发现-调用闭环 | `test_search_apis_discover_then_call` |
| 集成 | 限流 | `test_search_apis_rate_limiting` |
| 集成 | 热重载联动 | `test_hot_reload_updates_discovery_index` |
| 集成 | 回滚联动 | `test_rollback_restores_discovery_index` |
| 集成 | 现有功能不回退 | `test_normal_tool_call_unaffected` |
| 集成 | 管理状态接口包含发现统计 | `test_admin_status_includes_discovery_stats` |
| 边界 | 空 query 返回 -32602 | `test_search_apis_empty_query_returns_validation_error` |
| 边界 | 缺少 query 返回 -32602 | `test_search_apis_missing_query_returns_validation_error` |

### 缺失的测试场景

- **发现引擎内部异常时的降级行为**（对应隐患 1，需要 mock 让 search 抛异常后验证返回 fallback 而非 INTERNAL_ERROR）
- **category=null 的工具在分类过滤时不被包含**（文档 5.1 明确要求，未专门测试）
- **client_key 为 None 时限流是否生效**（对应隐患 2）

---

## 五、总结

### 整体评分：B+

**优点：**
- 架构清晰，初始化顺序、后绑定模式、拦截位置均严格遵循设计文档
- 并发安全策略（局部变量读取 + 原子替换）正确落地
- 测试覆盖全面，包含单元测试和端到端集成测试
- 现有功能无回退（test_gateway.py 适配正确）
- 错误处理分类准确（ValidationError -> -32602、限流 -> isError:true）
- `with_base_dir()` 方法已同步新增字段

**需要修复的问题（按优先级排序）：**
1. **[中]** 发现引擎内部异常未做降级保护（与设计文档 12 节矛盾）
2. **[中]** client_key 为 None 时限流失效（安全隐患）
3. **[低]** 降级文本缺少工具描述（与设计文档 8.3 节不符）
4. **[低]** 分类不存在时不应触发完整降级（与设计文档 12 节不符）
5. **[低]** 搜索日志缺少 request_id（与设计文档 13.1 节不符）
6. **[低]** 管理统计缺少 avg_top_score（与设计文档 13.2 节不符）
7. **[低]** tier 字段无枚举校验（潜在的配置错误难排查）
