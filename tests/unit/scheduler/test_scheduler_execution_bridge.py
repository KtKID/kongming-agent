"""unit：application.scheduled_runs.execution_bridge.ExecutionBridge + InactivityWatchdog（Wave C）。
覆盖矩阵：

§1 Runner 成功路径
  - completed → ScheduledRun.status=COMPLETED + excerpt
  - completed + final 命中 [SILENT] → status=SILENT + silent_suppressed=True + audit
  - completed + "[SILENT] no diff" → strip 后 excerpt="no diff"

§2 watchdog
  - 长任务无活动 → cancel + status=INACTIVITY_TIMEOUT
  - 长任务持续 emit 活动事件 → 不 cancel，正常完成
  - timeout=None → watchdog 不启动
  - timeout=0 → 视为禁用

§3 失败路径
  - Runner 抛异常 → status=FAILED + RUNNER_EXCEPTION + error_message 含异常类型
  - Runner 返回 status=failed + error 含 needs_approval → FAILED + NEEDS_APPROVAL

§4 工具裁剪
  - enabled 含 schedule.* / cron.* → 实际传给 runner 的 tools 仅留剩余
  - enabled 全部合规 → 全部保留

§5 Store 写回 / audit
  - 完成后 list_runs 含一条最终 status 行（旧 running 行被 superseded）
  - audit 至少含 run_started + run_finished
  - silent 路径写 run_silent_suppressed
  - inactivity timeout 路径写 run_inactivity_timeout
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from application.scheduled_runs.execution_bridge import ExecutionBridge, InactivityWatchdog
from core import AgentSpec, InMemorySession
from core.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    Event,
    EventSink,
    LLMRequest,
    LLMResponse,
    PreparedToolCall,
    Session,
    Tool,
    ToolContext,
    ToolResult,
)
from core.errors import AgentError
from core.message import Message
from core.result import Result
from core.runner import Runner
from scheduler.delivery import DeliveryResult
from scheduler.domain import (
    ConcurrencyPolicy,
    DeliveryChannel,
    DueTaskReservation,
    MisfirePolicy,
    RunFailureReason,
    RunStatus,
    ScheduleDelivery,
    ScheduledRun,
    ScheduledTask,
    ScheduleTrigger,
    SessionMode,
    TaskExecutionPolicy,
    TaskLifecycleState,
    TaskOrigin,
    TaskTarget,
    TriggerType,
)
from scheduler.store import Store
from scheduler.timing import to_iso, utc_now
from tests.support.scheduled_runtime import (
    RunnerBackedScheduledRuntime,
    execute_bridge_for_test,
)

# ---------------------------------------------------------------------------
# 通用 fakes
# ---------------------------------------------------------------------------


class _StubLLM:
    """无操作 LLM：execute 不会真去调它（Runner 被替换）。"""

    def __init__(self, content: str = "") -> None:
        self._content = content

    async def complete(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover
        del request
        return LLMResponse(message=Message(role="assistant", content=self._content))


class _AllowApproval:
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(outcome="approved")


@dataclass
class _NoopTool:
    name: str = "noop"
    description: str = "noop tool"
    input_schema: dict[str, Any] = field(default_factory=dict)

    async def execute(self, prepared: PreparedToolCall, ctx: ToolContext) -> ToolResult:
        del prepared, ctx
        return ToolResult(ok=True, content="ok")


def _make_tool(name: str) -> _NoopTool:
    return _NoopTool(name=name)


def _make_task(
    *,
    task_id: str = "task-1",
    inactivity_timeout_seconds: int | None = 600,
    max_turns: int | None = 5,
    concurrency_policy: ConcurrencyPolicy = ConcurrencyPolicy.FORBID,
    thread_id: str = "",
    delivery: ScheduleDelivery | None = None,
) -> ScheduledTask:
    now = to_iso(utc_now())
    return ScheduledTask(
        task_id=task_id,
        name="t",
        lifecycle=TaskLifecycleState.SCHEDULED,
        origin=TaskOrigin.CLI,
        trigger=ScheduleTrigger(
            trigger_type=TriggerType.INTERVAL,
            expr="5",
            timezone="UTC",
        ),
        policy=TaskExecutionPolicy(
            session_mode=SessionMode.FRESH_SESSION,
            concurrency_policy=concurrency_policy,
            misfire_policy=MisfirePolicy.SKIP,
            max_turns=max_turns,
            inactivity_timeout_seconds=inactivity_timeout_seconds,
            wall_timeout_seconds=None,
            retry_limit=0,
            silent_marker_enabled=True,
        ),
        target=TaskTarget(
            agent_name="agent-x",
            input_text="do the thing",
            metadata={},
        ),
        next_run_at=now,
        last_run_at=None,
        created_by="cli",
        created_at=now,
        updated_at=now,
        delivery=delivery,
        thread_id=thread_id,
    )


def _make_reservation(task: ScheduledTask) -> DueTaskReservation:
    now = to_iso(utc_now())
    return DueTaskReservation(
        task=task,
        scheduled_for=task.next_run_at or now,
        reserved_at=now,
    )


def _make_agent_spec() -> AgentSpec:
    return AgentSpec(
        name="agent-x",
        instructions="you are an agent",
        default_model="m",
        max_turns=10,
    )


# ---------------------------------------------------------------------------
# FakeRunner：把 Runner.run 替换为可配置行为
# ---------------------------------------------------------------------------


@dataclass
class _RunnerCallCapture:
    """记录每次 run 调用入参，供测试断言。"""

    user_input: str
    session_id: str
    run_id: str | None
    enabled_tool_names: tuple[str, ...]
    tools_lookup_contains_schedule: bool
    run_event_sink_types: tuple[str, ...]
    shared_event_sink_count: int
    tool_context_metadata: dict[str, Any] | None


class FakeRunner(Runner):
    """覆写 Runner.run；记录 run-scoped sinks 以便 watchdog 测试驱动。

    可配置：
    - ``result_factory``: 给定 (state) 返回 Result（用于 happy path）
    - ``raise_exc``: 跑期抛指定异常
    - ``activity_emitter``: 跑期定时 emit 活动事件（保活 watchdog）
    - ``run_duration``: 运行总时长（秒）；与 watchdog 配合
    - ``poll_interval``: 内部异步轮询粒度
    """

    def __init__(
        self,
        *,
        result_factory: Callable[[Sequence[Tool]], Result] | None = None,
        raise_exc: BaseException | None = None,
        activity_emitter: Callable[[], Awaitable[None]] | None = None,
        run_duration: float = 0.0,
        poll_interval: float = 0.02,
    ) -> None:
        super().__init__(event_sinks=[])
        self._result_factory = result_factory
        self._raise_exc = raise_exc
        self._activity_emitter = activity_emitter
        self._run_duration = run_duration
        self._poll_interval = poll_interval
        self.captured: list[_RunnerCallCapture] = []
        self.current_event_sinks: tuple[EventSink, ...] = ()

    async def run(
        self,
        user_input: str,
        *,
        session: Session,
        agent_spec: AgentSpec,
        llm: Any,
        tools: Any,
        approval: Any,
        max_turns: int | None = None,
        run_id: str | None = None,
        enabled_tools: Sequence[Tool] | None = None,
        event_sinks: Sequence[EventSink] | None = None,
        tool_context_metadata: dict[str, Any] | None = None,
    ) -> Result:
        # 记录调用入参
        names = tuple(t.name for t in (enabled_tools or ()))
        # 用 contains 判断是否还能命中 schedule.* —— 期望 False
        contains_schedule = any(
            n in tools  # type: ignore[operator]
            for n in ("schedule.create", "schedule.list", "cron.list")
        )
        run_sinks = tuple(event_sinks or ())
        self.captured.append(
            _RunnerCallCapture(
                user_input=user_input,
                session_id=session.session_id,
                run_id=run_id,
                enabled_tool_names=names,
                tools_lookup_contains_schedule=contains_schedule,
                run_event_sink_types=tuple(type(s).__name__ for s in run_sinks),
                shared_event_sink_count=len(self._event_sinks),
                tool_context_metadata=tool_context_metadata,
            )
        )
        self.current_event_sinks = run_sinks

        try:
            # 异常路径
            if self._raise_exc is not None:
                raise self._raise_exc

            # 长任务模拟：分片 sleep + 可选 emit
            elapsed = 0.0
            step = self._poll_interval
            while elapsed < self._run_duration:
                if self._activity_emitter is not None:
                    await self._activity_emitter()
                await asyncio.sleep(step)
                elapsed += step

            if self._result_factory is None:
                # default：completed + content="ok"
                return Result(
                    run_id=run_id or f"run-{session.session_id}-1",
                    session_id=session.session_id,
                    status="completed",
                    final_message=Message(role="assistant", content="ok"),
                    turn_count=1,
                )
            return self._result_factory(list(enabled_tools or ()))
        finally:
            self.current_event_sinks = ()


class _CaptureDispatcher:
    """记录 bridge 传入 dispatcher 的 task delivery target。"""

    def __init__(self) -> None:
        self.targets: list[str | None] = []

    async def deliver(
        self,
        task: ScheduledTask,
        run: ScheduledRun,
        final_message: str,
    ) -> DeliveryResult:
        del run, final_message
        self.targets.append(task.delivery.target if task.delivery is not None else None)
        return DeliveryResult.delivered(at=to_iso(utc_now()))


# ---------------------------------------------------------------------------
# 通用 fixture / helper
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(home_dir=tmp_path / "cron")


def _build_bridge(
    *,
    store: Store,
    runner: FakeRunner,
    enabled_tool_names: Sequence[str] = (),
    tools: dict[str, Tool] | None = None,
    inner_approval: Any | None = None,
    event_sinks: Sequence[EventSink] = (),
    poll_interval: float = 0.02,
    dispatcher: Any | None = None,
    trace_dir: Path | None = None,
    tool_context_metadata: dict[str, Any] | None = None,
    tool_context_metadata_factory: (Callable[[ScheduledTask], dict[str, Any] | None] | None) = None,
) -> ExecutionBridge:
    resolved_approval = inner_approval if inner_approval is not None else _AllowApproval()
    return ExecutionBridge(
        runtime=RunnerBackedScheduledRuntime(
            runner,
            approval=resolved_approval,
        ),
        llm=_StubLLM(),
        tools=tools if tools is not None else {},
        enabled_tool_names=enabled_tool_names,
        inner_approval=resolved_approval,
        session_factory=lambda sid: InMemorySession(sid),
        event_sinks=event_sinks,
        agent_spec=_make_agent_spec(),
        store=store,
        dispatcher=dispatcher,
        tool_context_metadata=tool_context_metadata,
        tool_context_metadata_factory=tool_context_metadata_factory,
        watchdog_poll_interval_seconds=poll_interval,
        trace_dir=trace_dir,
    )


# ---------------------------------------------------------------------------
# §1 Runner 成功路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_run_records_status(store: Store) -> None:
    runner = FakeRunner()
    task = _make_task()
    bridge = _build_bridge(store=store, runner=runner)

    run = await execute_bridge_for_test(bridge, _make_reservation(task))

    assert run.status is RunStatus.COMPLETED
    assert run.final_message_excerpt == "ok"
    assert run.silent_suppressed is False
    assert run.failure_reason is None
    # session_id / run_id 是 fresh 的
    assert run.session_id is not None and run.session_id != ""
    assert run.run_id != ""


@pytest.mark.asyncio
async def test_thread_bound_task_uses_fresh_run_session(store: Store) -> None:
    runner = FakeRunner()
    task = _make_task(thread_id="thread-aaaaaaaaaaaa")
    bridge = _build_bridge(store=store, runner=runner)

    run = await execute_bridge_for_test(bridge, _make_reservation(task))

    assert run.status is RunStatus.COMPLETED
    assert run.session_id.startswith(f"sched-{task.task_id}-")
    assert run.session_id != "thread-aaaaaaaaaaaa"
    assert runner.captured[0].session_id == run.session_id
    audits = store.list_audits(task_id=task.task_id)
    started = next(a for a in audits if a["action"] == "run_started")
    assert started["payload"]["thread_id"] == "thread-aaaaaaaaaaaa"
    assert started["payload"]["session_id"] == run.session_id


@pytest.mark.asyncio
async def test_bridge_passes_tool_context_metadata_to_runner(
    store: Store,
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    task = _make_task(thread_id="thread-aaaaaaaaaaaa")
    bridge = _build_bridge(
        store=store,
        runner=runner,
        tool_context_metadata={"cwd": str(tmp_path / "default"), "scope": "default"},
        tool_context_metadata_factory=lambda task: {
            "cwd": str(tmp_path / task.thread_id),
            "scope": "task",
        },
    )

    run = await execute_bridge_for_test(bridge, _make_reservation(task))

    assert run.status is RunStatus.COMPLETED
    assert runner.captured[0].tool_context_metadata == {
        "cwd": str(tmp_path / "thread-aaaaaaaaaaaa"),
        "scope": "task",
    }


@pytest.mark.asyncio
async def test_thread_bound_task_writes_context_to_fresh_run_session(store: Store) -> None:
    sessions: dict[str, InMemorySession] = {}

    def session_factory(sid: str) -> InMemorySession:
        session = sessions.get(sid)
        if session is None:
            session = InMemorySession(sid)
            sessions[sid] = session
        return session

    task = _make_task(thread_id="thread-cccccccccccc")
    approval = _AllowApproval()
    bridge = ExecutionBridge(
        runtime=RunnerBackedScheduledRuntime(Runner(), approval=approval),
        llm=_StubLLM("cron done"),
        tools={},
        enabled_tool_names=(),
        inner_approval=approval,
        session_factory=session_factory,
        event_sinks=(),
        agent_spec=_make_agent_spec(),
        store=store,
        watchdog_poll_interval_seconds=0.02,
    )

    run = await execute_bridge_for_test(bridge, _make_reservation(task))

    assert run.status is RunStatus.COMPLETED
    assert run.session_id.startswith(f"sched-{task.task_id}-")
    assert run.session_id != "thread-cccccccccccc"
    history = await sessions[run.session_id].history()
    assert [(m.role, m.content) for m in history] == [
        ("system", "you are an agent"),
        ("user", "do the thing"),
        ("assistant", "cron done"),
    ]


@pytest.mark.asyncio
async def test_thread_bound_task_keeps_target_delivery_metadata(store: Store) -> None:
    runner = FakeRunner()
    dispatcher = _CaptureDispatcher()
    task = _make_task(
        thread_id="thread-bbbbbbbbbbbb",
        delivery=ScheduleDelivery(
            channel=DeliveryChannel.WEB,
            target="thread:thread-bbbbbbbbbbbb",
        ),
    )
    bridge = _build_bridge(store=store, runner=runner, dispatcher=dispatcher)

    run = await execute_bridge_for_test(bridge, _make_reservation(task))

    assert run.status is RunStatus.COMPLETED
    assert run.delivery_status.value == "delivered"
    assert dispatcher.targets == ["thread:thread-bbbbbbbbbbbb"]


@pytest.mark.asyncio
async def test_silent_marker_short_circuits_to_silent(store: Store) -> None:
    def make(_: Sequence[Tool]) -> Result:
        return Result(
            run_id="r",
            session_id="s",
            status="completed",
            final_message=Message(role="assistant", content="[SILENT]"),
            turn_count=1,
        )

    runner = FakeRunner(result_factory=make)
    task = _make_task()
    bridge = _build_bridge(store=store, runner=runner)

    run = await execute_bridge_for_test(bridge, _make_reservation(task))

    assert run.status is RunStatus.SILENT
    assert run.silent_suppressed is True
    # 命中后 strip 剩空串 → 兜底为 "[SILENT]"
    assert run.final_message_excerpt == "[SILENT]"

    audits = store.list_audits(task_id=task.task_id)
    actions = [a["action"] for a in audits]
    assert "run_silent_suppressed" in actions


@pytest.mark.asyncio
async def test_silent_marker_with_trailing_text_is_stripped(store: Store) -> None:
    def make(_: Sequence[Tool]) -> Result:
        return Result(
            run_id="r",
            session_id="s",
            status="completed",
            final_message=Message(role="assistant", content="[SILENT] no diff"),
            turn_count=1,
        )

    runner = FakeRunner(result_factory=make)
    task = _make_task()
    bridge = _build_bridge(store=store, runner=runner)

    run = await execute_bridge_for_test(bridge, _make_reservation(task))

    assert run.status is RunStatus.SILENT
    assert run.silent_suppressed is True
    assert run.final_message_excerpt == "no diff"


# ---------------------------------------------------------------------------
# §2 watchdog
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_cancels_inactive_runner(store: Store) -> None:
    """长任务且不发活动事件 → watchdog 取消 → INACTIVITY_TIMEOUT。"""
    runner = FakeRunner(run_duration=0.5, poll_interval=0.02)
    approval = _AllowApproval()
    task = _make_task(inactivity_timeout_seconds=1)  # 1 秒，快进
    # 通过把 watchdog poll 间隔设小 + manual override timeout 走快测路径
    bridge = ExecutionBridge(
        runtime=RunnerBackedScheduledRuntime(runner, approval=approval),
        llm=_StubLLM(),
        tools={},
        enabled_tool_names=(),
        inner_approval=approval,
        session_factory=lambda sid: InMemorySession(sid),
        event_sinks=(),
        agent_spec=_make_agent_spec(),
        store=store,
        watchdog_poll_interval_seconds=0.02,
    )
    # 用 monkey-patch 的方式把 task.policy.inactivity_timeout_seconds 设小
    task = _make_task(inactivity_timeout_seconds=1)
    # 但 FakeRunner 跑 0.5s < 1s，watchdog 不会触发——把 run_duration 拉长
    runner._run_duration = 3.0  # type: ignore[attr-defined]

    run = await execute_bridge_for_test(bridge, _make_reservation(task))

    assert run.status is RunStatus.INACTIVITY_TIMEOUT
    assert run.failure_reason is RunFailureReason.INACTIVITY_TIMEOUT
    audits = store.list_audits(task_id=task.task_id)
    actions = [a["action"] for a in audits]
    assert "run_inactivity_timeout" in actions


@pytest.mark.asyncio
async def test_watchdog_does_not_cancel_when_active(store: Store) -> None:
    """长任务但持续 emit 活动 → 不取消，正常完成。"""
    # 我们需要从本次 run-scoped sinks 里抓到 watchdog 实例并主动喂活动事件。
    # FakeRunner 在跑期会调 activity_emitter → 我们让它 emit 到 current_event_sinks。
    captured_sinks: list[list[EventSink]] = []

    runner = FakeRunner(run_duration=0.4, poll_interval=0.02)

    async def emit_activity() -> None:
        # watchdog 已被 bridge 作为本次 run-scoped sink 传给 Runner.run。
        captured_sinks.append(list(runner.current_event_sinks))
        ev = Event(kind="content.delta", run_id="r", payload={"delta": "x"})
        for sink in runner.current_event_sinks:
            await sink.emit(ev)

    runner._activity_emitter = emit_activity  # type: ignore[attr-defined]

    task = _make_task(inactivity_timeout_seconds=1)  # 1s 超时 vs 0.4s 跑期
    bridge = _build_bridge(store=store, runner=runner, poll_interval=0.05)

    run = await execute_bridge_for_test(bridge, _make_reservation(task))

    assert run.status is RunStatus.COMPLETED
    # 至少有一次 sink list 包含 watchdog
    assert any(len(sinks) > 0 for sinks in captured_sinks)


@pytest.mark.asyncio
async def test_cron_run_sinks_are_run_scoped(store: Store, tmp_path: Path) -> None:
    """watchdog / per-run trace 走 Runner.run(event_sinks)，不污染共享列表。"""
    runner = FakeRunner()
    task = _make_task()
    bridge = _build_bridge(
        store=store,
        runner=runner,
        trace_dir=tmp_path / "traces",
    )

    run = await execute_bridge_for_test(bridge, _make_reservation(task))

    assert run.status is RunStatus.COMPLETED
    cap = runner.captured[0]
    assert "InactivityWatchdog" in cap.run_event_sink_types
    assert "JsonlTraceSink" in cap.run_event_sink_types
    assert cap.shared_event_sink_count == 0
    assert runner._event_sinks == []


@pytest.mark.asyncio
async def test_watchdog_disabled_when_timeout_none(store: Store) -> None:
    """timeout=None → watchdog 不取消，长跑能完成。"""
    runner = FakeRunner(run_duration=0.3, poll_interval=0.02)
    # ScheduledTask.policy.inactivity_timeout_seconds=None
    # Bridge 看到 None 会兜底成 DEFAULT_INACTIVITY_TIMEOUT=600 → 不会触发
    task = _make_task(inactivity_timeout_seconds=None)
    bridge = _build_bridge(store=store, runner=runner)

    run = await execute_bridge_for_test(bridge, _make_reservation(task))

    assert run.status is RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_watchdog_disabled_when_timeout_zero(store: Store) -> None:
    """timeout=0 → watchdog 视为禁用，长跑能完成。"""
    runner = FakeRunner(run_duration=0.2, poll_interval=0.02)
    task = _make_task(inactivity_timeout_seconds=0)
    bridge = _build_bridge(store=store, runner=runner)

    run = await execute_bridge_for_test(bridge, _make_reservation(task))

    assert run.status is RunStatus.COMPLETED


# ---------------------------------------------------------------------------
# §3 失败路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_raises_runtime_error(store: Store) -> None:
    runner = FakeRunner(raise_exc=RuntimeError("boom"))
    task = _make_task()
    bridge = _build_bridge(store=store, runner=runner)

    run = await execute_bridge_for_test(bridge, _make_reservation(task))

    assert run.status is RunStatus.FAILED
    assert run.failure_reason is RunFailureReason.RUNNER_EXCEPTION
    assert run.error_message is not None
    assert "RuntimeError" in run.error_message


@pytest.mark.asyncio
async def test_runner_returns_failed_with_needs_approval(store: Store) -> None:
    """Runner 返回 status=failed + error.message 含 'needs_approval' → NEEDS_APPROVAL。"""

    def make(_: Sequence[Tool]) -> Result:
        return Result(
            run_id="r",
            session_id="s",
            status="failed",
            final_message=None,
            turn_count=0,
            error=AgentError("needs_approval: shell denied"),
        )

    runner = FakeRunner(result_factory=make)
    task = _make_task()
    bridge = _build_bridge(store=store, runner=runner)

    run = await execute_bridge_for_test(bridge, _make_reservation(task))

    assert run.status is RunStatus.FAILED
    assert run.failure_reason is RunFailureReason.NEEDS_APPROVAL
    assert run.error_message is not None
    assert "needs_approval" in run.error_message


# ---------------------------------------------------------------------------
# §4 工具裁剪
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disallowed_tools_are_trimmed(store: Store) -> None:
    """schedule.* 与 cron.* 前缀工具被裁掉。"""
    tools_map = {
        "shell": _make_tool("shell"),
        "schedule.create": _make_tool("schedule.create"),
        "cron.list": _make_tool("cron.list"),
        "read": _make_tool("read"),
    }
    runner = FakeRunner()
    bridge = _build_bridge(
        store=store,
        runner=runner,
        enabled_tool_names=("shell", "schedule.create", "cron.list", "read"),
        tools=tools_map,
    )

    run = await execute_bridge_for_test(bridge, _make_reservation(_make_task()))

    assert run.status is RunStatus.COMPLETED
    assert len(runner.captured) == 1
    cap = runner.captured[0]
    assert set(cap.enabled_tool_names) == {"shell", "read"}
    # tools lookup 也不应再命中 schedule.*
    assert cap.tools_lookup_contains_schedule is False


@pytest.mark.asyncio
async def test_disallowed_bare_names_are_trimmed(store: Store) -> None:
    """单字调度工具与 lifecycle-bound 进化请求均从 cron run 裁掉。"""
    tools_map = {
        "shell": _make_tool("shell"),
        "schedule": _make_tool("schedule"),
        "cron": _make_tool("cron"),
        "request_evolution_review": _make_tool("request_evolution_review"),
        "read": _make_tool("read"),
    }
    runner = FakeRunner()
    bridge = _build_bridge(
        store=store,
        runner=runner,
        enabled_tool_names=(
            "shell",
            "schedule",
            "cron",
            "request_evolution_review",
            "read",
        ),
        tools=tools_map,
    )

    run = await execute_bridge_for_test(bridge, _make_reservation(_make_task()))

    assert run.status is RunStatus.COMPLETED
    cap = runner.captured[0]
    assert set(cap.enabled_tool_names) == {"shell", "read"}
    # tools lookup 也不应再命中单字 schedule / cron
    assert "schedule" not in cap.enabled_tool_names
    assert "cron" not in cap.enabled_tool_names
    assert "request_evolution_review" not in cap.enabled_tool_names


@pytest.mark.asyncio
async def test_disallowed_mixed_bare_and_prefix_are_trimmed(store: Store) -> None:
    """混合：单字 'schedule' + 'schedule.list' + 普通 'memory' → 仅 memory 保留。

    这是 v0.2 schedule_tool 装载后的典型 enabled_tool_names 形态：
    既有 v0.2 单字 schedule，也有未来扩展的 schedule.* namespace 工具。
    任何带 schedule/cron 字样的工具都不应进入 cron run。
    """
    tools_map = {
        "schedule": _make_tool("schedule"),
        "memory": _make_tool("memory"),
        "schedule.list": _make_tool("schedule.list"),
    }
    runner = FakeRunner()
    bridge = _build_bridge(
        store=store,
        runner=runner,
        enabled_tool_names=("schedule", "memory", "schedule.list"),
        tools=tools_map,
    )

    run = await execute_bridge_for_test(bridge, _make_reservation(_make_task()))

    assert run.status is RunStatus.COMPLETED
    cap = runner.captured[0]
    assert list(cap.enabled_tool_names) == ["memory"]
    assert "schedule" not in cap.enabled_tool_names
    assert "schedule.list" not in cap.enabled_tool_names


@pytest.mark.asyncio
async def test_no_disallowed_prefix_keeps_all_tools(store: Store) -> None:
    """不带 schedule/cron 前缀 → 全部保留。"""
    tools_map = {
        "shell": _make_tool("shell"),
        "read": _make_tool("read"),
        "memory": _make_tool("memory"),
    }
    runner = FakeRunner()
    bridge = _build_bridge(
        store=store,
        runner=runner,
        enabled_tool_names=("shell", "read", "memory"),
        tools=tools_map,
    )

    await execute_bridge_for_test(bridge, _make_reservation(_make_task()))

    cap = runner.captured[0]
    assert set(cap.enabled_tool_names) == {"shell", "read", "memory"}


# ---------------------------------------------------------------------------
# §5 Store / audit 写回
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_final_run_is_appended_and_supersedes_running(store: Store) -> None:
    runner = FakeRunner()
    task = _make_task()
    bridge = _build_bridge(store=store, runner=runner)

    final = await execute_bridge_for_test(bridge, _make_reservation(task))

    runs = store.list_runs(task.task_id)
    # list_runs 折叠相同 run_id：只保留最新；旧 RUNNING 被 supersede 掉
    assert len(runs) == 1
    assert runs[0].run_id == final.run_id
    assert runs[0].status is RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_run_started_and_finished_audits(store: Store) -> None:
    runner = FakeRunner()
    task = _make_task()
    bridge = _build_bridge(store=store, runner=runner)

    await execute_bridge_for_test(bridge, _make_reservation(task))

    audits = store.list_audits(task_id=task.task_id)
    actions = [a["action"] for a in audits]
    assert "run_started" in actions
    assert "run_finished" in actions


@pytest.mark.asyncio
async def test_silent_audit_includes_silent_suppressed(store: Store) -> None:
    def make(_: Sequence[Tool]) -> Result:
        return Result(
            run_id="r",
            session_id="s",
            status="completed",
            final_message=Message(role="assistant", content="[SILENT]"),
            turn_count=1,
        )

    runner = FakeRunner(result_factory=make)
    task = _make_task()
    bridge = _build_bridge(store=store, runner=runner)

    await execute_bridge_for_test(bridge, _make_reservation(task))

    audits = store.list_audits(task_id=task.task_id)
    actions = [a["action"] for a in audits]
    assert "run_silent_suppressed" in actions


@pytest.mark.asyncio
async def test_inactivity_timeout_writes_audit(store: Store) -> None:
    runner = FakeRunner(run_duration=2.0, poll_interval=0.02)
    task = _make_task(inactivity_timeout_seconds=1)
    bridge = _build_bridge(store=store, runner=runner, poll_interval=0.02)

    run = await execute_bridge_for_test(bridge, _make_reservation(task))

    assert run.status is RunStatus.INACTIVITY_TIMEOUT
    audits = store.list_audits(task_id=task.task_id)
    actions = [a["action"] for a in audits]
    assert "run_started" in actions
    assert "run_inactivity_timeout" in actions
    incidents = store.list_incidents(task_id=task.task_id)
    assert [i["action"] for i in incidents] == ["run_inactivity_timeout"]


# ---------------------------------------------------------------------------
# §6 InactivityWatchdog 单测（直接覆盖 watchdog 行为）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_emit_refreshes_only_on_activity_kinds() -> None:
    """非活动 kind 不刷新时间戳。"""
    fake_now = [100.0]

    def clock() -> float:
        return fake_now[0]

    wd = InactivityWatchdog(timeout_seconds=10, poll_interval_seconds=0.01, clock=clock)
    initial = wd._last_activity_ts
    await wd.emit(Event(kind="approval.request", run_id="r"))
    assert wd._last_activity_ts == initial  # 未刷新

    fake_now[0] = 200.0
    await wd.emit(Event(kind="content.delta", run_id="r"))
    assert wd._last_activity_ts == 200.0
