"""unit：HostDispatcher 投递门户覆盖（host-dispatch-consolidation 新增）。

覆盖 README 自动化测试责任第 (1) 项：``HostDispatcher.submit`` 的 QUEUE / IMMEDIATE
分支。HostDispatcher 是宿主层唯一投递入口，CLI 和 Web 都通过它投递 mailbox。

关键函数：
- ``_StubRuntime``：runtime 桩，支持 run（返回 Result）和 steer（IMMEDIATE 探测）。
- ``_fake_event_loop``：让 ensure_started 的 boot_root 能跑 agent_loop 的轻量 fixture。
- ``test_submit_queue_returns_merged_false``：QUEUE 路径阻塞等 future，merged=False。
- ``test_submit_immediate_merged_true``：IMMEDIATE 命中 steer，merged=True。
- ``test_submit_immediate_returns_unmerged_when_steer_misses``：IMMEDIATE 未命中只返回未合并。
- ``test_submit_queue_calls_result_handler``：QUEUE 路径统一触发 queued_result_handler。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from application.agents.manager import SubmitMode
from application.agents.registry import TaskRegistrationContext
from application.agents.subagent_tools import SpawnAgentRequest
from core.agent_spec import AgentSpec
from core.contracts import SteerRequest
from core.message import Message
from core.result import Result
from hosts.shared.host_dispatcher import HostDispatcher


class _StubRuntime:
    """runtime 桩，支持 run（返回 Result）和 steer（IMMEDIATE 探测）。

    输入为可选的 steer 命中标志；输出为可被 HostDispatcher 调用的桩。run 返回固定
    completed Result；steer 按 steer_hits 决定返回 True/False。
    """

    def __init__(self, *, steer_hits: bool = False) -> None:
        self.steer_hits = steer_hits
        self.steer_calls: list[tuple[str, SteerRequest]] = []
        self.run_calls: list[dict[str, Any]] = []
        self.continue_calls: list[dict[str, Any]] = []
        self.result_metadata: dict[str, Any] = {}

    async def run(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        event_context: Any | None = None,
        **_: Any,
    ) -> Result:
        self.run_calls.append(
            {
                "user_input": user_input,
                "session_id": session_id,
                "event_context": event_context,
                **_,
            }
        )
        return Result(
            run_id="r-1",
            session_id=session_id or "",
            status="completed",
            turn_count=1,
            final_message=Message.assistant(content="ok"),
            metadata=dict(self.result_metadata),
        )

    async def continue_from_last_user_message(
        self,
        *,
        session_id: str | None = None,
        event_context: Any | None = None,
        **kwargs: Any,
    ) -> Result:
        self.continue_calls.append(
            {
                "session_id": session_id,
                "event_context": event_context,
                **kwargs,
            }
        )
        return Result(
            run_id="r-continued",
            session_id=session_id or "",
            status="completed",
            turn_count=1,
            final_message=Message.assistant(content="continued"),
        )

    def steer(self, session_id: str, request: SteerRequest) -> bool:
        self.steer_calls.append((session_id, request))
        return self.steer_hits


@pytest.mark.asyncio
async def test_submit_queue_returns_merged_false() -> None:
    """QUEUE 模式：submit 阻塞到 run 完成，返回 merged=False。"""
    rt = _StubRuntime()
    dispatcher = HostDispatcher(runtime=rt, session_id="sid-1")  # type: ignore[arg-type]
    receipt = await dispatcher.submit("hello", mode=SubmitMode.QUEUE)
    assert receipt.merged is False
    # QUEUE 路径触发 runtime.run（经 agent_loop 消费 mailbox）。
    assert [call["user_input"] for call in rt.run_calls] == ["hello"]
    assert rt.run_calls[0]["session_id"] == "sid-1"
    assert rt.run_calls[0]["thread_id"] == "sid-1"


@pytest.mark.asyncio
async def test_scheduled_root_uses_injected_bridge_and_registration_identity() -> None:
    """cron root 复用 HostDispatcher，并把业务 ID 写入真实 TaskRegistry。"""
    rt = _StubRuntime()
    bridge_calls: list[str] = []

    async def scheduled_bridge(user_input: str, *, mail: object) -> Result:
        del mail
        bridge_calls.append(user_input)
        return Result(
            run_id="run-scheduled-1",
            session_id="sched-session-1",
            status="completed",
            turn_count=1,
            final_message=Message.assistant(content="scheduled ok"),
        )

    dispatcher = HostDispatcher(
        runtime=rt,  # type: ignore[arg-type]
        session_id="sched-session-1",
        thread_id="thread-aaaaaaaaaaaa",
        root_run_bridge=scheduled_bridge,  # type: ignore[arg-type]
        task_registration_context=TaskRegistrationContext(
            thread_id="thread-aaaaaaaaaaaa",
            source="scheduled",
            workflow_id="scheduler",
            workflow_task_id="task-scheduled-1",
            task_run_id="run-scheduled-1",
            task_name="scheduled task",
            session_id="sched-session-1",
        ),
    )

    result = await dispatcher.run_text("scheduled input")
    records = dispatcher.list_task_records(include_finished=True)

    assert result.status == "completed"
    assert bridge_calls == ["scheduled input"]
    assert rt.run_calls == []
    assert len(records) == 1
    assert records[0].thread_id == "thread-aaaaaaaaaaaa"
    assert records[0].source == "scheduled"
    assert records[0].workflow_task_id == "task-scheduled-1"
    assert records[0].task_run_id == "run-scheduled-1"
    assert records[0].session_id == "sched-session-1"
    await dispatcher.aclose(drain=True)


@pytest.mark.asyncio
async def test_submit_immediate_merged_true_when_steer_hits() -> None:
    """IMMEDIATE 模式：steer 命中活跃 run，返回 merged=True，不触发 runtime.run。"""
    rt = _StubRuntime(steer_hits=True)
    dispatcher = HostDispatcher(runtime=rt, session_id="sid-1")  # type: ignore[arg-type]
    receipt = await dispatcher.submit("merge me", mode=SubmitMode.IMMEDIATE)
    assert receipt.merged is True
    # IMMEDIATE 命中时不进 QUEUE 路径，runtime.run 不被调。
    assert rt.run_calls == []
    assert [(sid, request.text) for sid, request in rt.steer_calls] == [("sid-1", "merge me")]


@pytest.mark.asyncio
async def test_submit_immediate_returns_unmerged_when_steer_misses() -> None:
    """IMMEDIATE 模式：steer 未命中时只返回 merged=False，不自动排队。"""
    rt = _StubRuntime(steer_hits=False)
    dispatcher = HostDispatcher(runtime=rt, session_id="sid-1")  # type: ignore[arg-type]
    receipt = await dispatcher.submit("fallback", mode=SubmitMode.IMMEDIATE)
    assert receipt.merged is False
    assert rt.run_calls == []
    assert [(sid, request.text) for sid, request in rt.steer_calls] == [("sid-1", "fallback")]


@pytest.mark.asyncio
async def test_submit_queue_preserves_runtime_metadata() -> None:
    """QUEUE 模式：submit metadata 透传到 runtime run/continue 选择。"""
    rt = _StubRuntime()
    dispatcher = HostDispatcher(runtime=rt, session_id="sid-1")  # type: ignore[arg-type]

    receipt = await dispatcher.submit(
        "already persisted",
        mode=SubmitMode.QUEUE,
        attachments=[{"asset_id": "asset-1"}],
        references=[{"kind": "thread", "id": "t-1"}],
        metadata={
            "continue_from_last_user_message": True,
            "reasoning_effort": "high",
        },
    )

    assert receipt.merged is False
    assert rt.run_calls == []
    assert len(rt.continue_calls) == 1
    assert rt.continue_calls[0]["session_id"] == "sid-1"
    assert rt.continue_calls[0]["event_context"] == {
        "run_epoch": 0,
        "mail_kind": "user_message",
        "mail_task_id": "",
        "conversation_id": "sid-1",
    }
    assert rt.continue_calls[0]["thread_id"] == "sid-1"
    assert rt.continue_calls[0]["agent_id"]
    assert rt.continue_calls[0]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_submit_queue_calls_result_handler() -> None:
    """QUEUE 模式：submit 等 run 完成后调用 queued_result_handler。"""
    rt = _StubRuntime()
    rendered: list[Result] = []

    async def _render(result: Result) -> None:
        rendered.append(result)

    dispatcher = HostDispatcher(
        runtime=rt,  # type: ignore[arg-type]
        session_id="sid-1",
        queued_result_handler=_render,
    )
    receipt = await dispatcher.submit("hello", mode=SubmitMode.QUEUE)
    assert receipt.merged is False
    assert [call["user_input"] for call in rt.run_calls] == ["hello"]
    assert len(rendered) == 1
    assert rendered[0].final_message is not None
    assert rendered[0].final_message.content == "ok"


@pytest.mark.asyncio
async def test_run_text_can_preserve_undelivered_metadata_for_host_queue() -> None:
    """Web pending queue 可关闭 HostDispatcher 内部回投，拿到原始 Result.metadata。"""
    rt = _StubRuntime()
    rt.result_metadata = {"steer_undelivered": ["late message"]}
    dispatcher = HostDispatcher(runtime=rt, session_id="sid-1")  # type: ignore[arg-type]

    result = await dispatcher.run_text("hello", repost_undelivered=False)

    assert result.metadata == {"steer_undelivered": ["late message"]}


@pytest.mark.asyncio
async def test_child_runtime_kwargs_never_include_approval_override() -> None:
    """child bridge 保留执行 session，并把审批归属固定到 root thread。"""
    rt = _StubRuntime()
    dispatcher = HostDispatcher(runtime=rt, session_id="tree")  # type: ignore[arg-type]
    await dispatcher.ensure_started()
    manager = dispatcher.agent_manager
    assert manager is not None
    root = manager.get_agent(manager._root_agent_id or "")
    assert root is not None

    manager.spawn(
        SpawnAgentRequest(
            parent_agent_id=root.agent_id,
            spec=AgentSpec(name="child", instructions="", default_model="m"),
            seed_message=Message.user("child"),
            cwd=".",
            requested_tool_names=(),
        )
    )
    for _ in range(20):
        if rt.run_calls:
            break
        await asyncio.sleep(0.01)

    child_calls = [
        call
        for call in rt.run_calls
        if call["event_context"]["mail_kind"] == "user_message"
        and call.get("agent_spec") is not None
    ]
    assert len(child_calls) == 1
    child_call = child_calls[0]
    assert "approval" not in child_call
    assert child_call["session_id"] != "tree"
    assert child_call["thread_id"] == "tree"
    assert child_call["event_context"]["conversation_id"] == "tree"
    assert child_call["agent_spec"].name == "child"
    assert child_call["enabled_tools"] == ()
    await dispatcher.aclose(drain=False)
