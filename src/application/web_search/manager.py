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
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any, ClassVar
from urllib.parse import urlparse

from application.web_search.models import WebSearchResponse, WebSearchResult
from core.contracts import (
    PreparedToolCall,
    ToolCallPreparer,
    ToolContext,
    ToolResult,
)

# 搜索结果预览长度先集中放在模块顶部，方便根据 eval 调整。
_SEARCH_SNIPPET_MAX_CHARS = 300
# 搜索数量字符串最多允许的十进制位数，避免超长数字触发 Python int 保护异常。
_SEARCH_LIMIT_MAX_DIGITS = 9


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

    async def search(
        self,
        query: str,
        max_results: int | None = None,
        *,
        top_k: int | None = None,
    ) -> WebSearchResponse:
        """执行网页搜索，输入 query 和可选数量限制，输出归一化 WebSearchResponse。"""
        normalized_query = str(query).strip()
        if not normalized_query:
            raise ValueError("query must be non-empty")
        requested_limit = max_results if max_results is not None else top_k
        if requested_limit is not None:
            normalized_limit = _parse_positive_int(requested_limit, allow_string=False)
            if normalized_limit is None:
                raise ValueError("max_results/top_k must be an integer >= 1")
            requested_limit = normalized_limit

        args: dict[str, Any] = {"query": normalized_query}
        if requested_limit is not None:
            args["max_results"] = requested_limit

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

        limit = requested_limit if requested_limit is not None else len(items)
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
        """准备并调用底层搜索 Tool，输出 ToolResult 或兼容对象。"""
        execute = getattr(self._search_tool, "execute", None)
        if not callable(execute):
            raise TypeError("search_tool must provide execute(prepared, ctx)")
        context = ToolContext(
            run_id="web-search-manager",
            session_id="web-search",
            turn=0,
            call_id=f"web_search:{self._provider_tool_name}",
            metadata={"origin": "application.web_search"},
        )
        prepared = (
            self._search_tool.prepare(dict(args), context)
            if isinstance(self._search_tool, ToolCallPreparer)
            else PreparedToolCall(arguments=dict(args))
        )
        value = execute(
            prepared,
            context,
        )
        return await value if inspect.isawaitable(value) else value

    def _base_diagnostics(self, result: object) -> dict[str, Any]:
        """构造响应诊断，输入底层结果，输出 provider/tool/错误信息。"""
        data = _result_data(result)
        diagnostics = _merge_diagnostics_pessimistic(
            _mapping_or_empty(data.get("diagnostics")),
            _mapping_or_empty(data.get("mcp_diagnostics")),
            _mapping_or_empty(_result_field(result, "diagnostics")),
        )
        ok = bool(_result_field(result, "ok", True))
        if diagnostics.get("ok") is False:
            ok = False
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
        published_date = _text_field(
            item,
            "published_date",
            "published_at",
            "publishedAt",
            "publish_time",
            "published_time",
            "date",
        )
        score = _float_field(item, "score", "ranking_score", "confidence")
        if score is None:
            score = _float_field(metadata, "score")
        return WebSearchResult(
            url=url,
            title=_text_field(item, "title", "name", "headline") or url,
            snippet=_truncate_text(
                _text_field(item, "snippet", "summary", "description", "content", "text") or "",
                max_chars=_SEARCH_SNIPPET_MAX_CHARS,
            ),
            provider_name=provider_name,
            provider_tool_name=provider_tool_name,
            domain=_text_field(item, "domain", "host") or urlparse(url).netloc,
            published_date=published_date,
            score=score if score is not None else 0.0,
            published_at=published_date,
            metadata=metadata,
        )


def build_web_search_tool(manager: WebSearchManager) -> object:
    """构造 Kongming web_search Tool，输入 WebSearchManager，输出 Tool 实例。"""
    return _WebSearchTool(manager)


def build_missing_web_search_tool(
    *,
    provider_name: str = "web_search",
    candidate_tool_names: tuple[str, ...] = (),
) -> object:
    """构造缺失态 web_search Tool，输入 provider 和候选工具名，输出稳定失败工具。"""
    return _MissingWebSearchTool(
        provider_name=provider_name,
        candidate_tool_names=candidate_tool_names,
    )


class _WebSearchTool:
    """把 WebSearchManager 暴露为 Kongming Tool。"""

    name = "web_search"
    description = "Search the web and return normalized URL, title, snippet, and provider data."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {
                "anyOf": [
                    {"type": "integer", "minimum": 1},
                    {
                        "type": "string",
                        "pattern": f"^[1-9][0-9]{{0,{_SEARCH_LIMIT_MAX_DIGITS - 1}}}$",
                    },
                ],
                "description": "Maximum number of search results to return.",
            },
            "top_k": {
                "anyOf": [
                    {"type": "integer", "minimum": 1},
                    {
                        "type": "string",
                        "pattern": f"^[1-9][0-9]{{0,{_SEARCH_LIMIT_MAX_DIGITS - 1}}}$",
                    },
                ],
                "description": "Alias for max_results.",
            },
        },
        "required": ["query"],
    }

    def __init__(self, manager: WebSearchManager) -> None:
        """初始化 Tool，输入 WebSearchManager，输出可注册工具。"""
        self._manager = manager

    def prepare(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> PreparedToolCall:
        """审批前校验 query/limit 并冻结规范化参数。"""
        del context
        return PreparedToolCall(arguments=_prepare_search_arguments(arguments))

    async def execute(
        self,
        prepared: PreparedToolCall,
        ctx: ToolContext,
    ) -> ToolResult:
        """执行 web_search，输入已准备调用/context，输出 ToolResult。"""
        del ctx
        query = str(prepared.arguments["query"])
        requested_limit = prepared.arguments.get("max_results")
        try:
            response = await self._manager.search(query, max_results=requested_limit)
        except Exception as exc:
            error_message = _exception_message(exc)
            return ToolResult(
                ok=False,
                content=f"web_search failed: {error_message}",
                data={
                    "query": query,
                    "results": [],
                    "diagnostics": {
                        "ok": False,
                        "error_class": type(exc).__name__,
                        "error_message": error_message,
                    },
                },
                error_message=error_message,
            )
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


class _MissingWebSearchTool:
    """缺失底层 provider 时的稳定 web_search Tool。"""

    name = "web_search"
    description = "Search the web and return normalized URL, title, snippet, and provider data."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {
                "anyOf": [
                    {"type": "integer", "minimum": 1},
                    {
                        "type": "string",
                        "pattern": f"^[1-9][0-9]{{0,{_SEARCH_LIMIT_MAX_DIGITS - 1}}}$",
                    },
                ],
                "description": "Maximum number of search results to return.",
            },
            "top_k": {
                "anyOf": [
                    {"type": "integer", "minimum": 1},
                    {
                        "type": "string",
                        "pattern": f"^[1-9][0-9]{{0,{_SEARCH_LIMIT_MAX_DIGITS - 1}}}$",
                    },
                ],
                "description": "Alias for max_results.",
            },
        },
        "required": ["query"],
    }

    def __init__(
        self,
        *,
        provider_name: str,
        candidate_tool_names: tuple[str, ...],
    ) -> None:
        """初始化缺失态工具，输入 provider 和候选工具名，输出可调用实例。"""
        self._provider_name = provider_name
        self._candidate_tool_names = tuple(candidate_tool_names)

    def prepare(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> PreparedToolCall:
        """审批前校验查询参数并返回统一快照。"""
        del context
        return PreparedToolCall(arguments=_prepare_search_arguments(arguments))

    async def execute(
        self,
        prepared: PreparedToolCall,
        ctx: ToolContext,
    ) -> ToolResult:
        """执行缺失态搜索，输入已准备参数，输出工具缺失结果。"""
        del ctx
        query = str(prepared.arguments["query"])
        message = "web_search 工具缺失：未连接 MCP 搜索工具或用户搜索工具。"
        return ToolResult(
            ok=False,
            content=message,
            data={
                "query": query,
                "results": [],
                "diagnostics": {
                    "ok": False,
                    "reason": "search_tool_missing",
                    "error_message": message,
                    "provider_name": self._provider_name,
                    "candidate_tool_names": self._candidate_tool_names,
                },
            },
            error_message=message,
        )


def _prepare_search_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化搜索 query 和 limit，输出审批/执行共同参数。"""
    query = _optional_str(arguments.get("query"))
    if not query:
        raise ValueError("web_search query must be non-empty")
    limit_name = "max_results" if arguments.get("max_results") is not None else "top_k"
    requested_limit = arguments.get("max_results")
    if requested_limit is None:
        requested_limit = arguments.get("top_k")
    if requested_limit is None:
        return {"query": query}
    parsed_limit = _parse_positive_int(requested_limit, allow_string=True)
    if parsed_limit is None:
        raise ValueError(f"{limit_name} must be an integer >= 1")
    return {"query": query, "max_results": parsed_limit}


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
        if len(stripped) > _SEARCH_LIMIT_MAX_DIGITS:
            return None
        try:
            parsed = int(stripped)
        except ValueError:
            return None
        return parsed if parsed >= 1 else None
    return None


def _exception_message(exc: Exception) -> str:
    """生成异常消息，输入异常，输出适合 ToolResult 的短文本。"""
    message = str(exc).strip()
    return message or exc.__class__.__name__


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


def _truncate_text(text: str, *, max_chars: int) -> str:
    """按字符数截断文本，输入原文和上限，输出短预览。"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


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


def _merge_diagnostics_pessimistic(*parts: Mapping[str, Any]) -> dict[str, Any]:
    """合并诊断信息，输入多层 diagnostics，输出任一失败即失败的结果。"""
    merged: dict[str, Any] = {}
    saw_failure = False
    for part in parts:
        if part.get("ok") is False:
            saw_failure = True
        merged.update(dict(part))
    if saw_failure:
        merged["ok"] = False
    return merged


def _text_field(mapping: Mapping[str, Any], *keys: str) -> str | None:
    """读取文本字段，输入 mapping 和候选 key，输出字符串或 None。"""
    for key in keys:
        value = mapping.get(key)
        text = _optional_str(value)
        if text:
            return text
    return None


def _float_field(mapping: Mapping[str, Any], *keys: str) -> float | None:
    """读取数值字段，输入 mapping 和候选 key，输出 float 或 None。"""
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            parsed = float(value)
            return parsed if math.isfinite(parsed) else None
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                continue
            try:
                parsed = float(stripped)
            except ValueError:
                continue
            if math.isfinite(parsed):
                return parsed
    return None


def _optional_str(value: object) -> str | None:
    """把可选值转成字符串，输入任意对象，输出字符串或 None。"""
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


__all__ = ["WebSearchManager", "build_web_search_tool"]
