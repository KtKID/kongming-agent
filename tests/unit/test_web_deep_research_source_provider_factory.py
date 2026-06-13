"""Web Deep Research source provider factory 单元测试。

本脚本验证 Web 宿主侧 provider factory 和用户工具 adapter 的预期合同。
作用是固定 disabled、缺失工具、search-only、search+fetch 和工具异常降级语义，供后续实现对齐。
关键执行流程：构造 fake 工具 registry 和 fake 工具，调用 factory 或 adapter，断言 provider、候选和来源记录。
关键函数：test_factory_disabled_returns_fallback_status 固定关闭配置，test_user_tool_adapter_search_and_fetch_records_source 固定完整工具链路。
"""

from __future__ import annotations

from typing import Any

import pytest

from application.agent_workflows.strategies.deep_research import (
    ResearchSourceQuery,
)
from application.agent_workflows.strategies.deep_research.source_provider import (
    ResearchSourceManager,
)
from hosts.web.research_source_provider import (
    UserToolResearchSourceProviderAdapter,
    WebResearchSourceProviderFactory,
    WebResearchSourceProviderFactoryConfig,
)
from infrastructure.config.models import (
    Config,
    ModelConfig,
    WebConfig,
    WebDeepResearchSourceProviderConfig,
)


class _FakeUserTool:
    """测试用用户工具，按 fixture 返回结果或抛出异常。"""

    def __init__(self, result: Any = None, *, error: Exception | None = None) -> None:
        """初始化 fake 工具，输入为返回值或异常，输出为可执行工具。"""
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def execute(self, payload: dict[str, Any], context: Any | None = None) -> Any:
        """执行 fake 工具，输入为 payload，输出为 fixture 结果或异常。"""
        del context
        self.calls.append(payload)
        if self.error is not None:
            raise self.error
        return self.result


class _FakeToolRegistry:
    """测试用工具 registry，按 tool_id 返回 fake 工具。"""

    def __init__(self, tools: dict[str, _FakeUserTool]) -> None:
        """初始化 registry，输入为工具映射，输出为可查询 registry。"""
        self._tools = tools

    def get(self, tool_id: str) -> _FakeUserTool | None:
        """读取工具，输入为 tool_id，输出为工具或 None。"""
        return self._tools.get(tool_id)


class _FakeAuditWriter:
    """测试用 audit writer，记录写入事件。"""

    def __init__(self) -> None:
        """初始化 writer，输入为空，输出为可收集事件的实例。"""
        self.events: list[dict[str, Any]] = []

    def write_event(self, event: dict[str, Any]) -> None:
        """记录事件，输入为 audit event，输出为内存状态更新。"""
        self.events.append(event)


def test_factory_disabled_returns_fallback_status() -> None:
    """验证关闭配置，输入为 enabled=False，输出为空 provider 和 disabled 状态。"""
    config = WebResearchSourceProviderFactoryConfig(
        enabled=False,
        provider_name="user_tool",
        search_tool_name="web_search",
    )
    factory = WebResearchSourceProviderFactory(config)

    result = factory.build(_FakeToolRegistry({}))

    assert result.provider is None
    assert result.diagnostics.enabled is False
    assert result.diagnostics.reason == "disabled_by_config"
    assert result.diagnostics.fallback_reason == "deep_research source provider disabled by config"


def test_factory_missing_tool_returns_fallback_status() -> None:
    """验证缺失工具，输入为不存在的 tool_id，输出为空 provider 和 missing_tool 状态。"""
    config = WebResearchSourceProviderFactoryConfig(
        enabled=True,
        provider_name="user_tool",
        search_tool_name="missing_search",
    )
    factory = WebResearchSourceProviderFactory(config)

    result = factory.build(_FakeToolRegistry({}))

    assert result.provider is None
    assert result.diagnostics.reason == "search_tool_missing"
    assert result.diagnostics.fallback_reason == "search tool is not registered: missing_search"
    assert result.diagnostics.search_tool_name == "missing_search"


def test_factory_reads_web_config_source_provider_fields() -> None:
    """验证 Config 字段读取，输入为 web.deep_research_source_provider，输出为匹配工具 provider。"""
    config = Config(
        model=ModelConfig(
            name="fake-model",
            base_url="http://127.0.0.1:1234/v1",
            api_key="",
        ),
        web=WebConfig(
            deep_research_source_provider=WebDeepResearchSourceProviderConfig(
                provider_name="configured_provider",
                search_tool_name="configured_search",
            )
        ),
    )
    factory = WebResearchSourceProviderFactory(config)
    result = factory.build(_FakeToolRegistry({"configured_search": _FakeUserTool({"results": []})}))

    assert result.provider is not None
    assert result.provider.name == "configured_provider"
    assert result.diagnostics.search_tool_name == "configured_search"


@pytest.mark.asyncio
async def test_factory_builds_search_only_provider_with_weak_fetched_record() -> None:
    """验证 search-only 工具，输入为搜索摘要，输出 fetched weak record。"""
    search_tool = _FakeUserTool(
        {
            "results": [
                {
                    "url": "https://example.com/search-only",
                    "title": "Search Only",
                    "snippet": "candidate from user search",
                }
            ]
        }
    )
    factory = WebResearchSourceProviderFactory(
        WebResearchSourceProviderFactoryConfig(
            enabled=True,
            provider_name="user_tool",
            search_tool_name="web_search",
        )
    )
    result = factory.build(_FakeToolRegistry({"web_search": search_tool}))

    assert result.diagnostics.reason == "ok"
    assert result.provider is not None

    candidates = await result.provider.search(_query())
    record = await result.provider.fetch(candidates[0])

    assert candidates[0].provider_name == "user_tool"
    assert candidates[0].url == "https://example.com/search-only"
    assert record.status == "fetched"
    assert record.tier == "weak"
    assert record.error_code is None
    assert record.content_text == "candidate from user search"
    assert record.url == "https://example.com/search-only"


@pytest.mark.asyncio
async def test_user_tool_adapter_accepts_minimax_organic_results() -> None:
    """验证 organic 搜索结果，输入为 MiniMax 形态，输出候选 URL。"""
    search_tool = _FakeUserTool(
        {
            "organic": [
                {
                    "link": "https://platform.minimax.io/docs/token-plan/mcp-guide",
                    "title": "MiniMax MCP Guide",
                    "snippet": "Web Search MCP documentation",
                }
            ]
        }
    )
    provider = UserToolResearchSourceProviderAdapter(
        search_tool=search_tool,
        search_tool_name="web_search",
        name="user_tool",
    )

    candidates = await provider.search(_query())

    assert len(candidates) == 1
    assert candidates[0].url == "https://platform.minimax.io/docs/token-plan/mcp-guide"
    assert candidates[0].title == "MiniMax MCP Guide"
    assert candidates[0].snippet == "Web Search MCP documentation"
    record = await provider.fetch(candidates[0])
    assert record.status == "fetched"
    assert record.tier == "weak"
    assert record.content_text == "Web Search MCP documentation"


@pytest.mark.asyncio
async def test_user_tool_adapter_search_and_fetch_records_source() -> None:
    """验证完整 adapter，输入为 search+fetch 工具，输出 fetched strong record。"""
    search_tool = _FakeUserTool(
        {
            "results": [
                {
                    "url": "https://example.com/full",
                    "title": "Full Source",
                    "snippet": "search snippet",
                }
            ]
        }
    )
    fetch_tool = _FakeUserTool(
        {
            "url": "https://example.com/full",
            "title": "Full Source",
            "content_text": "Fetched source content for extract phase.",
        }
    )
    provider = UserToolResearchSourceProviderAdapter(
        name="user_tool",
        search_tool=search_tool,
        fetch_tool=fetch_tool,
        search_tool_name="web_search",
        fetch_tool_name="web_fetch",
    )

    candidates = await provider.search(_query())
    record = await provider.fetch(candidates[0])

    assert candidates[0].provider_name == "user_tool"
    assert candidates[0].rank == 1
    assert search_tool.calls[0]["query"] == "test query"
    assert record.status == "fetched"
    assert record.tier == "strong"
    assert record.provider_name == "user_tool"
    assert record.content_text == "Fetched source content for extract phase."
    assert fetch_tool.calls[0]["url"] == "https://example.com/full"


@pytest.mark.asyncio
async def test_user_tool_adapter_tool_errors_return_failed_weak_records() -> None:
    """验证工具异常，输入为 fetch 抛错，输出 failed weak record。"""
    search_tool = _FakeUserTool(
        {"results": [{"url": "https://example.com/fail", "title": "Fail Source"}]}
    )
    fetch_tool = _FakeUserTool(error=RuntimeError("fetch exploded"))
    provider = UserToolResearchSourceProviderAdapter(
        name="user_tool",
        search_tool=search_tool,
        fetch_tool=fetch_tool,
        search_tool_name="web_search",
        fetch_tool_name="web_fetch",
    )

    candidates = await provider.search(_query())
    record = await provider.fetch(candidates[0])

    assert record.status == "failed"
    assert record.tier == "weak"
    assert record.error_code == "runtimeerror"
    assert "fetch exploded" in str(record.error_message)
    assert record.url == "https://example.com/fail"


@pytest.mark.asyncio
async def test_user_tool_adapter_search_errors_are_audited_by_source_manager() -> None:
    """验证 search 异常审计，输入为 search 抛错，输出为 search_failed audit 和 failed record。"""
    search_tool = _FakeUserTool(error=RuntimeError("search exploded"))
    provider = UserToolResearchSourceProviderAdapter(
        name="user_tool",
        search_tool=search_tool,
        search_tool_name="web_search",
    )
    audit_writer = _FakeAuditWriter()
    source_manager = ResearchSourceManager(provider, audit_writer=audit_writer)

    records = await source_manager.collect_sources(
        [_query()],
        source_budget=3,
        fetch_budget=3,
    )

    assert len(records) == 1
    assert records[0].status == "failed"
    assert records[0].tier == "weak"
    assert records[0].query_id == "q-web"
    assert records[0].provider_name == "user_tool"
    assert records[0].error_code == "runtimeerror"
    assert "search exploded" in str(records[0].error_message)
    assert audit_writer.events[0]["action"] == "deep_research.search_failed"
    assert audit_writer.events[0]["payload"]["provider_name"] == "user_tool"
    assert "search exploded" in audit_writer.events[0]["payload"]["error_digest"]
    assert audit_writer.events[1]["action"] == "deep_research.source_recorded"
    assert audit_writer.events[1]["payload"]["status"] == "failed"


def _query() -> ResearchSourceQuery:
    """构造搜索线，输入为空，输出为 ResearchSourceQuery。"""
    return ResearchSourceQuery(
        query_id="q-web",
        line="test query",
        intent="overview",
        max_results=3,
    )
