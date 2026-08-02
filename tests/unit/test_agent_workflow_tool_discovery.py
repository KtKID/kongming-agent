"""agent workflow 工具发现合同测试。

本脚本验证 workflow prompt listing 指向的 describe/run 工具真实可注册和可调用。
作用是锁定 LLM 从 workflow catalog 进入 payload schema 查询，再进入 run 工具执行的路径。
关键执行流程：构造 fake manager -> 注册 workflow tools -> 调用 describe 工具 -> 断言数据字段。
关键函数：test_register_agent_workflow_tool_exposes_describe_and_run 验证注册合同。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from application.agent_workflows.strategies.description import (
    WorkflowStrategyDescription,
    WorkflowStrategyInputField,
)
from core.contracts import ToolContext
from tests.support.tool_calls import execute_prepared_tool
from tools import AgentWorkflowHandle, ToolRegistry, register_agent_workflow_tool
from tools.agent_workflow_tool import build_run_agent_workflow_tool


class _FakeWorkflowManager:
    """测试用 workflow manager，输入为 mode，输出固定 description。"""

    def describe_workflow_strategy(self, mode: str) -> WorkflowStrategyDescription:
        """返回策略详情，输入为 mode，输出 WorkflowStrategyDescription。"""
        return WorkflowStrategyDescription(
            mode=mode,
            title="测试 workflow",
            status="available",
            runnable=True,
            summary="测试策略摘要",
            when_to_use=("需要测试工具发现链路",),
            warnings=("测试 warning",),
            inputs=(
                WorkflowStrategyInputField(
                    name="objective",
                    required=True,
                    type_label="string",
                    description="测试目标",
                    example="完成测试",
                ),
            ),
            outputs=("测试输出",),
            examples=({"objective": "完成测试"},),
        )


class _BadExampleWorkflowManager:
    """测试用坏 description manager，输入为 mode，输出不可 JSON 化 example。"""

    def __init__(self, *, bad_input_example: bool = True) -> None:
        """初始化坏例子模式，输入为布尔开关，输出 manager 实例。"""
        self._bad_input_example = bad_input_example

    def describe_workflow_strategy(self, mode: str) -> WorkflowStrategyDescription:
        """返回含坏 example 的策略详情，输入为 mode，输出 WorkflowStrategyDescription。"""
        input_example = Path("bad-example") if self._bad_input_example else "完成测试"
        examples = () if self._bad_input_example else ({"objective": Path("bad-payload")},)
        return WorkflowStrategyDescription(
            mode=mode,
            title="坏 example workflow",
            status="available",
            runnable=True,
            summary="测试坏 example",
            when_to_use=("需要测试错误定位",),
            warnings=(),
            inputs=(
                WorkflowStrategyInputField(
                    name="objective",
                    required=True,
                    type_label="string",
                    description="测试目标",
                    example=input_example,
                ),
            ),
            outputs=(),
            examples=examples,
        )


def _ctx() -> ToolContext:
    """构造工具上下文，输入为空，输出 ToolContext。"""
    return ToolContext(run_id="run-1", session_id="sid-1", turn=1, call_id="call-1")


@pytest.mark.asyncio
async def test_register_agent_workflow_tool_exposes_describe_and_run() -> None:
    """验证工具注册合同，输入为 fake manager，输出 describe/run 均可查。"""
    handle = AgentWorkflowHandle()
    handle.bind(_FakeWorkflowManager())
    registry = ToolRegistry()

    register_agent_workflow_tool(registry, handle)

    assert "describe_agent_workflow_strategy" in registry.names()
    assert "run_agent_workflow" in registry.names()
    describe_tool = registry["describe_agent_workflow_strategy"]
    result = await execute_prepared_tool(describe_tool, {"mode": "task_flow"}, _ctx())

    assert result.ok is True
    assert result.data is not None
    assert result.data["mode"] == "task_flow"
    assert result.data["inputs"][0]["name"] == "objective"
    assert result.data["payload_schema"]["required"] == ["objective"]
    assert result.data["payload_schema"]["properties"]["objective"]["type"] == "string"
    assert result.data["payload_schema"]["properties"]["objective"]["examples"] == ["完成测试"]
    assert result.data["warnings"] == ["测试 warning"]
    assert result.data["examples"] == [{"objective": "完成测试"}]
    assert "workflow_strategy: task_flow" in result.content
    assert "inputs:" in result.content
    assert "payload_schema:" in result.content


@pytest.mark.asyncio
async def test_describe_agent_workflow_strategy_reports_non_json_example_context() -> None:
    """验证坏 example 错误定位，输入为 Path example，输出含 mode/input/type 的错误。"""
    handle = AgentWorkflowHandle()
    handle.bind(_BadExampleWorkflowManager())
    registry = ToolRegistry()

    register_agent_workflow_tool(registry, handle)
    result = await execute_prepared_tool(
        registry["describe_agent_workflow_strategy"],
        {"mode": "task_flow"},
        _ctx(),
    )

    assert result.ok is False
    assert result.error_message is not None
    assert "workflow strategy task_flow input objective example" in result.error_message
    assert "Path" in result.error_message


@pytest.mark.asyncio
async def test_describe_agent_workflow_strategy_reports_non_json_payload_example_context() -> None:
    """验证坏 payload example 错误定位，输入为 Path payload，输出含 mode/index/type。"""
    handle = AgentWorkflowHandle()
    handle.bind(_BadExampleWorkflowManager(bad_input_example=False))
    registry = ToolRegistry()

    register_agent_workflow_tool(registry, handle)
    result = await execute_prepared_tool(
        registry["describe_agent_workflow_strategy"],
        {"mode": "task_flow"},
        _ctx(),
    )

    assert result.ok is False
    assert result.error_message is not None
    assert "workflow strategy task_flow examples[0]" in result.error_message
    assert "dict" in result.error_message


def test_run_agent_workflow_mode_schema_accepts_registered_strategy_string() -> None:
    """验证 run 工具 mode schema 开放，输入为空 handle，输出无硬编码 enum。"""
    tool = build_run_agent_workflow_tool(AgentWorkflowHandle())

    mode_schema = tool.input_schema["properties"]["mode"]

    assert mode_schema["type"] == "string"
    assert "enum" not in mode_schema
