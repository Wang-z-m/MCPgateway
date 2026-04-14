"""Data models for the intelligent tool discovery feature."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field


class SearchApisParams(BaseModel):
    query: str = Field(min_length=1)
    category: str | None = None
    top_k: int | None = None


@dataclass(slots=True)
class DiscoveryResult:
    matched_tools: list[dict[str, Any]] = field(default_factory=list)
    query: str = ""
    total_indexed: int = 0
    category_filter: str | None = None
    fallback_triggered: bool = False
    primary_tools: list[str] = field(default_factory=list)
