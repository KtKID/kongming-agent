"""Runner turn 事件上下文单元测试。"""

from __future__ import annotations

from typing import Any

from core import AgentSpec, InMemorySession, Runner
from core.contracts import ApprovalDecision, ApprovalRequest, Event, LLMRequest, LLMResponse
from core.mail import Mail
from core.message import Message
from hosts.web.threads.manager import _build_mail_event_context


class _StubLLM:
    """返回一条终态 assistant 消息的最小 LLM stub。"""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        del request
        return LLMResponse(
            message=Message.assistant("ok"),
            finish_reason="stop",
        )


class _AllowApproval:
    """允许所有工具请求的最小审批 stub。"""

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        del request
        return ApprovalDecision(outcome="approved")


class _RecordingSink:
    """记录 Runner emit 的 Event。"""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


async def test_turn_events_include_mailbox_context() -> None:
    """turn.start/end 写入 session、agent、epoch、mail 和 conversation 上下文。"""
    sink = _RecordingSink()
    runner = Runner(event_sinks=[sink])
    session = InMemorySession("session-child-1")
    spec = AgentSpec(
        name="tester",
        instructions="",
        default_model="stub",
        max_turns=2,
    )

    result = await runner.run(
        "hello",
        session=session,
        agent_spec=spec,
        llm=_StubLLM(),
        tools={},
        approval=_AllowApproval(),
        event_context={
            "run_epoch": 7,
            "mail_kind": "child_result",
            "mail_task_id": "task-123",
            "conversation_id": "thread-main",
        },
        agent_id="agent-child",
    )

    assert result.status == "completed"
    turn_start = next(event for event in sink.events if event.kind == "turn.start")
    turn_end = next(event for event in sink.events if event.kind == "turn.end")

    expected_context: dict[str, Any] = {
        "session_id": "session-child-1",
        "agent_id": "agent-child",
        "run_epoch": 7,
        "mail_kind": "child_result",
        "mail_task_id": "task-123",
        "conversation_id": "thread-main",
    }
    assert turn_start.payload == {**expected_context, "phase": "start"}
    assert turn_start.agent_id == "agent-child"
    assert turn_start.task_id == "task-123"
    assert turn_start.conversation_id == "thread-main"

    assert turn_end.payload == {
        **expected_context,
        "phase": "end",
        "has_tool_calls": False,
        "tool_call_count": 0,
        "finish_reason": "stop",
        "history_index": 1,
    }
    assert turn_end.agent_id == "agent-child"
    assert turn_end.task_id == "task-123"
    assert turn_end.conversation_id == "thread-main"


def test_build_mail_event_context_from_web_mail() -> None:
    """Web mailbox helper 从 Mail 提取 Runner event_context。"""
    mail = Mail(
        kind="user_message",
        sender="user",
        recipient_agent_id="root",
        task_id="task-abc",
        epoch=3,
        payload=Message.user("hi"),
    )

    assert _build_mail_event_context(mail, conversation_id="thread-1") == {
        "run_epoch": 3,
        "mail_kind": "user_message",
        "mail_task_id": "task-abc",
        "conversation_id": "thread-1",
    }
