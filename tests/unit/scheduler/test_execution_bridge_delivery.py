"""v0.3 cron-delivery M3：``ExecutionBridge`` + ``DeliveryDispatcher`` 集成测试。

覆盖 ``execute()`` 完成路径在 supersede 之前调 dispatcher、把投递结果合并到
``ScheduledRun`` 落盘的端到端行为。

与 ``test_delivery_dispatcher.py`` 的分工：
- ``test_delivery_dispatcher.py``：dispatcher / sink / DeliveryResult 单元
- 本文件：bridge.execute → dispatcher.deliver → run 字段写盘的端到端路径

复用 ``test_scheduler_execution_bridge.py`` 已有的 FakeRunner / _make_task /
_StubLLM 等 helper —— 但保持本文件 import 自包含（不跨 test 文件 import）。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from core.agent_spec import AgentSpec
from core.contracts import EventSink
from core.message import Message
from core.result import Result
from core.runner import Runner
from core.session import InMemorySession
from scheduler.delivery import (
    DeliveryDispatcher,
    DeliveryResult,
    DeliverySink,
)
from scheduler.domain import (
    ConcurrencyPolicy,
    DeliveryChannel,
    DeliveryStatus,
    DueTaskReservation,
    MisfirePolicy,
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
from scheduler.execution_bridge import ExecutionBridge
from scheduler.store import Store
from scheduler.timing import to_iso, utc_now

# ---------------------------------------------------------------------------
# Helpers（最小化复制，保持测试自包含）
# ---------------------------------------------------------------------------


class _StubLLM:
    """LLMProvider 占位；ExecutionBridge 不直接调用，只透传给 Runner。"""


class _AllowApproval:
    """允许所有 tool；不被本测试用到，仅作占位。"""

    async def decide(self, call):  # type: ignore[no-untyped-def]
        from core.contracts import ApprovalDecision

        return ApprovalDecision.allow()


class _CompletedRunner(Runner):
    """Runner 替代品：直接返回 completed Result，``final_message`` 可配置。"""

    def __init__(self, *, final_text: str = "ok") -> None:
        super().__init__(event_sinks=[])
        self._final_text = final_text

    async def run(self, *args: Any, **kwargs: Any) -> Result:  # type: ignore[override]
        sess = kwargs.get("session")
        return Result(
            run_id=kwargs.get("run_id", "rid"),
            session_id=sess.session_id if sess is not None else "x",
            status="completed",
            final_message=Message(role="assistant", content=self._final_text),
            turn_count=1,
        )


def _make_task(
    *,
    task_id: str = "t-int",
    delivery: ScheduleDelivery | None = None,
    silent_marker_enabled: bool = True,
) -> ScheduledTask:
    now = to_iso(utc_now())
    return ScheduledTask(
        task_id=task_id,
        name=f"task-{task_id}",
        enabled=True,
        state=TaskState.SCHEDULED,
        origin=TaskOrigin.CLI,
        trigger=ScheduleTrigger(
            trigger_type=TriggerType.INTERVAL, expr="5", timezone="UTC"
        ),
        policy=TaskExecutionPolicy(
            session_mode=SessionMode.FRESH_SESSION,
            concurrency_policy=ConcurrencyPolicy.FORBID,
            misfire_policy=MisfirePolicy.SKIP,
            max_turns=5,
            inactivity_timeout_seconds=600,
            silent_marker_enabled=silent_marker_enabled,
        ),
        target=TaskTarget(
            agent_name="agent-x", input_text="do thing", metadata={}
        ),
        next_run_at=now,
        last_run_at=None,
        created_by="cli",
        created_at=now,
        updated_at=now,
        delivery=delivery,
    )


def _make_reservation(task: ScheduledTask) -> DueTaskReservation:
    now = to_iso(utc_now())
    return DueTaskReservation(
        task=task, scheduled_for=task.next_run_at or now, reserved_at=now
    )


def _build_bridge(
    *,
    store: Store,
    runner: Runner,
    dispatcher: DeliveryDispatcher | None = None,
    enabled_tool_names: Sequence[str] = (),
    event_sinks: Sequence[EventSink] = (),
) -> ExecutionBridge:
    return ExecutionBridge(
        runner=runner,
        llm=_StubLLM(),  # type: ignore[arg-type]
        tools={},
        enabled_tool_names=enabled_tool_names,
        inner_approval=_AllowApproval(),  # type: ignore[arg-type]
        session_factory=lambda sid: InMemorySession(sid),
        event_sinks=event_sinks,
        agent_spec=AgentSpec(
            name="agent-x", instructions="you are an agent", default_model="m"
        ),
        store=store,
        dispatcher=dispatcher,
        watchdog_poll_interval_seconds=0.02,
    )


class _StubSink(DeliverySink):
    def __init__(self, *, return_result: DeliveryResult | None = None) -> None:
        self.calls: list[tuple[ScheduledTask, ScheduledRun, str]] = []
        self._return = return_result or DeliveryResult.delivered(
            at="2026-05-04T01:00:00+00:00"
        )

    async def deliver(self, task, run, final_message):  # type: ignore[no-untyped-def]
        self.calls.append((task, run, final_message))
        return self._return


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(home_dir=tmp_path / "cron")


# ---------------------------------------------------------------------------
# 测试矩阵
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_without_dispatcher_keeps_v02_behavior(store: Store) -> None:
    """dispatcher=None（默认）→ run 不带 delivery 字段更新（保持 v0.2 行为）。"""
    runner = _CompletedRunner(final_text="hello v0.2")
    task = _make_task(delivery=None)
    bridge = _build_bridge(store=store, runner=runner, dispatcher=None)

    run = await bridge.execute(_make_reservation(task))

    assert run.status is RunStatus.COMPLETED
    # 未集成 dispatcher → 字段保持默认（v0.3 schema 默认 PENDING / None）
    assert run.delivery_status is DeliveryStatus.PENDING
    assert run.delivered_at is None
    assert run.delivery_error is None


@pytest.mark.asyncio
async def test_bridge_with_dispatcher_web_channel_delivers(store: Store) -> None:
    """dispatcher 装配 + task.delivery.channel=web → web_sink 被调，run 含 DELIVERED。"""
    runner = _CompletedRunner(final_text="hello via web")
    web_sink = _StubSink()
    dispatcher = DeliveryDispatcher(web_sink=web_sink)
    task = _make_task(delivery=ScheduleDelivery(channel=DeliveryChannel.WEB))
    bridge = _build_bridge(store=store, runner=runner, dispatcher=dispatcher)

    run = await bridge.execute(_make_reservation(task))

    assert run.status is RunStatus.COMPLETED
    assert run.delivery_status is DeliveryStatus.DELIVERED
    assert run.delivered_at == "2026-05-04T01:00:00+00:00"
    assert run.delivery_error is None
    assert len(web_sink.calls) == 1


@pytest.mark.asyncio
async def test_bridge_no_delivery_config_skipped(store: Store) -> None:
    """task.delivery is None → dispatcher 返回 SKIPPED，run.delivery_status=SKIPPED。"""
    runner = _CompletedRunner()
    web_sink = _StubSink()
    dispatcher = DeliveryDispatcher(web_sink=web_sink)
    task = _make_task(delivery=None)
    bridge = _build_bridge(store=store, runner=runner, dispatcher=dispatcher)

    run = await bridge.execute(_make_reservation(task))

    assert run.delivery_status is DeliveryStatus.SKIPPED
    assert web_sink.calls == []


@pytest.mark.asyncio
async def test_bridge_silent_marker_skips_delivery(store: Store) -> None:
    """silent_marker 命中 → run.status=SILENT，dispatcher SKIPPED；sink 不被调。"""
    runner = _CompletedRunner(final_text="[SILENT] internal note")
    web_sink = _StubSink()
    dispatcher = DeliveryDispatcher(web_sink=web_sink)
    task = _make_task(
        delivery=ScheduleDelivery(channel=DeliveryChannel.WEB),
        silent_marker_enabled=True,
    )
    bridge = _build_bridge(store=store, runner=runner, dispatcher=dispatcher)

    run = await bridge.execute(_make_reservation(task))

    # bridge 自己把 [SILENT] 转成 RunStatus.SILENT；dispatcher 再依此 skip
    assert run.status is RunStatus.SILENT
    assert run.delivery_status is DeliveryStatus.SKIPPED
    assert web_sink.calls == []


@pytest.mark.asyncio
async def test_bridge_sink_exception_does_not_break_run(store: Store) -> None:
    """sink 抛异常 → run.status 仍为 COMPLETED；delivery_status=FAILED + error。"""

    class _BoomSink(DeliverySink):
        async def deliver(self, task, run, msg):  # type: ignore[no-untyped-def]
            raise RuntimeError("ws broker down")

    runner = _CompletedRunner(final_text="hi")
    dispatcher = DeliveryDispatcher(web_sink=_BoomSink())
    task = _make_task(delivery=ScheduleDelivery(channel=DeliveryChannel.WEB))
    bridge = _build_bridge(store=store, runner=runner, dispatcher=dispatcher)

    run = await bridge.execute(_make_reservation(task))

    # 关键不变量：sink 异常**不污染** agent run.status
    assert run.status is RunStatus.COMPLETED
    assert run.delivery_status is DeliveryStatus.FAILED
    assert run.delivery_error is not None
    assert "RuntimeError" in run.delivery_error
    assert "ws broker down" in run.delivery_error


@pytest.mark.asyncio
async def test_bridge_run_is_persisted_with_delivery_fields(store: Store) -> None:
    """delivery 字段经过 store.supersede_and_append_run 落盘 + 重 load 不丢。"""
    runner = _CompletedRunner(final_text="persist test")
    web_sink = _StubSink(
        return_result=DeliveryResult.delivered(at="2026-05-04T02:00:00+00:00")
    )
    dispatcher = DeliveryDispatcher(web_sink=web_sink)
    task = _make_task(delivery=ScheduleDelivery(channel=DeliveryChannel.WEB))
    bridge = _build_bridge(store=store, runner=runner, dispatcher=dispatcher)

    run_returned = await bridge.execute(_make_reservation(task))

    # 重 load store 验证字段持久化
    fetched = store.get_latest_run(task.task_id)
    assert fetched is not None
    assert fetched.run_id == run_returned.run_id
    assert fetched.delivery_status is DeliveryStatus.DELIVERED
    assert fetched.delivered_at == "2026-05-04T02:00:00+00:00"


@pytest.mark.asyncio
async def test_bridge_dispatcher_self_crash_logs_traceback(store: Store) -> None:
    """v0.3 R2 加固：dispatcher 自身崩（半坏对象，不是 sink 崩）→ bridge 外层
    catch 兜底；error_message 含 traceback 帮定位。

    场景：装配错误把"半坏 dispatcher"传进来（如 mock 漏方法）。dispatcher
    不是 None 但 .deliver() 不可调用 → AttributeError → bridge `_apply_delivery`
    外层 catch → run.delivery_status=FAILED + error 带 traceback。
    """

    class _BrokenDispatcher:
        """没有 deliver 方法；调用 .deliver() 会抛 AttributeError。"""

        # 故意不实现 deliver

    runner = _CompletedRunner(final_text="hi")
    broken: Any = _BrokenDispatcher()
    task = _make_task(delivery=ScheduleDelivery(channel=DeliveryChannel.WEB))
    bridge = _build_bridge(store=store, runner=runner, dispatcher=broken)

    run = await bridge.execute(_make_reservation(task))

    assert run.status is RunStatus.COMPLETED  # agent run 不被污染
    assert run.delivery_status is DeliveryStatus.FAILED
    assert run.delivery_error is not None
    assert "dispatcher_AttributeError" in run.delivery_error
    # 加 traceback 后应包含 "execution_bridge.py" 关键字（traceback 帧路径）
    assert "execution_bridge" in run.delivery_error or "_apply_delivery" in run.delivery_error


@pytest.mark.asyncio
async def test_bridge_no_sink_for_channel_skipped(store: Store) -> None:
    """task.delivery.channel=cli 但 dispatcher 没装 cli_sink → SKIPPED。"""
    runner = _CompletedRunner()
    dispatcher = DeliveryDispatcher(web_sink=_StubSink(), cli_sink=None)
    task = _make_task(delivery=ScheduleDelivery(channel=DeliveryChannel.CLI))
    bridge = _build_bridge(store=store, runner=runner, dispatcher=dispatcher)

    run = await bridge.execute(_make_reservation(task))

    assert run.delivery_status is DeliveryStatus.SKIPPED
    assert run.delivery_error is None  # SKIPPED 时不写 error
