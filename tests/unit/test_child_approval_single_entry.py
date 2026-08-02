"""AgentManager child 运行路径的审批单入口集成测试。

本脚本验证 AgentManager/HostDispatcher 把工具请求交给 SessionEngine 持有的最终
审批对象。作用是固定全局拒绝、批准执行以及
``approval.request``/``approval.decision`` 事件坐标，防止 scoped provider 旁路复活。
关键执行流程：LLM 首轮发起工具调用，Runner 进入审批，第二轮读取 tool_result 后结束；
测试从 Agent 树启动 child。
"""

from __future__ import annotations

import asyncio
from typing import Any

from application.agents.subagent_tools import SpawnAgentRequest
from core.agent_spec import AgentSpec
from core.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    Event,
    LLMRequest,
    LLMResponse,
    PreparedToolCall,
    ToolContext,
    ToolResult,
)
from core.message import Message, ToolCall
from core.runner import Runner
from core.session import InMemorySession
from hosts.shared.host_dispatcher import HostDispatcher
from infrastructure.config.models import Config, ModelSelectionConfig
from runtime_assembly.session_engine import SessionEngine


class _EventSink:
    """事件收集器，输入为 Runner Event，输出为内存列表。"""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        """记录事件，输入为 Event，输出为空。"""
        self.events.append(event)


class _RecordingApproval:
    """最终审批对象替身，记录请求并返回配置结果。"""

    def __init__(self, outcome: str, reason: str) -> None:
        self._outcome = outcome
        self._reason = reason
        self.requests: list[ApprovalRequest] = []

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """记录请求，输入为审批请求，输出配置的最终决定。"""
        self.requests.append(request)
        return ApprovalDecision(
            outcome=self._outcome,  # type: ignore[arg-type]
            reason=self._reason,
        )


class _CountingTool:
    """计数工具，输入为任意参数，输出固定成功结果。"""

    name = "child_tool"
    description = "child tool"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.execute_calls = 0

    async def execute(self, prepared: PreparedToolCall, ctx: ToolContext) -> ToolResult:
        args = prepared.arguments
        """累计执行次数，输入为参数与上下文，输出成功结果。"""
        del args, ctx
        self.execute_calls += 1
        return ToolResult(ok=True, content="executed")


class _ToolCallingLLM:
    """每个 session 首轮调用工具，看到 tool_result 后返回完成消息。"""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """按历史是否含 tool_result 选择工具调用或最终回复。"""
        if not request.tools:
            return LLMResponse(message=Message.assistant("done"), finish_reason="stop")
        if any(message.role == "tool" for message in request.messages):
            return LLMResponse(message=Message.assistant("done"), finish_reason="stop")
        return LLMResponse(
            message=Message.assistant(
                "",
                tool_calls=(
                    ToolCall(
                        call_id="child-call",
                        tool_name="child_tool",
                        arguments={},
                    ),
                ),
            ),
            finish_reason="tool_calls",
        )


def _runtime(
    *,
    approval: _RecordingApproval,
    tool: _CountingTool,
    sink: _EventSink,
) -> SessionEngine:
    """装配测试 SessionEngine，输入为最终审批、工具和事件 sink，输出 runtime。"""
    config = Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"))
    return SessionEngine(
        config=config,
        runner=Runner(event_sinks=[sink]),
        llm=_ToolCallingLLM(),
        tools={tool.name: tool},
        enabled_tool_names=[tool.name],
        approval=approval,
        session_factory=lambda sid: InMemorySession(session_id=sid),
        event_sinks=[sink],
        agent_spec=AgentSpec(
            name="root",
            instructions="",
            default_model="stub",
            tool_names=(),
            max_turns=2,
        ),
    )


async def test_new_child_path_global_rejection_emits_single_correlated_decision() -> None:
    """新 Agent 树 child 命中全局拒绝，工具零执行且审批事件坐标完整。"""
    sink = _EventSink()
    approval = _RecordingApproval("rejected", "global-deny")
    tool = _CountingTool()
    runtime = _runtime(approval=approval, tool=tool, sink=sink)
    dispatcher = HostDispatcher(runtime=runtime, session_id="tree")
    await dispatcher.ensure_started()
    manager = dispatcher.agent_manager
    assert manager is not None
    root = manager.get_agent(manager._root_agent_id or "")
    assert root is not None
    spawned = manager.spawn(
        SpawnAgentRequest(
            parent_agent_id=root.agent_id,
            spec=AgentSpec(
                name="child",
                instructions="",
                default_model="stub",
                tool_names=(tool.name,),
                max_turns=2,
            ),
            seed_message=Message.user("run"),
            cwd=".",
            requested_tool_names=(tool.name,),
            enabled_tools=(tool,),
        )
    )
    for _ in range(50):
        if any(
            event.kind == "approval.decision" and event.agent_id == spawned.child_id
            for event in sink.events
        ):
            break
        await asyncio.sleep(0.01)

    request_events = [
        event
        for event in sink.events
        if event.kind == "approval.request" and event.agent_id == spawned.child_id
    ]
    decision_events = [
        event
        for event in sink.events
        if event.kind == "approval.decision" and event.agent_id == spawned.child_id
    ]
    child_requests = [
        request for request in approval.requests if request.run_id == request_events[0].run_id
    ]
    assert len(child_requests) == 1
    assert child_requests[0].session_id != "tree"
    assert child_requests[0].metadata["thread_id"] == "tree"
    assert tool.execute_calls == 0
    assert len(request_events) == 1
    assert len(decision_events) == 1
    assert request_events[0].run_id == decision_events[0].run_id
    assert request_events[0].turn == decision_events[0].turn == 1
    assert request_events[0].payload["call_id"] == "child-call"
    assert decision_events[0].payload["outcome"] == "rejected"
    await dispatcher.aclose(drain=False)
