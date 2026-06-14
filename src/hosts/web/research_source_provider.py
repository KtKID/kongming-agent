"""Web Deep Research 来源 provider 工厂和用户工具 adapter。

本脚本把 Web 宿主已经注册的用户工具适配为 Deep Research 的
ResearchSourceProvider 协议。
作用是让 Web 装配层按配置和 tool registry 构造来源 provider，同时在关闭、
缺工具或配置不完整时返回可审计的诊断结果。
关键执行流程：WebResearchSourceProviderFactory.build 解析配置和 registry，
UserToolResearchSourceProviderAdapter.search 调用搜索工具并归一化候选，
UserToolResearchSourceProviderAdapter.fetch 调用可选读取工具或复用 search 正文。
关键函数：WebResearchSourceProviderFactory.build 构造 provider，adapter 的
search/fetch 完成协议适配和工具返回值归一化。
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

from application.agent_workflows.strategies.deep_research.contracts import (
    ResearchSourceCandidate,
    ResearchSourceProvider,
    ResearchSourceQuery,
    ResearchSourceRecord,
    SourceStatus,
    SourceTier,
)
from application.agent_workflows.strategies.deep_research.dedupe import (
    canonicalize_url,
    stable_source_id,
)
from core.contracts import ToolContext, ToolResult

DEFAULT_SEARCH_TOOL_NAMES = (
    "deep_research_search",
    "web_search",
    "search_web",
    "browser_search",
)
DEFAULT_FETCH_TOOL_NAMES = (
    "deep_research_fetch",
    "web_fetch",
    "fetch_url",
    "browser_fetch",
)
MAX_STORED_SEARCH_PAYLOAD_KEYS = 1024


@dataclass(frozen=True)
class WebResearchSourceProviderFactoryConfig:
    """Web 来源 provider 工厂配置，输入来自 Config 或测试 mapping，输出给 build。"""

    enabled: bool = True
    search_tool_name: str | None = None
    fetch_tool_name: str | None = None
    provider_name: str = "web_user_tool_research_source"
    search_tool_names: tuple[str, ...] = DEFAULT_SEARCH_TOOL_NAMES
    fetch_tool_names: tuple[str, ...] = DEFAULT_FETCH_TOOL_NAMES


@dataclass(frozen=True)
class WebResearchSourceProviderDiagnostics:
    """工厂诊断结果，输入为构造过程状态，输出给 Web 装配日志或测试断言。"""

    enabled: bool
    provider_name: str
    search_tool_name: str | None = None
    fetch_tool_name: str | None = None
    reason: str | None = None
    missing_tools: tuple[str, ...] = ()
    fallback_reason: str | None = None


@dataclass(frozen=True)
class WebResearchSourceProviderBuildResult:
    """工厂构造结果，输入为 provider 和诊断信息，输出给 Web manager 注入。"""

    provider: ResearchSourceProvider | None
    diagnostics: WebResearchSourceProviderDiagnostics


@dataclass(frozen=True)
class _StoredSearchPayload:
    """保存 search 返回的正文，输入为候选 key 和正文，输出给 fetch 降级复用。"""

    content_text: str | None = None
    raw: Mapping[str, object] = field(default_factory=dict)


class WebResearchSourceProviderFactory:
    """按 Web 配置和工具 registry 构造 deep_research 来源 provider。"""

    def __init__(self, config: object | None = None) -> None:
        """初始化工厂，输入为可选 Config/mapping，输出为可 build 的实例。"""
        self._config = _factory_config_from(config)

    @property
    def config(self) -> WebResearchSourceProviderFactoryConfig:
        """返回标准化配置，输入为工厂状态，输出为 dataclass 配置。"""
        return self._config

    def build(self, registry: object | None) -> WebResearchSourceProviderBuildResult:
        """构造来源 provider，输入为工具 registry，输出 provider 或诊断原因。"""
        cfg = self._config
        if not cfg.enabled:
            return WebResearchSourceProviderBuildResult(
                provider=None,
                diagnostics=WebResearchSourceProviderDiagnostics(
                    enabled=False,
                    provider_name=cfg.provider_name,
                    reason="disabled_by_config",
                    fallback_reason="deep_research source provider disabled by config",
                ),
            )

        search_name = cfg.search_tool_name or _first_available_tool(registry, cfg.search_tool_names)
        if not search_name:
            return WebResearchSourceProviderBuildResult(
                provider=None,
                diagnostics=WebResearchSourceProviderDiagnostics(
                    enabled=True,
                    provider_name=cfg.provider_name,
                    reason="search_tool_missing",
                    missing_tools=cfg.search_tool_names,
                    fallback_reason="no configured or default search tool is registered",
                ),
            )
        search_tool = _lookup_tool(registry, search_name)
        if search_tool is None:
            return WebResearchSourceProviderBuildResult(
                provider=None,
                diagnostics=WebResearchSourceProviderDiagnostics(
                    enabled=True,
                    provider_name=cfg.provider_name,
                    search_tool_name=search_name,
                    reason="search_tool_missing",
                    missing_tools=(search_name,),
                    fallback_reason=f"search tool is not registered: {search_name}",
                ),
            )

        fetch_name = cfg.fetch_tool_name or _first_available_tool(registry, cfg.fetch_tool_names)
        fetch_tool = _lookup_tool(registry, fetch_name) if fetch_name else None
        missing_fetch = (fetch_name,) if fetch_name and fetch_tool is None else ()
        provider = UserToolResearchSourceProviderAdapter(
            search_tool=search_tool,
            fetch_tool=fetch_tool,
            search_tool_name=search_name,
            fetch_tool_name=fetch_name if fetch_tool is not None else None,
            name=cfg.provider_name,
        )
        return WebResearchSourceProviderBuildResult(
            provider=provider,
            diagnostics=WebResearchSourceProviderDiagnostics(
                enabled=True,
                provider_name=cfg.provider_name,
                search_tool_name=search_name,
                fetch_tool_name=fetch_name if fetch_tool is not None else None,
                reason="ok",
                missing_tools=missing_fetch,
                fallback_reason=(
                    f"fetch tool is not registered: {fetch_name}" if missing_fetch else None
                ),
            ),
        )


class UserToolResearchSourceProviderAdapter:
    """把用户工具 registry 中的 search/fetch 工具适配为 ResearchSourceProvider。"""

    def __init__(
        self,
        *,
        search_tool: object,
        fetch_tool: object | None = None,
        search_tool_name: str,
        fetch_tool_name: str | None = None,
        name: str = "web_user_tool_research_source",
    ) -> None:
        """初始化 adapter，输入为搜索工具和可选读取工具，输出为 provider 实例。"""
        if not name:
            raise ValueError("name must be non-empty")
        if not search_tool_name:
            raise ValueError("search_tool_name must be non-empty")
        self.name = name
        self._search_tool = search_tool
        self._fetch_tool = fetch_tool
        self._search_tool_name = search_tool_name
        self._fetch_tool_name = fetch_tool_name
        self._payloads_by_key: dict[str, _StoredSearchPayload] = {}

    async def search(self, query: ResearchSourceQuery) -> tuple[ResearchSourceCandidate, ...]:
        """调用用户搜索工具，输入为 ResearchSourceQuery，输出候选来源列表。"""
        raw = await _call_user_tool(
            self._search_tool,
            {
                "query": query.line,
                "line": query.line,
                "query_id": query.query_id,
                "intent": query.intent,
                "max_results": query.max_results,
            },
            tool_name=self._search_tool_name,
        )
        items = _extract_search_items(raw)
        candidates: list[ResearchSourceCandidate] = []
        for index, item in enumerate(items[: max(query.max_results, 0)]):
            candidate = _candidate_from_mapping(
                item,
                query=query,
                rank=index + 1,
                provider_name=self.name,
            )
            if not candidate.url:
                continue
            candidates.append(candidate)
            _store_payload(
                self._payloads_by_key,
                candidate,
                content_text=_search_content_text(item),
                raw=item,
            )
        return tuple(candidates)

    async def fetch(self, candidate: ResearchSourceCandidate) -> ResearchSourceRecord:
        """调用可选读取工具，输入为候选来源，输出结构化来源记录。"""
        if self._fetch_tool is None:
            stored = _lookup_payload(self._payloads_by_key, candidate)
            if stored is not None and stored.content_text:
                return _record_from_payload(
                    candidate,
                    stored.raw,
                    content_text=stored.content_text,
                    tier="weak",
                )
            return _weak_record(
                candidate,
                error_code="fetch_tool_unavailable",
                error_message="fetch tool is not configured and search result has no content_text",
            )

        try:
            raw = await _call_user_tool(
                self._fetch_tool,
                {
                    "url": candidate.url,
                    "canonical_url": candidate.canonical_url,
                    "source_id": candidate.source_id,
                    "title": candidate.title,
                    "candidate": _candidate_to_dict(candidate),
                },
                tool_name=self._fetch_tool_name or "fetch",
            )
        except Exception as exc:
            return _weak_record(
                candidate,
                status="failed",
                error_code=type(exc).__name__.lower(),
                error_message=str(exc),
            )
        payload = _extract_fetch_payload(raw)
        content_text = _text_field(payload, "content_text", "content", "text", "markdown")
        if content_text:
            return _record_from_payload(candidate, payload, content_text=content_text)
        return _weak_record(
            candidate,
            error_code="content_text_missing",
            error_message="fetch tool returned no content_text/content/text/markdown",
        )


async def _call_user_tool(tool: object, args: dict[str, object], *, tool_name: str) -> object:
    """调用用户工具，输入为 Tool 或 callable 和参数，输出原始返回值。"""
    execute = getattr(tool, "execute", None)
    if callable(execute):
        result = execute(
            dict(args),
            ToolContext(
                run_id="web-deep-research-source-provider",
                session_id="web-deep-research",
                turn=0,
                call_id=f"deep_research:{tool_name}",
                metadata={"origin": "web_deep_research_source_provider"},
            ),
        )
        value = await result if inspect.isawaitable(result) else result
        return _unwrap_tool_result(value, tool_name=tool_name)

    if callable(tool):
        value = tool(dict(args))
        resolved = await value if inspect.isawaitable(value) else value
        return _unwrap_tool_result(resolved, tool_name=tool_name)

    raise TypeError(f"tool is neither Tool nor callable: {tool_name}")


def _unwrap_tool_result(value: object, *, tool_name: str) -> object:
    """解包 ToolResult，输入为工具返回值，输出 data 或 content。"""
    if isinstance(value, ToolResult):
        if not value.ok:
            message = value.error_message or value.content or f"tool failed: {tool_name}"
            raise RuntimeError(message)
        return value.data if value.data is not None else {"content": value.content}
    return value


def _extract_search_items(raw: object) -> list[Mapping[str, object]]:
    """提取搜索结果列表，输入为工具原始返回，输出 dict item 列表。"""
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return [item for item in raw if isinstance(item, Mapping)]
    if not isinstance(raw, Mapping):
        return []
    for key in (
        "results",
        "items",
        "web_results",
        "organic",
        "organic_results",
        "sources",
        "hits",
        "data",
    ):
        value = raw.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [item for item in value if isinstance(item, Mapping)]
    if raw.get("url"):
        return [raw]
    return []


def _extract_fetch_payload(raw: object) -> Mapping[str, object]:
    """提取 fetch 载荷，输入为工具原始返回，输出正文 payload。"""
    if isinstance(raw, Mapping):
        for key in ("source", "result", "data", "page"):
            value = raw.get(key)
            if isinstance(value, Mapping):
                return value
        return raw
    if isinstance(raw, str):
        return {"content_text": raw}
    return {}


def _candidate_from_mapping(
    item: Mapping[str, object],
    *,
    query: ResearchSourceQuery,
    rank: int,
    provider_name: str,
) -> ResearchSourceCandidate:
    """从工具返回 dict 构造候选，输入为 item 和 query，输出 ResearchSourceCandidate。"""
    url = _string_field(item, "url", "link", "href")
    canonical = _string_field(item, "canonical_url") or canonicalize_url(url)
    source_id = _string_field(item, "source_id", "id") or stable_source_id(canonical or url)
    return ResearchSourceCandidate(
        source_id=source_id,
        query_id=_string_field(item, "query_id") or query.query_id,
        url=url,
        canonical_url=canonical,
        title=_string_field(item, "title", "name"),
        snippet=_string_field(item, "snippet", "summary", "description"),
        rank=_int_field(item, "rank", default=rank),
        provider_name=_string_field(item, "provider_name") or provider_name,
    )


def _record_from_payload(
    candidate: ResearchSourceCandidate,
    payload: Mapping[str, object],
    *,
    content_text: str,
    tier: SourceTier = "strong",
) -> ResearchSourceRecord:
    """从工具 payload 构造 fetched 记录，输入为候选和正文，输出 ResearchSourceRecord。"""
    return ResearchSourceRecord(
        source_id=_string_field(payload, "source_id", "id") or candidate.source_id,
        query_id=_string_field(payload, "query_id") or candidate.query_id,
        url=_string_field(payload, "url", "link", "href") or candidate.url,
        canonical_url=_string_field(payload, "canonical_url") or candidate.canonical_url,
        title=_string_field(payload, "title", "name") or candidate.title,
        status="fetched",
        tier=tier,
        content_text=content_text,
        error_code=None,
        error_message=None,
        provider_name=_string_field(payload, "provider_name") or candidate.provider_name,
        rank=_int_field(payload, "rank", default=candidate.rank),
    )


def _search_content_text(item: Mapping[str, object]) -> str | None:
    """从搜索结果提取摘要正文，输入 item，输出可用于弱来源记录的正文。"""
    return _text_field(
        item,
        "content_text",
        "content",
        "text",
        "markdown",
        "snippet",
        "summary",
        "description",
    )


def _weak_record(
    candidate: ResearchSourceCandidate,
    *,
    status: SourceStatus = "failed",
    error_code: str,
    error_message: str,
) -> ResearchSourceRecord:
    """构造弱来源记录，输入为候选和失败原因，输出 ResearchSourceRecord。"""
    return ResearchSourceRecord(
        source_id=candidate.source_id,
        query_id=candidate.query_id,
        url=candidate.url,
        canonical_url=candidate.canonical_url,
        title=candidate.title,
        status=status,
        tier="weak",
        content_text=None,
        error_code=error_code,
        error_message=error_message,
        provider_name=candidate.provider_name,
        rank=candidate.rank,
    )


def _store_payload(
    store: dict[str, _StoredSearchPayload],
    candidate: ResearchSourceCandidate,
    *,
    content_text: str | None,
    raw: Mapping[str, object],
) -> None:
    """保存搜索正文，输入为候选和原始 item，输出为内部缓存更新。"""
    payload = _StoredSearchPayload(content_text=content_text, raw=raw)
    for key in _candidate_keys(candidate):
        store[key] = payload
    _trim_payload_store(store)


def _trim_payload_store(store: dict[str, _StoredSearchPayload]) -> None:
    """限制搜索正文缓存大小，输入缓存 dict，输出为原地裁剪后的缓存。"""
    overflow = len(store) - MAX_STORED_SEARCH_PAYLOAD_KEYS
    for _ in range(max(overflow, 0)):
        store.pop(next(iter(store)))


def _lookup_payload(
    store: Mapping[str, _StoredSearchPayload],
    candidate: ResearchSourceCandidate,
) -> _StoredSearchPayload | None:
    """读取搜索正文缓存，输入为候选，输出已保存 payload 或 None。"""
    for key in _candidate_keys(candidate):
        payload = store.get(key)
        if payload is not None:
            return payload
    return None


def _candidate_keys(candidate: ResearchSourceCandidate) -> tuple[str, ...]:
    """生成候选缓存 key，输入为候选，输出 source/canonical/url key。"""
    return tuple(
        key for key in (candidate.source_id, candidate.canonical_url, candidate.url) if key
    )


def _candidate_to_dict(candidate: ResearchSourceCandidate) -> dict[str, object]:
    """序列化候选，输入为 ResearchSourceCandidate，输出工具参数 dict。"""
    return {
        "source_id": candidate.source_id,
        "query_id": candidate.query_id,
        "url": candidate.url,
        "canonical_url": candidate.canonical_url,
        "title": candidate.title,
        "snippet": candidate.snippet,
        "rank": candidate.rank,
        "provider_name": candidate.provider_name,
    }


def _factory_config_from(config: object | None) -> WebResearchSourceProviderFactoryConfig:
    """解析工厂配置，输入为 Config/mapping/dataclass，输出标准化配置。"""
    raw = _resolve_raw_config(config)
    if raw is None:
        return WebResearchSourceProviderFactoryConfig()
    return WebResearchSourceProviderFactoryConfig(
        enabled=_bool_value(_read_value(raw, "enabled"), default=True),
        search_tool_name=_optional_string(_read_value(raw, "search_tool_name")),
        fetch_tool_name=_optional_string(_read_value(raw, "fetch_tool_name")),
        provider_name=_optional_string(_read_value(raw, "provider_name"))
        or "web_user_tool_research_source",
        search_tool_names=_string_tuple(_read_value(raw, "search_tool_names"))
        or DEFAULT_SEARCH_TOOL_NAMES,
        fetch_tool_names=_string_tuple(_read_value(raw, "fetch_tool_names"))
        or DEFAULT_FETCH_TOOL_NAMES,
    )


def _resolve_raw_config(config: object | None) -> object | None:
    """解析配置节点，输入为任意配置对象，输出 deep_research source 子配置。"""
    if config is None:
        return None
    if isinstance(config, WebResearchSourceProviderFactoryConfig):
        return config
    if isinstance(config, Mapping):
        return (
            config.get("deep_research_source_provider")
            or config.get("research_source_provider")
            or config
        )
    for attr in ("deep_research_source_provider", "research_source_provider"):
        value = getattr(config, attr, None)
        if value is not None:
            return cast(object, value)
    web_cfg = getattr(config, "web", None)
    if web_cfg is not None:
        for attr in ("deep_research_source_provider", "research_source_provider"):
            value = getattr(web_cfg, attr, None)
            if value is not None:
                return cast(object, value)
    return None


def _first_available_tool(registry: object | None, names: Sequence[str]) -> str | None:
    """查找第一个可用工具名，输入 registry 和候选名，输出命中名称。"""
    for name in names:
        if _lookup_tool(registry, name) is not None:
            return name
    return None


def _lookup_tool(registry: object | None, name: str | None) -> object | None:
    """从 registry 查找工具，输入 registry 和名称，输出工具对象或 None。"""
    if registry is None or not name:
        return None
    get = getattr(registry, "get", None)
    if callable(get):
        value = get(name)
        if value is not None:
            return cast(object, value)
    if isinstance(registry, Mapping):
        return cast(object | None, registry.get(name))
    try:
        if name in registry:  # type: ignore[operator]
            return cast(object, registry[name])  # type: ignore[index]
    except Exception:
        return None
    return None


def _read_value(raw: object, key: str) -> object:
    """读取配置字段，输入为 mapping/dataclass 和 key，输出字段值。"""
    if isinstance(raw, WebResearchSourceProviderFactoryConfig):
        return getattr(raw, key)
    if isinstance(raw, Mapping):
        return raw.get(key)
    return getattr(raw, key, None)


def _string_field(item: Mapping[str, object], *keys: str) -> str:
    """读取字符串字段，输入为 dict 和候选 key，输出去空白字符串。"""
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _text_field(item: Mapping[str, object], *keys: str) -> str | None:
    """读取正文文本字段，输入为 dict 和候选 key，输出正文或 None。"""
    value = _string_field(item, *keys)
    return value or None


def _optional_string(value: object) -> str | None:
    """读取可选字符串，输入为任意值，输出去空白字符串或 None。"""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _string_tuple(value: object) -> tuple[str, ...]:
    """读取字符串序列，输入为任意值，输出字符串元组。"""
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, Sequence):
        return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    return ()


def _bool_value(value: object, *, default: bool) -> bool:
    """读取布尔配置，输入为任意值和默认值，输出 bool。"""
    if isinstance(value, bool):
        return value
    return default


def _int_field(item: Mapping[str, object], key: str, *, default: int) -> int:
    """读取整数字段，输入为 dict 和 key，输出整数。"""
    value = item.get(key)
    return value if isinstance(value, int) else default


__all__ = [
    "UserToolResearchSourceProviderAdapter",
    "WebResearchSourceProviderBuildResult",
    "WebResearchSourceProviderDiagnostics",
    "WebResearchSourceProviderFactory",
    "WebResearchSourceProviderFactoryConfig",
]
