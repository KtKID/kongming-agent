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
    TaskOrigin,
    TaskState,
    TaskTarget,
    TriggerType,
)
from scheduler.store import Store
from scheduler.timing import to_iso, utc_now

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

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
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
        enabled=True,
        state=TaskState.SCHEDULED,
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


class FakeRunner(Runner):
    """覆写 Runner.run；保留构造参数（含 _event_sinks 列表）以便 watchdog 注入。

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
    ) -> Result:
        # 记录调用入参
        names = tuple(t.name for t in (enabled_tools or ()))
        # 用 contains 判断是否还能命中 schedule.* —— 期望 False
        contains_schedule = any(
            n in tools  # type: ignore[operator]
            for n in ("schedule.create", "schedule.list", "cron.list")
        )
        self.captured.append(
            _RunnerCallCapture(
                user_input=user_input,
                session_id=session.session_id,
                run_id=run_id,
                enabled_tool_names=names,
                tools_lookup_contains_schedule=contains_schedule,
            )
        )

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
) -> ExecutionBridge:
    return ExecutionBridge(
        runner=runner,
        llm=_StubLLM(),
        tools=tools if tools is not None else {},
        enabled_tool_names=enabled_tool_names,
        inner_approval=inner_approval if inner_approval is not None else _AllowApproval(),
        session_factory=lambda sid: InMemorySession(sid),
        event_sinks=event_sinks,
        agent_spec=_make_agent_spec(),
        store=store,
        dispatcher=dispatcher,
        watchdog_poll_interval_seconds=poll_interval,
    )


# ---------------------------------------------------------------------------
# §1 Runner 成功路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_run_records_status(store: Store) -> None:
    runner = FakeRunner()
    task = _make_task()
    bridge = _build_bridge(store=store, runner=runner)

    run = await bridge.execute(_make_reservation(task))

    assert run.status is RunStatus.COMPLETED
    assert run.final_message_excerpt == "ok"
    assert run.silent_suppressed is False
    assert run.failure_reason is None
    # session_id / run_id 是 fresh 的
    assert run.session_id is not None and run.session_id != ""
    assert run.run_id != ""


@pytest.mark.asyncio
async def test_thread_bound_task_uses_thread_session(store: Store) -> None:
    runner = FakeRunner()
    task = _make_task(thread_id="thread-aaaaaaaaaaaa")
    bridge = _build_bridge(store=store, runner=runner)

    run = await bridge.execute(_make_reservation(task))

    assert run.status is RunStatus.COMPLETED
    assert run.session_id == "thread-aaaaaaaaaaaa"
    assert runner.captured[0].session_id == "thread-aaaaaaaaaaaa"
    audits = store.list_audits(task_id=task.task_id)
    started = next(a for a in audits if a["action"] == "run_started")
    assert started["payload"]["thread_id"] == "thread-aaaaaaaaaaaa"


@pytest.mark.asyncio
async def test_thread_bound_task_writes_context_to_thread_session(store: Store) -> None:
    sessions: dict[str, InMemorySession] = {}

    def session_factory(sid: str) -> InMemorySession:
        session = sessions.get(sid)
        if session is None:
            session = InMemorySession(sid)
            sessions[sid] = session
        return session

    task = _make_task(thread_id="thread-cccccccccccc")
    bridge = ExecutionBridge(
        runner=Runner(),
        llm=_StubLLM("cron done"),
        tools={},
        enabled_tool_names=(),
        inner_approval=_AllowApproval(),
        session_factory=session_factory,
        event_sinks=(),
        agent_spec=_make_agent_spec(),
        store=store,
        watchdog_poll_interval_seconds=0.02,
    )

    run = await bridge.execute(_make_reservation(task))

    assert run.status is RunStatus.COMPLETED
    assert run.session_id == "thread-cccccccccccc"
    history = await sessions["thread-cccccccccccc"].history()
    assert [(m.role, m.content) for m in history] == [
        ("system", "you are an agent"),
        ("user", "do the thing"),
        ("assistant", "cron done"),
    ]


@pytest.mark.asyncio
async def test_thread_bound_task_suppresses_target_delivery_duplicate(store: Store) -> None:
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

    run = await bridge.execute(_make_reservation(task))

    assert run.status is RunStatus.COMPLETED
    assert run.delivery_status.value == "delivered"
    assert dispatcher.targets == [None]


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

    run = await bridge.execute(_make_reservation(task))

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

    run = await bridge.execute(_make_reservation(task))

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
    task = _make_task(inactivity_timeout_seconds=1)  # 1 秒，快进
    # 通过把 watchdog poll 间隔设小 + manual override timeout 走快测路径
    bridge = ExecutionBridge(
        runner=runner,
        llm=_StubLLM(),
        tools={},
        enabled_tool_names=(),
        inner_approval=_AllowApproval(),
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

    run = await bridge.execute(_make_reservation(task))

    assert run.status is RunStatus.INACTIVITY_TIMEOUT
    assert run.failure_reason is RunFailureReason.INACTIVITY_TIMEOUT
    audits = store.list_audits(task_id=task.task_id)
    actions = [a["action"] for a in audits]
    assert "run_inactivity_timeout" in actions


@pytest.mark.asyncio
async def test_watchdog_does_not_cancel_when_active(store: Store) -> None:
    """长任务但持续 emit 活动 → 不取消，正常完成。"""
    # 我们需要从 runner._event_sinks 里抓到 watchdog 实例并主动喂活动事件。
    # FakeRunner 在跑期会调 activity_emitter → 我们让它 emit 到 runner._event_sinks。
    captured_sinks: list[list[EventSink]] = []

    runner = FakeRunner(run_duration=0.4, poll_interval=0.02)

    async def emit_activity() -> None:
        # runner._event_sinks 是 list；watchdog 已被 bridge append 进来
        captured_sinks.append(list(runner._event_sinks))
        ev = Event(kind="content.delta", run_id="r", payload={"delta": "x"})
        for sink in runner._event_sinks:
            await sink.emit(ev)

    runner._activity_emitter = emit_activity  # type: ignore[attr-defined]

    task = _make_task(inactivity_timeout_seconds=1)  # 1s 超时 vs 0.4s 跑期
    bridge = _build_bridge(store=store, runner=runner, poll_interval=0.05)

    run = await bridge.execute(_make_reservation(task))

    assert run.status is RunStatus.COMPLETED
    # 至少有一次 sink list 包含 watchdog
    assert any(len(sinks) > 0 for sinks in captured_sinks)


@pytest.mark.asyncio
async def test_watchdog_disabled_when_timeout_none(store: Store) -> None:
    """timeout=None → watchdog 不取消，长跑能完成。"""
    runner = FakeRunner(run_duration=0.3, poll_interval=0.02)
    # ScheduledTask.policy.inactivity_timeout_seconds=None
    # Bridge 看到 None 会兜底成 DEFAULT_INACTIVITY_TIMEOUT=600 → 不会触发
    task = _make_task(inactivity_timeout_seconds=None)
    bridge = _build_bridge(store=store, runner=runner)

    run = await bridge.execute(_make_reservation(task))

    assert run.status is RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_watchdog_disabled_when_timeout_zero(store: Store) -> None:
    """timeout=0 → watchdog 视为禁用，长跑能完成。"""
    runner = FakeRunner(run_duration=0.2, poll_interval=0.02)
    task = _make_task(inactivity_timeout_seconds=0)
    bridge = _build_bridge(store=store, runner=runner)

    run = await bridge.execute(_make_reservation(task))

    assert run.status is RunStatus.COMPLETED


# ---------------------------------------------------------------------------
# §3 失败路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_raises_runtime_error(store: Store) -> None:
    runner = FakeRunner(raise_exc=RuntimeError("boom"))
    task = _make_task()
    bridge = _build_bridge(store=store, runner=runner)

    run = await bridge.execute(_make_reservation(task))

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

    run = await bridge.execute(_make_reservation(task))

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

    run = await bridge.execute(_make_reservation(_make_task()))

    assert run.status is RunStatus.COMPLETED
    assert len(runner.captured) == 1
    cap = runner.captured[0]
    assert set(cap.enabled_tool_names) == {"shell", "read"}
    # tools lookup 也不应再命中 schedule.*
    assert cap.tools_lookup_contains_schedule is False


@pytest.mark.asyncio
async def test_disallowed_bare_names_are_trimmed(store: Store) -> None:
    """单字 'schedule' / 'cron' 也被裁掉（防 v0.2 ScheduleTool 在 cron run 内被调）。"""
    tools_map = {
        "shell": _make_tool("shell"),
        "schedule": _make_tool("schedule"),
        "cron": _make_tool("cron"),
        "read": _make_tool("read"),
    }
    runner = FakeRunner()
    bridge = _build_bridge(
        store=store,
        runner=runner,
        enabled_tool_names=("shell", "schedule", "cron", "read"),
        tools=tools_map,
    )

    run = await bridge.execute(_make_reservation(_make_task()))

    assert run.status is RunStatus.COMPLETED
    cap = runner.captured[0]
    assert set(cap.enabled_tool_names) == {"shell", "read"}
    # tools lookup 也不应再命中单字 schedule / cron
    assert "schedule" not in cap.enabled_tool_names
    assert "cron" not in cap.enabled_tool_names


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

    run = await bridge.execute(_make_reservation(_make_task()))

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

    await bridge.execute(_make_reservation(_make_task()))

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

    final = await bridge.execute(_make_reservation(task))

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

    await bridge.execute(_make_reservation(task))

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

    await bridge.execute(_make_reservation(task))

    audits = store.list_audits(task_id=task.task_id)
    actions = [a["action"] for a in audits]
    assert "run_silent_suppressed" in actions


@pytest.mark.asyncio
async def test_inactivity_timeout_writes_audit(store: Store) -> None:
    runner = FakeRunner(run_duration=2.0, poll_interval=0.02)
    task = _make_task(inactivity_timeout_seconds=1)
    bridge = _build_bridge(store=store, runner=runner, poll_interval=0.02)

    run = await bridge.execute(_make_reservation(task))

    assert run.status is RunStatus.INACTIVITY_TIMEOUT
    audits = store.list_audits(task_id=task.task_id)
    actions = [a["action"] for a in audits]
    assert "run_started" in actions
    assert "run_inactivity_timeout" in actions


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


# ---------------------------------------------------------------------------
# §7 concurrency_policy 集成（Wave D1，#18）
# ---------------------------------------------------------------------------


def _seed_running_run(store: Store, task_id: str, run_id: str) -> None:
    """直接往 Store 里写一行 RUNNING run（绕过 Bridge.execute）。"""
    now = to_iso(utc_now())
    store.append_run(
        ScheduledRun(
            run_id=run_id,
            task_id=task_id,
            status=RunStatus.RUNNING,
            scheduled_for=now,
            started_at=now,
            finished_at=None,
            session_id=f"sess-{run_id}",
            result_status=None,
            final_message_excerpt=None,
            error_message=None,
            failure_reason=None,
            delivery_error=None,
            silent_suppressed=False,
        )
    )


@pytest.mark.asyncio
async def test_concurrency_allow_proceeds_normally(store: Store) -> None:
    """ALLOW 即使有 RUNNING 也照常 execute。"""
    runner = FakeRunner()
    task = _make_task(task_id="t-allow", concurrency_policy=ConcurrencyPolicy.ALLOW)
    store.create_task(task)
    _seed_running_run(store, task.task_id, "r-old")

    bridge = _build_bridge(store=store, runner=runner)
    run = await bridge.execute(_make_reservation(task))

    assert run.status is RunStatus.COMPLETED
    # FakeRunner.run 被调一次
    assert len(runner.captured) == 1
    # 旧 RUNNING 行未被改动（list_runs 折叠后仍存在 RUNNING + 新 COMPLETED）
    runs = store.list_runs(task.task_id)
    statuses = sorted(r.status for r in runs)
    assert RunStatus.RUNNING in statuses
    assert RunStatus.COMPLETED in statuses


@pytest.mark.asyncio
async def test_concurrency_forbid_with_running_skips(store: Store) -> None:
    """FORBID + 已有 RUNNING → 不调 Runner，落 CANCELLED 合成行 + audit。"""
    runner = FakeRunner()
    task = _make_task(task_id="t-forbid", concurrency_policy=ConcurrencyPolicy.FORBID)
    store.create_task(task)
    _seed_running_run(store, task.task_id, "r-old-forbid")

    bridge = _build_bridge(store=store, runner=runner)
    run = await bridge.execute(_make_reservation(task))

    assert run.status is RunStatus.CANCELLED
    assert run.session_id is None
    assert run.error_message is not None
    assert "r-old-forbid" in run.error_message
    # FakeRunner.run 没被调用
    assert runner.captured == []
    # 旧 RUNNING 行依然 RUNNING
    runs = store.list_runs(task.task_id)
    old = next(r for r in runs if r.run_id == "r-old-forbid")
    assert old.status is RunStatus.RUNNING
    # audit 写了 run_skipped_by_concurrency
    audits = store.list_audits(task_id=task.task_id)
    actions = [a["action"] for a in audits]
    assert "run_skipped_by_concurrency" in actions


@pytest.mark.asyncio
async def test_concurrency_replace_with_running_cancels_old_and_runs(
    store: Store,
) -> None:
    """REPLACE + 已有 RUNNING → 旧 run CANCELLED，新 run 启动并完成。"""
    runner = FakeRunner()
    task = _make_task(task_id="t-replace", concurrency_policy=ConcurrencyPolicy.REPLACE)
    store.create_task(task)
    _seed_running_run(store, task.task_id, "r-old-replace")

    bridge = _build_bridge(store=store, runner=runner)
    run = await bridge.execute(_make_reservation(task))

    # 新 run 跑成功
    assert run.status is RunStatus.COMPLETED
    assert len(runner.captured) == 1
    assert run.run_id != "r-old-replace"
    # 旧 run 已被 cancel
    runs = store.list_runs(task.task_id)
    old = next(r for r in runs if r.run_id == "r-old-replace")
    assert old.status is RunStatus.CANCELLED
    assert old.finished_at is not None
    # audit：run_cancelled_by_replace + run_started + run_finished 都在
    audits = store.list_audits(task_id=task.task_id)
    actions = [a["action"] for a in audits]
    assert "run_cancelled_by_replace" in actions
    assert "run_started" in actions
    assert "run_finished" in actions
