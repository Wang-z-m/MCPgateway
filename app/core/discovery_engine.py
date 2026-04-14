"""Intelligent tool discovery engine based on TF-IDF semantic search."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import jieba
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app.models.discovery_models import DiscoveryResult
from app.models.tool_config import ToolConfig
from app.settings import Settings
from app.utils.logging import log_json

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"\w")

SEARCH_APIS_SCHEMA: dict[str, Any] = {
    "name": "search_apis",
    "title": "搜索 API 工具库",
    "description": (
        "【系统内置工具】当现有的核心 API 无法满足用户需求时，请调用此工具。"
        "传入用户的自然语言需求，系统将从企业 API 库中检索并返回最相关的 API 工具"
        "名称、描述和参数格式。注意：最多尝试 3 次不同的关键词。如果 3 次后仍未找"
        "到合适工具，请停止搜索并告知用户当前系统不支持该功能。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "用自然语言描述需要的 API 功能，例如'查询用户邮箱'或'创建订单'",
            },
            "category": {
                "type": "string",
                "description": "可选：按业务分类过滤，如 user_management、order、finance",
            },
            "top_k": {
                "type": "integer",
                "description": "可选：最多返回几个工具，默认 5",
                "minimum": 1,
                "maximum": 20,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "annotations": {
        "version": "1.0.0",
        "tags": ["system", "discovery"],
    },
}


def _chinese_analyzer(text: str) -> list[str]:
    raw_tokens = jieba.cut(text)
    tokens = [t for t in raw_tokens if _WORD_RE.search(t)]
    bigrams = [f"{tokens[i]}{tokens[i + 1]}" for i in range(len(tokens) - 1)]
    return tokens + bigrams


def _build_document(tool: ToolConfig) -> str:
    meta = tool.tool_meta
    parts = [
        meta.name,
        meta.name,
        meta.title,
        meta.description,
        " ".join(meta.tags),
    ]
    if meta.category:
        parts.append(meta.category)
    return " ".join(parts)


def _simplify_schema(schema: dict[str, Any]) -> dict[str, str]:
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    result: dict[str, str] = {}
    for name, prop in properties.items():
        ptype = prop.get("type", "any")
        suffix = " (必填)" if name in required else ""
        result[name] = f"{ptype}{suffix}"
    return result


class ToolDiscoveryEngine:
    def __init__(self, settings: Settings) -> None:
        self._default_top_k = settings.discovery_default_top_k
        self._max_top_k = settings.discovery_max_top_k
        self._score_threshold = settings.discovery_score_threshold
        self._max_features = settings.discovery_tfidf_max_features
        self._fallback_on_empty = settings.discovery_fallback_on_empty

        self._vectorizer: TfidfVectorizer | None = None
        self._tfidf_matrix: Any = None
        self._indexed_tools: list[ToolConfig] = []

        self._total_searches: int = 0
        self._total_matched: int = 0
        self._fallback_count: int = 0
        self._rate_limited_count: int = 0
        self._last_index_built_at: str | None = None

    def rebuild_index(self, tools: list[ToolConfig]) -> None:
        if not tools:
            self._vectorizer = None
            self._tfidf_matrix = None
            self._indexed_tools = []
            self._last_index_built_at = None
            return

        documents = [_build_document(tool) for tool in tools]
        new_vectorizer = TfidfVectorizer(
            analyzer=_chinese_analyzer,
            max_features=self._max_features,
            sublinear_tf=True,
        )
        new_matrix = new_vectorizer.fit_transform(documents)
        new_tool_list = list(tools)

        self._vectorizer = new_vectorizer
        self._tfidf_matrix = new_matrix
        self._indexed_tools = new_tool_list
        self._last_index_built_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        log_json(
            logger,
            logging.INFO,
            "discovery_index_rebuilt",
            tool_count=len(new_tool_list),
        )

    def search(
        self,
        query: str,
        category: str | None = None,
        top_k: int | None = None,
        primary_tools: list[ToolConfig] | None = None,
    ) -> dict[str, Any]:
        start = time.perf_counter()
        self._total_searches += 1

        effective_top_k = min(top_k or self._default_top_k, self._max_top_k)

        vectorizer = self._vectorizer
        matrix = self._tfidf_matrix
        tools = self._indexed_tools

        if vectorizer is None or matrix is None or not tools:
            result = self._build_fallback(query, category, primary_tools or [])
            self._log_search(query, category, effective_top_k, result, start)
            return self._format_response(result)

        if category is not None:
            filtered_indices = [
                i for i, t in enumerate(tools) if t.tool_meta.category == category
            ]
        else:
            filtered_indices = list(range(len(tools)))

        if not filtered_indices:
            result = self._build_fallback(query, category, primary_tools or [])
            self._log_search(query, category, effective_top_k, result, start)
            return self._format_response(result)

        query_vector = vectorizer.transform([query])
        all_scores = cosine_similarity(query_vector, matrix).flatten()

        candidates: list[tuple[int, float]] = []
        for idx in filtered_indices:
            score = float(all_scores[idx])
            if score >= self._score_threshold:
                candidates.append((idx, score))

        candidates.sort(key=lambda x: x[1], reverse=True)
        candidates = candidates[:effective_top_k]

        if not candidates:
            result = self._build_fallback(query, category, primary_tools or [])
            self._log_search(query, category, effective_top_k, result, start)
            return self._format_response(result)

        matched: list[dict[str, Any]] = []
        for idx, score in candidates:
            tool = tools[idx]
            matched.append({
                "name": tool.tool_meta.name,
                "title": tool.tool_meta.title,
                "description": tool.tool_meta.description,
                "relevance_score": round(score, 4),
                "inputSchema": tool.input_schema,
            })

        self._total_matched += len(matched)

        result = DiscoveryResult(
            matched_tools=matched,
            query=query,
            total_indexed=len(tools),
            category_filter=category,
            fallback_triggered=False,
        )
        self._log_search(query, category, effective_top_k, result, start)
        return self._format_response(result)

    def _build_fallback(
        self,
        query: str,
        category: str | None,
        primary_tools: list[ToolConfig],
    ) -> DiscoveryResult:
        self._fallback_count += 1
        return DiscoveryResult(
            matched_tools=[],
            query=query,
            total_indexed=len(self._indexed_tools),
            category_filter=category,
            fallback_triggered=self._fallback_on_empty,
            primary_tools=[t.tool_meta.name for t in primary_tools],
        )

    def _format_response(self, result: DiscoveryResult) -> dict[str, Any]:
        if result.matched_tools:
            lines = [f"找到 {len(result.matched_tools)} 个相关 API 工具：\n"]
            for i, tool in enumerate(result.matched_tools, 1):
                params = _simplify_schema(tool.get("inputSchema", {}))
                params_str = json.dumps(params, ensure_ascii=False) if params else "{}"
                lines.append(
                    f"{i}. {tool['name']} (相关度: {tool['relevance_score']})\n"
                    f"   描述: {tool['description']}\n"
                    f"   参数: {params_str}"
                )
            text = "\n".join(lines)
        elif result.fallback_triggered and result.primary_tools:
            tool_list = "\n".join(f"- {name}" for name in result.primary_tools)
            text = (
                f"未找到与\u201c{result.query}\u201d相关的 API 工具。"
                f"当前系统可能不支持此功能。\n\n"
                f"以下是当前可用的核心 API：\n{tool_list}"
            )
        else:
            text = f"未找到与\u201c{result.query}\u201d相关的 API 工具。当前系统可能不支持此功能。"

        structured: dict[str, Any] = {
            "matched_tools": result.matched_tools,
            "query": result.query,
            "total_indexed": result.total_indexed,
            "category_filter": result.category_filter,
            "fallback_triggered": result.fallback_triggered,
        }
        if result.fallback_triggered:
            structured["primary_tools"] = result.primary_tools

        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": structured,
        }

    def _log_search(
        self,
        query: str,
        category: str | None,
        top_k: int,
        result: DiscoveryResult,
        start: float,
    ) -> None:
        latency_ms = int((time.perf_counter() - start) * 1000)
        top_score = (
            max((t["relevance_score"] for t in result.matched_tools), default=0.0)
            if result.matched_tools
            else 0.0
        )
        log_json(
            logger,
            logging.INFO,
            "search_apis_called",
            query=query,
            category_filter=category,
            top_k=top_k,
            total_indexed=result.total_indexed,
            matched_tools=len(result.matched_tools),
            top_score=top_score,
            fallback_triggered=result.fallback_triggered,
            latency_ms=latency_ms,
        )

    def increment_rate_limited(self) -> None:
        self._rate_limited_count += 1

    def describe(self) -> dict[str, Any]:
        return {
            "total_searches": self._total_searches,
            "avg_matched_tools": (
                round(self._total_matched / self._total_searches, 1)
                if self._total_searches
                else 0.0
            ),
            "fallback_count": self._fallback_count,
            "rate_limited_count": self._rate_limited_count,
            "index_tool_count": len(self._indexed_tools),
            "last_index_built_at": self._last_index_built_at,
        }
