"""MCP tool adapter 单元测试。

本脚本验证 canonical name、alias 注册计划、冲突诊断和 tools/call 转接。
关键执行流程：构造 fake descriptor 与 fake client，生成注册计划并执行 adapter。
关键函数：_ctx 构造 ToolContext，_FakeMcpClient 记录 call_tool 调用。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.contracts import ToolContext
from tools.mcp import McpToolAdapterManager, McpToolAliasConfig, McpToolDescriptor


class _FakeMcpCallResult:
    """fake MCP 调用结果，模拟 infrastructure.mcp.McpCallResult。"""

    def __init__(self, data: dict[str, Any]) -> None:
        """初始化 fake result，输入 data，输出可被 adapter 读取的对象。"""
        self.ok = True
        self.content_text = "fake search result"
        self.data = data
        self.error_message = None
        self.diagnostics = {"elapsed_ms": 12, "server_status": "ready"}


class _FakeMcpClient:
    """fake MCP client，记录 call_tool 入参并返回固定结果。"""

    def __init__(self) -> None:
        """初始化 fake client，输入为空，输出带 calls 列表的实例。"""
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def call_tool(
        self,
        server_id: str,
        tool_name: str,
        args: dict[str, Any],
    ) -> _FakeMcpCallResult:
        """记录 MCP 调用，输入 server/tool/args，输出 fake 结果。"""
        self.calls.append((server_id, tool_name, args))
        return _FakeMcpCallResult({"results": [{"url": "https://example.com"}]})


def _descriptor() -> McpToolDescriptor:
    """构造 MCP descriptor，输入为空，输出 web_search descriptor。"""
    return McpToolDescriptor(
        server_id="minimax",
        name="web-search",
        title="MiniMax Web Search",
        description="Search the web through MiniMax MCP.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        raw_descriptor={"name": "web-search"},
    )


def _ctx() -> ToolContext:
    """构造 ToolContext，输入为空，输出测试上下文。"""
    return ToolContext(run_id="r", session_id="s", turn=1, call_id="c")


@pytest.mark.unit
def test_adapter_plan_generates_canonical_and_alias_registration() -> None:
    """验证 canonical 和 web_search alias 注册计划。"""
    manager = McpToolAdapterManager(
        _FakeMcpClient(),
        alias_configs=[McpToolAliasConfig(tool_name="web-search", alias="web_search")],
    )

    plan = manager.build_registration_plan([_descriptor()])

    assert [item.kongming_tool_name for item in plan.registrations] == [
        "mcp__minimax__web_search",
        "web_search",
    ]
    assert plan.registrations[0].canonical_name == "mcp__minimax__web_search"
    assert plan.registrations[1].is_alias is True
    assert plan.registrations[1].input_schema["properties"]["query"]["type"] == "string"
    assert plan.skipped_aliases == ()


@pytest.mark.unit
def test_adapter_plan_skips_alias_conflict_and_keeps_canonical() -> None:
    """验证 alias 冲突跳过 alias，同时保留 canonical。"""
    manager = McpToolAdapterManager(
        _FakeMcpClient(),
        alias_configs=[{"tool_name": "web-search", "alias": "web_search", "enabled": True}],
        existing_tool_names=["web_search"],
    )

    plan = manager.build_registration_plan([_descriptor()])

    assert [item.kongming_tool_name for item in plan.registrations] == ["mcp__minimax__web_search"]
    assert plan.skipped_aliases == ("web_search",)
    assert plan.diagnostics["skipped_aliases"][0]["reason"] == "alias_conflict"
    assert plan.diagnostics["skipped_aliases"][0]["canonical_name"] == "mcp__minimax__web_search"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_adapter_execute_calls_fake_client_and_returns_tool_result_data() -> None:
    """验证 adapter execute 调用 fake client 并保留 data/diagnostics。"""
    client = _FakeMcpClient()
    manager = McpToolAdapterManager(client)
    plan = manager.build_registration_plan([_descriptor()])
    tool = manager.build_tool(plan.registrations[0])

    result = await tool.execute({"query": "kongming"}, _ctx())

    assert client.calls == [("minimax", "web-search", {"query": "kongming"})]
    assert result.ok is True
    assert result.data is not None
    assert result.data["results"] == [{"url": "https://example.com"}]
    assert result.data["mcp_diagnostics"]["elapsed_ms"] == 12
    assert result.data["mcp_tool"]["canonical_name"] == "mcp__minimax__web_search"
