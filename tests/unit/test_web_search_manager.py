"""WebSearchManager 单元测试。

本脚本验证 fake MCP data 到 WebSearchResponse 的归一化，以及 `web_search`
Tool 能注册进 ToolRegistry 并执行。
关键执行流程：用 fake search Tool 返回 MCP 风格 data，调用 manager/search tool，
断言 URL/title/snippet/provider/diagnostics。
"""

from __future__ import annotations

from typing import Any

import pytest

from application.web_search import WebSearchManager, build_web_search_tool
from core.contracts import ToolContext, ToolResult
from tools import ToolRegistry


class _FakeSearchTool:
    """fake 底层搜索 Tool，返回 MCP 风格 results data。"""

    name = "mcp__minimax__web_search"
    description = "fake minimax web search"
    input_schema: dict[str, Any] = {"type": "object"}

    def __init__(self) -> None:
        """初始化 fake tool，输入为空，输出带 calls 列表的实例。"""
        self.calls: list[dict[str, Any]] = []

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """记录调用并返回 fake 搜索结果。"""
        self.calls.append(dict(args))
        return ToolResult(
            ok=True,
            content="ok",
            data={
                "provider_name": "minimax_web_search",
                "provider_tool_name": self.name,
                "results": [
                    {
                        "url": "https://example.com/a",
                        "title": "A title",
                        "snippet": "A snippet",
                        "published_at": "2026-06-13",
                        "metadata": {"score": 0.9},
                    },
                    {
                        "link": "https://example.com/b",
                        "name": "B title",
                        "summary": "B snippet",
                    },
                ],
                "mcp_diagnostics": {"elapsed_ms": 7},
            },
        )


class _FakeOrganicSearchTool:
    """fake MiniMax MCP 工具，返回 organic 风格搜索结果。"""

    name = "mcp__minimax__web_search"
    description = "fake minimax organic web search"
    input_schema: dict[str, Any] = {"type": "object"}

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """返回 organic JSON content，输入为搜索参数，输出 ToolResult。"""
        del args, ctx
        return ToolResult(
            ok=True,
            content=(
                '{"organic":[{"link":"https://platform.minimax.io/docs/token-plan/mcp-guide",'
                '"title":"MiniMax MCP Guide","snippet":"Web Search MCP documentation"}]}'
            ),
            data={"mcp_diagnostics": {"elapsed_ms": 9}},
        )


class _FakeProviderErrorSearchTool:
    """fake MiniMax MCP 工具，返回文本形态 provider 错误。"""

    name = "mcp__minimax__web_search"
    description = "fake minimax provider error"
    input_schema: dict[str, Any] = {"type": "object"}

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """返回 API 错误文本，输入搜索参数，输出 ToolResult。"""
        del args, ctx
        return ToolResult(
            ok=True,
            content="Failed to perform search: API Error: 2049-invalid api key Trace-Id: abc",
            data={
                "raw_result": {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Failed to perform search: API Error: "
                                "2049-invalid api key Trace-Id: abc"
                            ),
                        }
                    ],
                    "isError": False,
                },
                "mcp_diagnostics": {"elapsed_ms": 9},
            },
        )


class _FakeConflictingDiagnosticsSearchTool:
    """fake 搜索工具，返回冲突 diagnostics 用于验证失败优先合并。"""

    name = "mcp__minimax__web_search"
    description = "fake conflicting diagnostics"
    input_schema: dict[str, Any] = {"type": "object"}

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """返回冲突诊断，输入搜索参数，输出 ToolResult。"""
        del args, ctx
        return ToolResult(
            ok=True,
            content="ok",
            data={
                "results": [{"url": "https://example.com/diagnostics"}],
                "diagnostics": {"ok": False, "error_message": "provider marked failure"},
                "mcp_diagnostics": {"ok": True, "elapsed_ms": 12},
            },
        )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_web_search_manager_normalizes_fake_mcp_data() -> None:
    """验证 manager 归一化 URL/title/snippet/provider/diagnostics。"""
    fake_tool = _FakeSearchTool()
    manager = WebSearchManager(
        fake_tool,
        provider_name="minimax_web_search",
        provider_tool_name=fake_tool.name,
    )

    response = await manager.search("latest ai news", max_results=1)

    assert fake_tool.calls == [{"query": "latest ai news", "max_results": 1}]
    assert response.query == "latest ai news"
    assert len(response.results) == 1
    result = response.results[0]
    assert result.url == "https://example.com/a"
    assert result.title == "A title"
    assert result.snippet == "A snippet"
    assert result.provider_name == "minimax_web_search"
    assert result.provider_tool_name == "mcp__minimax__web_search"
    assert result.published_at == "2026-06-13"
    assert result.metadata["score"] == 0.9
    assert response.diagnostics["provider_name"] == "minimax_web_search"
    assert response.diagnostics["provider_tool_name"] == "mcp__minimax__web_search"
    assert response.diagnostics["elapsed_ms"] == 7


@pytest.mark.asyncio
@pytest.mark.unit
async def test_web_search_manager_normalizes_minimax_organic_content() -> None:
    """验证 MiniMax organic content，输入为 MCP JSON 文本，输出标准 results。"""
    manager = WebSearchManager(
        _FakeOrganicSearchTool(),
        provider_name="minimax_web_search",
        provider_tool_name="mcp__minimax__web_search",
    )

    response = await manager.search("minimax mcp", max_results=2)

    assert len(response.results) == 1
    assert response.results[0].url == "https://platform.minimax.io/docs/token-plan/mcp-guide"
    assert response.results[0].title == "MiniMax MCP Guide"
    assert response.results[0].snippet == "Web Search MCP documentation"
    assert response.diagnostics["result_count"] == 1
    assert response.diagnostics["elapsed_ms"] == 9


@pytest.mark.asyncio
@pytest.mark.unit
async def test_web_search_tool_marks_provider_text_error_as_failed() -> None:
    """验证 provider 文本错误，输入 MiniMax 失败文本，输出 ToolResult(ok=False)。"""
    manager = WebSearchManager(
        _FakeProviderErrorSearchTool(),
        provider_name="minimax_web_search",
        provider_tool_name="mcp__minimax__web_search",
    )
    tool = build_web_search_tool(manager)

    result = await tool.execute(
        {"query": "minimax mcp", "max_results": 2},
        ToolContext(run_id="r", session_id="s", turn=1, call_id="c"),
    )

    assert result.ok is False
    assert result.error_message is not None
    assert "2049-invalid api key" in result.error_message
    assert result.data is not None
    assert result.data["diagnostics"]["ok"] is False
    assert result.data["diagnostics"]["result_count"] == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_web_search_manager_keeps_pessimistic_diagnostics() -> None:
    """验证 diagnostics 合并，输入冲突 ok 字段，输出任一失败即失败。"""
    manager = WebSearchManager(
        _FakeConflictingDiagnosticsSearchTool(),
        provider_name="minimax_web_search",
        provider_tool_name="mcp__minimax__web_search",
    )
    tool = build_web_search_tool(manager)

    result = await tool.execute(
        {"query": "diagnostics merge"},
        ToolContext(run_id="r", session_id="s", turn=1, call_id="c"),
    )

    assert result.ok is False
    assert result.error_message == "provider marked failure"
    assert result.data is not None
    assert result.data["diagnostics"]["ok"] is False
    assert result.data["diagnostics"]["elapsed_ms"] == 12


@pytest.mark.asyncio
@pytest.mark.unit
async def test_build_web_search_tool_registers_and_executes_with_tool_registry() -> None:
    """验证 build_web_search_tool 可注册进 ToolRegistry 并调用。"""
    fake_tool = _FakeSearchTool()
    manager = WebSearchManager(fake_tool, provider_name="minimax_web_search")
    registry = ToolRegistry()
    registry.register(build_web_search_tool(manager))

    tool = registry["web_search"]
    result = await tool.execute(
        {"query": "kongming", "max_results": 2},
        ToolContext(run_id="r", session_id="s", turn=1, call_id="c"),
    )

    assert result.ok is True
    assert result.data is not None
    assert [item["url"] for item in result.data["results"]] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert result.data["diagnostics"]["result_count"] == 2
    assert "A title" in result.content


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("max_results", ["abc", 0, -1, True, 1.9, float("inf")])
async def test_web_search_tool_rejects_invalid_max_results(max_results: object) -> None:
    """验证 max_results 非法时返回 ToolResult 错误而非抛异常。"""
    fake_tool = _FakeSearchTool()
    manager = WebSearchManager(fake_tool, provider_name="minimax_web_search")
    tool = build_web_search_tool(manager)

    result = await tool.execute(
        {"query": "kongming", "max_results": max_results},
        ToolContext(run_id="r", session_id="s", turn=1, call_id="c"),
    )

    assert result.ok is False
    assert result.error_message == "max_results must be an integer >= 1"
    assert fake_tool.calls == []


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("max_results", ["2", "abc", 0, -1, True, 1.9, float("inf")])
async def test_web_search_manager_rejects_invalid_max_results(max_results: object) -> None:
    """验证 manager 入口也拒绝非正整数 max_results。"""
    fake_tool = _FakeSearchTool()
    manager = WebSearchManager(fake_tool, provider_name="minimax_web_search")

    with pytest.raises(ValueError, match="max_results must be an integer >= 1"):
        await manager.search("kongming", max_results=max_results)  # type: ignore[arg-type]

    assert fake_tool.calls == []
