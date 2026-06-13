"""通用 Web Search Manager。

本模块把底层搜索 Tool 封装为应用层 `WebSearchManager`，提供稳定的
`search(query, max_results)` 入口，并把 provider 返回的常见 data shape
归一化成 `WebSearchResponse`。关键流程：调用底层 Tool，抽取 results/item/list，
规范 URL/title/snippet/provider 字段，最后通过 `build_web_search_tool` 暴露
Kongming `web_search` Tool。
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any, ClassVar

from application.web_search.models import WebSearchResponse, WebSearchResult
from core.contracts import ToolContext, ToolResult


class WebSearchManager:
    """通用 Web Search 边界类。"""

    def __init__(
        self,
        search_tool: object,
        *,
        provider_name: str = "web_search",
        provider_tool_name: str | None = None,
    ) -> None:
        """初始化 manager，输入底层搜索 Tool，输出可执行 search 的实例。"""
        self._search_tool = search_tool
        self._provider_name = provider_name
        self._provider_tool_name = provider_tool_name or str(
            getattr(search_tool, "name", "web_search_provider")
        )

    async def search(self, query: str, max_results: int | None = None) -> WebSearchResponse:
        """执行网页搜索，输入 query 和可选数量限制，输出归一化 WebSearchResponse。"""
        normalized_query = str(query).strip()
        if not normalized_query:
            raise ValueError("query must be non-empty")
        if max_results is not None:
            normalized_max_results = _parse_positive_int(max_results, allow_string=False)
            if normalized_max_results is None:
                raise ValueError("max_results must be an integer >= 1")
            max_results = normalized_max_results

        args: dict[str, Any] = {"query": normalized_query}
        if max_results is not None:
            args["max_results"] = max_results

        result = await self._execute_search_tool(args)
        tool_data = _result_data(result)
        diagnostics = self._base_diagnostics(result)

        content_text = _result_content(result)
        provider_error_message = _provider_error_message(tool_data, content_text)
        if provider_error_message:
            diagnostics["ok"] = False
            diagnostics["error_message"] = provider_error_message

        content_payload = _json_mapping_or_sequence(content_text)
        items = _extract_search_items(tool_data)
        if not items and content_payload is not None:
            items = _extract_search_items(content_payload)

        limit = max_results if max_results is not None else len(items)
        normalized_results: list[WebSearchResult] = []
        skipped: list[dict[str, Any]] = []
        for index, item in enumerate(items[: max(limit, 0)]):
            search_result = self._normalize_item(item, rank=index + 1, root_data=tool_data)
            if search_result is None:
                skipped.append({"rank": index + 1, "reason": "url_missing", "item": dict(item)})
                continue
            normalized_results.append(search_result)

        diagnostics["result_count"] = len(normalized_results)
        if skipped:
            diagnostics["skipped_results"] = tuple(skipped)

        return WebSearchResponse(
            query=normalized_query,
            results=tuple(normalized_results),
            diagnostics=diagnostics,
        )

    async def _execute_search_tool(self, args: dict[str, Any]) -> object:
        """调用底层搜索 Tool，输入参数 dict，输出 ToolResult 或兼容对象。"""
        execute = getattr(self._search_tool, "execute", None)
        if not callable(execute):
            raise TypeError("search_tool must provide execute(args, ctx)")
        value = execute(
            args,
            ToolContext(
                run_id="web-search-manager",
                session_id="web-search",
                turn=0,
                call_id=f"web_search:{self._provider_tool_name}",
                metadata={"origin": "application.web_search"},
            ),
        )
        return await value if inspect.isawaitable(value) else value

    def _base_diagnostics(self, result: object) -> dict[str, Any]:
        """构造响应诊断，输入底层结果，输出 provider/tool/错误信息。"""
        data = _result_data(result)
        diagnostics = _mapping_or_empty(data.get("diagnostics"))
        diagnostics.update(_mapping_or_empty(data.get("mcp_diagnostics")))
        result_diagnostics = _mapping_or_empty(_result_field(result, "diagnostics"))
        diagnostics.update(result_diagnostics)
        ok = bool(_result_field(result, "ok", True))
        diagnostics.update(
            {
                "ok": ok,
                "provider_name": _text_field(data, "provider_name", "provider")
                or self._provider_name,
                "provider_tool_name": _text_field(
                    data,
                    "provider_tool_name",
                    "tool_name",
                    "kongming_tool_name",
                )
                or self._provider_tool_name,
            }
        )
        error_message = _optional_str(_result_field(result, "error_message"))
        if error_message:
            diagnostics["error_message"] = error_message
        return diagnostics

    def _normalize_item(
        self,
        item: Mapping[str, Any],
        *,
        rank: int,
        root_data: Mapping[str, Any],
    ) -> WebSearchResult | None:
        """归一化单条结果，输入 provider item，输出 WebSearchResult。"""
        url = _text_field(item, "url", "link", "href", "source_url")
        if not url:
            return None
        provider_name = (
            _text_field(item, "provider_name", "provider")
            or _text_field(root_data, "provider_name", "provider")
            or self._provider_name
        )
        provider_tool_name = (
            _text_field(item, "provider_tool_name", "tool_name", "kongming_tool_name")
            or _text_field(root_data, "provider_tool_name", "tool_name", "kongming_tool_name")
            or self._provider_tool_name
        )
        metadata = _metadata_for_item(item, rank=rank)
        return WebSearchResult(
            url=url,
            title=_text_field(item, "title", "name", "headline") or url,
            snippet=_text_field(item, "snippet", "summary", "description", "content", "text") or "",
            provider_name=provider_name,
            provider_tool_name=provider_tool_name,
            published_at=_text_field(
                item,
                "published_at",
                "publishedAt",
                "publish_time",
                "published_time",
                "date",
            ),
            metadata=metadata,
        )


def build_web_search_tool(manager: WebSearchManager) -> object:
    """构造 Kongming web_search Tool，输入 WebSearchManager，输出 Tool 实例。"""
    return _WebSearchTool(manager)


class _WebSearchTool:
    """把 WebSearchManager 暴露为 Kongming Tool。"""

    name = "web_search"
    description = "Search the web and return normalized URL, title, snippet, and provider data."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "description": "Maximum number of search results to return.",
            },
        },
        "required": ["query"],
    }

    def __init__(self, manager: WebSearchManager) -> None:
        """初始化 Tool，输入 WebSearchManager，输出可注册工具。"""
        self._manager = manager

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行 web_search，输入 Tool args/context，输出 ToolResult。"""
        query = _optional_str(args.get("query"))
        if not query:
            return ToolResult(
                ok=False,
                content="web_search requires a non-empty query.",
                error_message="query must be non-empty",
            )
        max_results = args.get("max_results")
        if max_results is not None:
            max_results = _parse_positive_int(max_results, allow_string=True)
            if max_results is None:
                return ToolResult(
                    ok=False,
                    content="web_search max_results must be an integer >= 1.",
                    error_message="max_results must be an integer >= 1",
                )
        response = await self._manager.search(query, max_results=max_results)
        data = {
            "query": response.query,
            "results": [asdict(result) for result in response.results],
            "diagnostics": dict(response.diagnostics),
        }
        ok = bool(response.diagnostics.get("ok", True))
        return ToolResult(
            ok=ok,
            content=_format_content(response),
            data=data,
            error_message=None if ok else _optional_str(response.diagnostics.get("error_message")),
        )


def _parse_positive_int(value: object, *, allow_string: bool) -> int | None:
    """解析正整数，输入 tool 参数值，输出正整数或 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 1 else None
    if allow_string and isinstance(value, str):
        stripped = value.strip()
        if not stripped.isdecimal():
            return None
        parsed = int(stripped)
        return parsed if parsed >= 1 else None
    return None


def _extract_search_items(value: object) -> list[Mapping[str, Any]]:
    """抽取搜索结果列表，输入 provider payload，输出 item mappings。"""
    if isinstance(value, Mapping):
        for key in (
            "results",
            "items",
            "web_results",
            "organic",
            "organic_results",
            "sources",
            "hits",
            "data",
            "result",
        ):
            nested = value.get(key)
            items = _extract_search_items(nested)
            if items:
                return items
        if _text_field(value, "url", "link", "href", "source_url"):
            return [value]
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _metadata_for_item(item: Mapping[str, Any], *, rank: int) -> dict[str, Any]:
    """构造结果 metadata，输入原始 item 和 rank，输出 metadata dict。"""
    metadata = _mapping_or_empty(item.get("metadata"))
    metadata.setdefault("rank", rank)
    metadata.setdefault("raw", dict(item))
    return metadata


def _format_content(response: WebSearchResponse) -> str:
    """生成 ToolResult content，输入 WebSearchResponse，输出可读文本。"""
    if not response.results:
        return f"No web search results found for: {response.query}"
    lines = [f"Web search results for: {response.query}"]
    for index, result in enumerate(response.results, start=1):
        snippet = f" - {result.snippet}" if result.snippet else ""
        lines.append(f"{index}. {result.title} ({result.url}){snippet}")
    return "\n".join(lines)


def _result_data(value: object) -> dict[str, Any]:
    """提取 ToolResult.data，输入 ToolResult 或兼容对象，输出 dict。"""
    data = _result_field(value, "data")
    if isinstance(data, Mapping):
        return dict(data)
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _result_content(value: object) -> str | None:
    """提取 ToolResult.content，输入 ToolResult 或兼容对象，输出字符串。"""
    return _optional_str(_result_field(value, "content"))


def _provider_error_message(data: Mapping[str, Any], content: str | None) -> str | None:
    """识别 provider 错误文本，输入 data/content，输出错误消息或 None。"""
    raw_result = _mapping_or_empty(data.get("raw_result"))
    if raw_result.get("isError") is True:
        return content or "provider returned isError=true"
    base_resp = _mapping_or_empty(data.get("base_resp"))
    if not base_resp:
        base_resp = _mapping_or_empty(raw_result.get("base_resp"))
    status_code = base_resp.get("status_code")
    if status_code is not None and str(status_code) not in {"0", "200"}:
        status_msg = _optional_str(base_resp.get("status_msg")) or content
        return status_msg or f"provider returned status_code={status_code}"
    text = _optional_str(content)
    if text and text.lower().startswith(("failed to perform search:", "api error:")):
        return text
    return None


def _result_field(value: object, name: str, default: Any = None) -> Any:
    """读取结果字段，输入对象和字段名，输出字段值。"""
    if isinstance(value, Mapping) and name in value:
        return value[name]
    return getattr(value, name, default)


def _json_mapping_or_sequence(value: str | None) -> object | None:
    """尝试解析 JSON content，输入字符串，输出 mapping/sequence 或 None。"""
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, (Mapping, Sequence)) and not isinstance(parsed, (str, bytes, bytearray)):
        return parsed
    return None


def _mapping_or_empty(value: object) -> dict[str, Any]:
    """把 mapping 转成 dict，输入任意对象，输出 dict 或空 dict。"""
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _text_field(mapping: Mapping[str, Any], *keys: str) -> str | None:
    """读取文本字段，输入 mapping 和候选 key，输出字符串或 None。"""
    for key in keys:
        value = mapping.get(key)
        text = _optional_str(value)
        if text:
            return text
    return None


def _optional_str(value: object) -> str | None:
    """把可选值转成字符串，输入任意对象，输出字符串或 None。"""
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


__all__ = ["WebSearchManager", "build_web_search_tool"]
