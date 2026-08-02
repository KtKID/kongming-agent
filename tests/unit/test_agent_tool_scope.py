"""子 Agent 工具裁剪合同测试。

本脚本验证父工具、任务请求和 scope 允许集合的三方交集语义。作用是固定父顺序、
去重、缺省继承、显式空集合和 execution wrapper 保留规则，避免子 Agent 从任一
派生入口扩大工具能力。
关键执行流程：构造带重复项的父工具快照，调用统一裁剪入口，再断言有效工具对象
与名称顺序。
"""

from __future__ import annotations

from typing import Any

from application.tool_scope import clip_child_tool_snapshot
from core.agent_spec import AgentSpec
from core.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    LLMRequest,
    LLMResponse,
    PreparedToolCall,
    ToolContext,
    ToolResult,
)
from core.message import Message, ToolCall
from core.runner import Runner
from core.session import InMemorySession


class _Tool:
    """最小工具替身，输入为名称，输出用于身份断言的 Tool 对象。"""

    description = "test"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self, name: str) -> None:
        self.name = name
        self.execute_calls = 0

    async def execute(self, prepared: PreparedToolCall, ctx: ToolContext) -> ToolResult:
        args = prepared.arguments
        """返回成功结果，输入为任意参数，输出固定 ToolResult。"""
        del args, ctx
        self.execute_calls += 1
        return ToolResult(ok=True, content=self.name)


class _AllowApproval:
    """审批替身，输入为请求，输出固定同意。"""

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """返回 approved，输入为任意请求，输出固定决定。"""
        del request
        return ApprovalDecision(outcome="approved", reason="test")


class _ForgedCallLLM:
    """先伪造未授权 tool call，再返回终态；同时记录每轮 LLMRequest。"""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """记录请求，首轮输出伪造调用，次轮输出完成消息。"""
        self.requests.append(request)
        if len(self.requests) == 1:
            return LLMResponse(
                message=Message.assistant(
                    "",
                    tool_calls=(
                        ToolCall(
                            call_id="forged-1",
                            tool_name="write_file",
                            arguments={"path": "outside.txt", "content": "bad"},
                        ),
                    ),
                ),
                finish_reason="tool_calls",
            )
        return LLMResponse(message=Message.assistant("done"), finish_reason="stop")


def test_clip_child_tools_uses_three_way_intersection_in_parent_order() -> None:
    """三方交集按父顺序输出，并过滤父级缺失和 scope 外工具。"""
    read = _Tool("read_file")
    write = _Tool("write_file")
    shell = _Tool("shell")

    effective = clip_child_tool_snapshot(
        parent_tools=(read, write, shell),
        requested_tool_names=("shell", "missing", "read_file"),
        scope_allowed_tool_names={"read_file", "write_file"},
    )

    assert effective == (read,)


def test_clip_child_tools_missing_request_inherits_parent_and_deduplicates() -> None:
    """requested 缺省时继承父集合，重复父工具只保留第一次出现。"""
    read = _Tool("read_file")
    read_duplicate = _Tool("read_file")
    write = _Tool("write_file")

    effective = clip_child_tool_snapshot(
        parent_tools=(read, read_duplicate, write),
        requested_tool_names=None,
        scope_allowed_tool_names={"read_file", "write_file"},
    )

    assert effective == (read, write)


def test_clip_child_tools_explicit_empty_remains_empty() -> None:
    """requested 显式空集合保持零工具语义。"""
    effective = clip_child_tool_snapshot(
        parent_tools=(_Tool("read_file"),),
        requested_tool_names=(),
    )

    assert effective == ()


def test_clip_child_tools_drops_lifecycle_bound_evolution_request() -> None:
    """workflow child 未安装 evolution lifecycle 时不继承公开审查 Tool。"""
    review = _Tool("request_evolution_review")
    read = _Tool("read_file")

    effective = clip_child_tool_snapshot(
        parent_tools=(review, read),
        requested_tool_names=("request_evolution_review", "read_file"),
    )

    assert effective == (read,)


def test_clip_child_tools_keeps_scoped_wrapper_for_allowed_parent_name() -> None:
    """同名 wrapper 只在父级已持有该工具时替换 execution 对象。"""
    parent_read = _Tool("read_file")
    wrapped_read = _Tool("read_file")
    forged_shell = _Tool("shell")

    effective = clip_child_tool_snapshot(
        parent_tools=(parent_read,),
        requested_tool_names=("read_file", "shell"),
        requested_tools=(wrapped_read, forged_shell),
    )

    assert effective == (wrapped_read,)


async def test_effective_snapshot_drives_llm_schema_and_rejects_forged_call() -> None:
    """LLM 与 Runner 共用空快照，伪造工具调用返回 tool_unavailable 且不执行。"""
    forbidden = _Tool("write_file")
    effective = clip_child_tool_snapshot(
        parent_tools=(forbidden,),
        requested_tool_names=(),
    )
    llm = _ForgedCallLLM()
    session = InMemorySession(session_id="child-zero-tools")

    result = await Runner().run(
        "try forged call",
        session=session,
        agent_spec=AgentSpec(
            name="child",
            instructions="",
            default_model="stub",
            tool_names=(),
            max_turns=2,
        ),
        llm=llm,
        tools={"write_file": forbidden},
        approval=_AllowApproval(),
        enabled_tools=effective,
    )

    assert result.status == "completed"
    assert tuple(tool.name for tool in llm.requests[0].tools) == ()
    history = await session.history()
    forged_result = next(message for message in history if message.role == "tool")
    assert forged_result.metadata["reason"] == "tool_unavailable"
    assert forbidden.execute_calls == 0
