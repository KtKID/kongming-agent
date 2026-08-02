"""通用 Web Search 应用层入口。"""

from __future__ import annotations

from application.web_search.manager import (
    WebSearchManager,
    build_missing_web_search_tool,
    build_web_search_tool,
)
from application.web_search.models import WebSearchResponse, WebSearchResult

__all__ = [
    "WebSearchManager",
    "WebSearchResponse",
    "WebSearchResult",
    "build_missing_web_search_tool",
    "build_web_search_tool",
]
