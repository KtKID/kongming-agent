"""Web Search 应用层数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class WebSearchResult:
    """归一化搜索结果。"""

    url: str
    title: str
    snippet: str
    provider_name: str
    provider_tool_name: str
    domain: str = ""
    published_date: str | None = None
    score: float = 0.0
    published_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WebSearchResponse:
    """通用 Web Search 响应。"""

    query: str
    results: tuple[WebSearchResult, ...]
    diagnostics: dict[str, Any] = field(default_factory=dict)


__all__ = ["WebSearchResponse", "WebSearchResult"]
