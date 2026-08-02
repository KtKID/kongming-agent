"""ScheduledRunManager 统一 live owner 测试。

通过真实 HostDispatcher/AgentManager/TaskRegistry 启动链验证 reservation 幂等、
ALLOW/FORBID/REPLACE 和可取消 live handle；仅 Runner 执行体使用可控 fake。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

import pytest

from application.scheduled_runs.manager import (
    ScheduledRunManager,
)
from hosts.shared.host_dispatcher import build_scheduled_run_dispatcher_factory
from scheduler.domain import (
    ConcurrencyPolicy,
    DueTaskReservation,
    RunStatus,
    ScheduledRun,
    ScheduledRunSubmitDisposition,
    ScheduledTask,
    ScheduleTrigger,
    TaskExecutionPolicy,
    TaskLifecycleState,
    TaskOrigin,
    TaskTarget,
    TriggerType,
)


class _Runtime:
    """HostDispatcher 的最小 runtime 载体；root 执行由注入 bridge 接管。"""


class _BlockingLifecycleSink:
    """阻塞 finished 通知，验证 durable result 与 live owner 已先收口。"""

    def __init__(self) -> None:
        self.finished_entered = asyncio.Event()
        self.release_finished = asyncio.Event()

    async def run_started(
        self,
        task: ScheduledTask,
        run: ScheduledRun,
    ) -> None:
        del task, run

    async def run_finished(
        self,
        task: ScheduledTask,
        run: ScheduledRun,
    ) -> None:
        del task, run
        self.finished_entered.set()
        await self.release_finished.wait()


class _BlockingInterruptDispatcher:
    """在真实 dispatcher 外层冻结 interrupt，制造 cancel/close 交错。"""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.interrupt_entered = asyncio.Event()
        self.release_interrupt = asyncio.Event()

    async def run_scheduled_text(
        self,
        user_input: str,
        *,
        metadata: dict[str, object],
    ) -> object:
        return await self._inner.run_scheduled_text(  # type: ignore[attr-defined,no-any-return]
            user_input,
            metadata=metadata,
        )

    def list_task_records(
        self,
        *,
        include_finished: bool = False,
    ) -> tuple[object, ...]:
        return self._inner.list_task_records(  # type: ignore[attr-defined,no-any-return]
            include_finished=include_finished,
        )

    async def interrupt(self) -> None:
        self.interrupt_entered.set()
        await self.release_interrupt.wait()
        await self._inner.interrupt()  # type: ignore[attr-defined]

    async def aclose(self) -> None:
        await self._inner.aclose()  # type: ignore[attr-defined]


@dataclass
class _BridgeCall:
    """记录一次 admitted run 的业务坐标。"""

    reservation: DueTaskReservation
    run_id: str
    session_id: str


class _ControlledBridge:
    """可按 run_id 释放的执行体，并把外部 cancel 映射为 CANCELLED。"""

    def __init__(self) -> None:
        self.calls: list[_BridgeCall] = []
        self.cancel_calls: list[tuple[str, str]] = []
        self.started = asyncio.Event()
        self.release_by_run: dict[str, asyncio.Event] = {}

    async def execute_admitted(
        self,
        reservation: DueTaskReservation,
        *,
        run_id: str,
        session_id: str,
        cancel_reason_getter: Callable[[], str | None] | None = None,
        event_context: dict[str, object] | None = None,
        agent_id: str = "",
        on_started: (Callable[[ScheduledTask, ScheduledRun], Awaitable[None]] | None) = None,
    ) -> ScheduledRun:
        del event_context, agent_id
        self.calls.append(_BridgeCall(reservation, run_id, session_id))
        self.started.set()
        if on_started is not None:
            await on_started(
                reservation.task,
                _running_run(
                    reservation,
                    run_id=run_id,
                    session_id=session_id,
                ),
            )
        release = self.release_by_run.setdefault(run_id, asyncio.Event())
        try:
            await release.wait()
            status = RunStatus.COMPLETED
            cancel_reason = None
            result_status = "completed"
        except asyncio.CancelledError:
            status = RunStatus.CANCELLED
            cancel_reason = (
                cancel_reason_getter() if cancel_reason_getter is not None else None
            ) or "user_interrupt"
            result_status = "cancelled"
        return _final_run(
            reservation,
            run_id=run_id,
            session_id=session_id,
            status=status,
            result_status=result_status,
            cancel_reason=cancel_reason,
        )

    def record_skipped_submission(
        self,
        reservation: DueTaskReservation,
        *,
        run_id: str,
        session_id: str,
        reason: str,
        cancel_reason: str,
    ) -> ScheduledRun:
        del reason
        return _final_run(
            reservation,
            run_id=run_id,
            session_id=session_id,
            status=RunStatus.CANCELLED,
            result_status="cancelled",
            cancel_reason=cancel_reason,
        )

    def cancel_admitted_run(
        self,
        reservation: DueTaskReservation,
        *,
        run_id: str,
        reason: str,
    ) -> ScheduledRun | None:
        self.cancel_calls.append((run_id, reason))
        call = next((item for item in self.calls if item.run_id == run_id), None)
        if call is None:
            return None
        return _final_run(
            reservation,
            run_id=run_id,
            session_id=call.session_id,
            status=RunStatus.CANCELLED,
            result_status="cancelled",
            cancel_reason=reason,
        )


def _make_task(
    *,
    task_id: str = "task-scheduled",
    policy: ConcurrencyPolicy,
) -> ScheduledTask:
    """构造绑定稳定 thread 的最小定时任务。"""
    timestamp = "2026-07-31T00:00:00+00:00"
    return ScheduledTask(
        task_id=task_id,
        name=task_id,
        lifecycle=TaskLifecycleState.SCHEDULED,
        origin=TaskOrigin.SYSTEM,
        trigger=ScheduleTrigger(
            trigger_type=TriggerType.CRON,
            expr="* * * * *",
            timezone="UTC",
        ),
        policy=TaskExecutionPolicy(concurrency_policy=policy),
        target=TaskTarget(agent_name="default", input_text=f"run {task_id}"),
        next_run_at=timestamp,
        last_run_at=None,
        created_by="test",
        created_at=timestamp,
        updated_at=timestamp,
        thread_id="thread-aaaaaaaaaaaa",
    )


def _reservation(task: ScheduledTask, reservation_id: str) -> DueTaskReservation:
    """构造指定 reservation identity 的领取结果。"""
    return DueTaskReservation(
        task=task,
        scheduled_for=task.next_run_at or task.created_at,
        reserved_at=task.created_at,
        reservation_id=reservation_id,
    )


def _final_run(
    reservation: DueTaskReservation,
    *,
    run_id: str,
    session_id: str,
    status: RunStatus,
    result_status: str,
    cancel_reason: str | None,
) -> ScheduledRun:
    """把 fake 执行结果映射成完整 ScheduledRun。"""
    return ScheduledRun(
        run_id=run_id,
        task_id=reservation.task.task_id,
        status=status,
        scheduled_for=reservation.scheduled_for,
        started_at=reservation.reserved_at,
        finished_at=reservation.reserved_at,
        session_id=session_id,
        result_status=result_status,
        final_message_excerpt="ok" if status is RunStatus.COMPLETED else None,
        error_message=None,
        failure_reason=None,
        delivery_error=None,
        silent_suppressed=False,
        thread_id=reservation.task.thread_id,
        reservation_id=reservation.reservation_id,
        cancel_reason=cancel_reason,
    )


def _running_run(
    reservation: DueTaskReservation,
    *,
    run_id: str,
    session_id: str,
) -> ScheduledRun:
    """构造生命周期 started 使用的 RUNNING 投影。"""
    return ScheduledRun(
        run_id=run_id,
        task_id=reservation.task.task_id,
        status=RunStatus.RUNNING,
        scheduled_for=reservation.scheduled_for,
        started_at=reservation.reserved_at,
        finished_at=None,
        session_id=session_id,
        result_status=None,
        final_message_excerpt=None,
        error_message=None,
        failure_reason=None,
        delivery_error=None,
        silent_suppressed=False,
        thread_id=reservation.task.thread_id,
        reservation_id=reservation.reservation_id,
    )


@pytest.mark.asyncio
async def test_submit_same_reservation_starts_exactly_once() -> None:
    bridge = _ControlledBridge()
    manager = ScheduledRunManager(
        bridge=bridge,  # type: ignore[arg-type]
        dispatcher_factory=build_scheduled_run_dispatcher_factory(
            _Runtime(),  # type: ignore[arg-type]
        ),
        max_inflight=2,
    )
    reservation = _reservation(
        _make_task(policy=ConcurrencyPolicy.ALLOW),
        "reservation-once",
    )

    first = await manager.submit_scheduled_run(reservation)
    duplicate = await manager.submit_scheduled_run(reservation)
    bridge.release_by_run.setdefault(first.run_id, asyncio.Event()).set()
    final = await manager.wait_for_run(first.run_id)

    assert first.run_id == duplicate.run_id
    assert first.session_id == duplicate.session_id
    assert duplicate.disposition is ScheduledRunSubmitDisposition.DUPLICATE
    assert final.status is RunStatus.COMPLETED
    assert [call.run_id for call in bridge.calls] == [first.run_id]
    await manager.aclose()


@pytest.mark.asyncio
async def test_concurrent_duplicate_and_conflicting_reservation_are_deterministic() -> None:
    bridge = _ControlledBridge()
    manager = ScheduledRunManager(
        bridge=bridge,  # type: ignore[arg-type]
        dispatcher_factory=build_scheduled_run_dispatcher_factory(
            _Runtime(),  # type: ignore[arg-type]
        ),
        max_inflight=2,
    )
    reservation = _reservation(
        _make_task(policy=ConcurrencyPolicy.ALLOW),
        "reservation-concurrent",
    )

    receipts = await asyncio.gather(*[manager.submit_scheduled_run(reservation) for _ in range(3)])
    assert len({receipt.run_id for receipt in receipts}) == 1
    assert len({receipt.session_id for receipt in receipts}) == 1

    conflicting_task = replace(
        reservation.task,
        target=TaskTarget(agent_name="default", input_text="different payload"),
    )
    with pytest.raises(ValueError, match="reservation_id collision"):
        await manager.submit_scheduled_run(
            _reservation(conflicting_task, reservation.reservation_id)
        )
    conflicting_preset = replace(
        reservation.task,
        preset_id="different-preset",
    )
    with pytest.raises(ValueError, match="reservation_id collision"):
        await manager.submit_scheduled_run(
            _reservation(conflicting_preset, reservation.reservation_id)
        )

    run_id = receipts[0].run_id
    bridge.release_by_run.setdefault(run_id, asyncio.Event()).set()
    await manager.wait_for_run(run_id)
    assert [call.run_id for call in bridge.calls] == [run_id]
    await manager.aclose()


@pytest.mark.asyncio
async def test_forbid_back_to_back_submissions_keep_earlier_winner() -> None:
    """两个 owner 都尚未准入时，submission sequence 固定较早 run 获胜。"""
    bridge = _ControlledBridge()
    manager = ScheduledRunManager(
        bridge=bridge,  # type: ignore[arg-type]
        dispatcher_factory=build_scheduled_run_dispatcher_factory(
            _Runtime(),  # type: ignore[arg-type]
        ),
        max_inflight=2,
    )
    task = _make_task(policy=ConcurrencyPolicy.FORBID)

    first = await manager.submit_scheduled_run(_reservation(task, "reservation-back-to-back-a"))
    second = await manager.submit_scheduled_run(_reservation(task, "reservation-back-to-back-b"))
    await bridge.started.wait()
    second_run = await manager.wait_for_run(second.run_id)

    assert second_run.status is RunStatus.CANCELLED
    assert second_run.cancel_reason == "forbid_existing_run"
    assert [call.run_id for call in bridge.calls] == [first.run_id]
    bridge.release_by_run[first.run_id].set()
    assert (await manager.wait_for_run(first.run_id)).status is RunStatus.COMPLETED
    await manager.aclose()


@pytest.mark.asyncio
async def test_forbid_records_second_trigger_without_starting_runner() -> None:
    bridge = _ControlledBridge()
    manager = ScheduledRunManager(
        bridge=bridge,  # type: ignore[arg-type]
        dispatcher_factory=build_scheduled_run_dispatcher_factory(
            _Runtime(),  # type: ignore[arg-type]
        ),
        max_inflight=2,
    )
    task = _make_task(policy=ConcurrencyPolicy.FORBID)
    first = await manager.submit_scheduled_run(_reservation(task, "reservation-a"))
    await bridge.started.wait()
    second = await manager.submit_scheduled_run(_reservation(task, "reservation-b"))
    second_run = await manager.wait_for_run(second.run_id)

    assert second_run.status is RunStatus.CANCELLED
    assert second_run.cancel_reason == "forbid_existing_run"
    assert [call.run_id for call in bridge.calls] == [first.run_id]
    bridge.release_by_run[first.run_id].set()
    await manager.wait_for_run(first.run_id)
    await manager.aclose()


@pytest.mark.asyncio
async def test_manager_max_inflight_owns_pending_runs() -> None:
    bridge = _ControlledBridge()
    manager = ScheduledRunManager(
        bridge=bridge,  # type: ignore[arg-type]
        dispatcher_factory=build_scheduled_run_dispatcher_factory(
            _Runtime(),  # type: ignore[arg-type]
        ),
        max_inflight=2,
    )
    receipts = [
        await manager.submit_scheduled_run(
            _reservation(
                _make_task(
                    task_id=f"task-{index}",
                    policy=ConcurrencyPolicy.ALLOW,
                ),
                f"reservation-{index}",
            )
        )
        for index in range(5)
    ]
    while len(bridge.calls) < 2:
        await asyncio.sleep(0)

    assert len(bridge.calls) == 2
    assert len(manager.live_run_ids("task-4")) == 1

    for receipt in receipts:
        bridge.release_by_run.setdefault(receipt.run_id, asyncio.Event()).set()
    await asyncio.gather(*(manager.wait_for_run(receipt.run_id) for receipt in receipts))
    assert len(bridge.calls) == 5
    await manager.aclose()


@pytest.mark.asyncio
async def test_replace_interrupts_real_registered_run_before_new_start() -> None:
    bridge = _ControlledBridge()
    manager = ScheduledRunManager(
        bridge=bridge,  # type: ignore[arg-type]
        dispatcher_factory=build_scheduled_run_dispatcher_factory(
            _Runtime(),  # type: ignore[arg-type]
        ),
        max_inflight=2,
    )
    task = _make_task(policy=ConcurrencyPolicy.REPLACE)
    first = await manager.submit_scheduled_run(_reservation(task, "reservation-a"))
    await bridge.started.wait()
    second = await manager.submit_scheduled_run(_reservation(task, "reservation-b"))

    first_run = await manager.wait_for_run(first.run_id)
    while len(bridge.calls) < 2:
        await asyncio.sleep(0)
    bridge.release_by_run[second.run_id].set()
    second_run = await manager.wait_for_run(second.run_id)

    assert first_run.status is RunStatus.CANCELLED
    assert first_run.cancel_reason == "replaced_by_new_run"
    assert second_run.status is RunStatus.COMPLETED
    assert [call.run_id for call in bridge.calls] == [first.run_id, second.run_id]
    await manager.aclose()


@pytest.mark.asyncio
async def test_user_cancel_has_distinct_durable_reason() -> None:
    bridge = _ControlledBridge()
    manager = ScheduledRunManager(
        bridge=bridge,  # type: ignore[arg-type]
        dispatcher_factory=build_scheduled_run_dispatcher_factory(
            _Runtime(),  # type: ignore[arg-type]
        ),
        max_inflight=1,
    )
    receipt = await manager.submit_scheduled_run(
        _reservation(
            _make_task(task_id="task-user-cancel", policy=ConcurrencyPolicy.ALLOW),
            "reservation-user-cancel",
        )
    )
    while len(bridge.calls) < 1:
        await asyncio.sleep(0)

    final = await manager.cancel_run(receipt.run_id)

    assert final.status is RunStatus.CANCELLED
    assert final.cancel_reason == "user_interrupt"
    assert bridge.cancel_calls == [(receipt.run_id, "user_interrupt")]
    await manager.aclose()


@pytest.mark.asyncio
async def test_immediate_cancel_before_owner_first_step_publishes_result() -> None:
    """submit 返回后立即取消时，尚未首步执行的 owner 也完整发布 terminal。"""
    bridge = _ControlledBridge()
    manager = ScheduledRunManager(
        bridge=bridge,  # type: ignore[arg-type]
        dispatcher_factory=build_scheduled_run_dispatcher_factory(
            _Runtime(),  # type: ignore[arg-type]
        ),
        max_inflight=1,
    )
    task = _make_task(
        task_id="task-immediate-cancel",
        policy=ConcurrencyPolicy.ALLOW,
    )
    receipt = await manager.submit_scheduled_run(_reservation(task, "reservation-immediate-cancel"))

    async with asyncio.timeout(0.5):
        final = await manager.cancel_run(receipt.run_id)

    assert final.status is RunStatus.CANCELLED
    assert final.cancel_reason == "user_interrupt"
    assert bridge.calls == []
    assert manager.live_run_ids(task.task_id) == ()
    await manager.aclose()


@pytest.mark.asyncio
async def test_finished_notification_cannot_hide_durable_result_or_live_cleanup() -> None:
    """finished sink 阻塞时，业务结果已可读，close 仍持有 FINISHING owner。"""
    bridge = _ControlledBridge()
    lifecycle = _BlockingLifecycleSink()
    manager = ScheduledRunManager(
        bridge=bridge,  # type: ignore[arg-type]
        dispatcher_factory=build_scheduled_run_dispatcher_factory(
            _Runtime(),  # type: ignore[arg-type]
        ),
        max_inflight=1,
        lifecycle_sink=lifecycle,
    )
    task = _make_task(
        task_id="task-blocked-finished",
        policy=ConcurrencyPolicy.ALLOW,
    )
    receipt = await manager.submit_scheduled_run(_reservation(task, "reservation-blocked-finished"))
    while receipt.run_id not in bridge.release_by_run:
        await asyncio.sleep(0)
    bridge.release_by_run[receipt.run_id].set()
    await lifecycle.finished_entered.wait()

    final = await asyncio.wait_for(manager.wait_for_run(receipt.run_id), timeout=0.2)
    assert final.status is RunStatus.COMPLETED
    assert manager.live_run_ids(task.task_id) == ()

    close_task = asyncio.create_task(manager.aclose())
    await asyncio.sleep(0)
    assert not close_task.done()
    lifecycle.release_finished.set()
    await asyncio.wait_for(close_task, timeout=1.0)


@pytest.mark.asyncio
async def test_close_timeout_cancels_blocked_finished_notification() -> None:
    """finished sink 永久阻塞时，Manager 在共享 deadline 内完成关闭。"""
    bridge = _ControlledBridge()
    lifecycle = _BlockingLifecycleSink()
    manager = ScheduledRunManager(
        bridge=bridge,  # type: ignore[arg-type]
        dispatcher_factory=build_scheduled_run_dispatcher_factory(
            _Runtime(),  # type: ignore[arg-type]
        ),
        max_inflight=1,
        shutdown_timeout_seconds=0.02,
        lifecycle_sink=lifecycle,
    )
    task = _make_task(
        task_id="task-close-timeout",
        policy=ConcurrencyPolicy.ALLOW,
    )
    receipt = await manager.submit_scheduled_run(_reservation(task, "reservation-close-timeout"))
    while receipt.run_id not in bridge.release_by_run:
        await asyncio.sleep(0)
    bridge.release_by_run[receipt.run_id].set()
    await lifecycle.finished_entered.wait()

    await asyncio.wait_for(manager.aclose(), timeout=0.2)

    assert (await manager.wait_for_run(receipt.run_id)).status is RunStatus.COMPLETED
    assert manager.live_run_ids(task.task_id) == ()


@pytest.mark.asyncio
async def test_close_timeout_cancels_blocked_interrupt_and_owner() -> None:
    """dispatcher interrupt 永久阻塞时，deadline 后 owner 仍形成 durable terminal。"""
    bridge = _ControlledBridge()
    real_factory = build_scheduled_run_dispatcher_factory(
        _Runtime(),  # type: ignore[arg-type]
    )
    blocking_dispatchers: list[_BlockingInterruptDispatcher] = []

    def dispatcher_factory(**kwargs: object) -> _BlockingInterruptDispatcher:
        inner = real_factory(**kwargs)  # type: ignore[arg-type]
        dispatcher = _BlockingInterruptDispatcher(inner)
        blocking_dispatchers.append(dispatcher)
        return dispatcher

    manager = ScheduledRunManager(
        bridge=bridge,  # type: ignore[arg-type]
        dispatcher_factory=dispatcher_factory,  # type: ignore[arg-type]
        max_inflight=1,
        shutdown_timeout_seconds=0.02,
    )
    task = _make_task(
        task_id="task-blocked-interrupt-timeout",
        policy=ConcurrencyPolicy.ALLOW,
    )
    receipt = await manager.submit_scheduled_run(
        _reservation(task, "reservation-blocked-interrupt-timeout")
    )
    while not bridge.calls:
        await asyncio.sleep(0)

    await asyncio.wait_for(manager.aclose(), timeout=0.2)
    final = await asyncio.wait_for(manager.wait_for_run(receipt.run_id), timeout=0.2)

    assert blocking_dispatchers[0].interrupt_entered.is_set()
    assert final.status is RunStatus.CANCELLED
    assert final.cancel_reason == "scheduler_shutdown"
    assert manager.live_run_ids(task.task_id) == ()


@pytest.mark.asyncio
async def test_user_cancel_reason_wins_when_shutdown_overlaps_interrupt() -> None:
    """用户取消先线性化后，随后 shutdown 保留首个 cancel reason。"""
    bridge = _ControlledBridge()
    real_factory = build_scheduled_run_dispatcher_factory(
        _Runtime(),  # type: ignore[arg-type]
    )
    blocking_dispatchers: list[_BlockingInterruptDispatcher] = []

    def dispatcher_factory(**kwargs: object) -> _BlockingInterruptDispatcher:
        inner = real_factory(**kwargs)  # type: ignore[arg-type]
        dispatcher = _BlockingInterruptDispatcher(inner)
        blocking_dispatchers.append(dispatcher)
        return dispatcher

    manager = ScheduledRunManager(
        bridge=bridge,  # type: ignore[arg-type]
        dispatcher_factory=dispatcher_factory,  # type: ignore[arg-type]
        max_inflight=1,
    )
    receipt = await manager.submit_scheduled_run(
        _reservation(
            _make_task(task_id="task-cancel-close", policy=ConcurrencyPolicy.ALLOW),
            "reservation-cancel-close",
        )
    )
    while not bridge.calls:
        await asyncio.sleep(0)
    dispatcher = blocking_dispatchers[0]

    cancel_task = asyncio.create_task(manager.cancel_run(receipt.run_id))
    await dispatcher.interrupt_entered.wait()
    close_task = asyncio.create_task(manager.aclose())
    await asyncio.sleep(0)
    dispatcher.release_interrupt.set()

    final = await asyncio.wait_for(cancel_task, timeout=1.0)
    await asyncio.wait_for(close_task, timeout=1.0)
    assert final.cancel_reason == "user_interrupt"
    assert bridge.cancel_calls == [
        (receipt.run_id, "user_interrupt"),
        (receipt.run_id, "user_interrupt"),
    ]


@pytest.mark.asyncio
async def test_replace_cancels_same_task_pending_before_it_gets_global_slot() -> None:
    bridge = _ControlledBridge()
    manager = ScheduledRunManager(
        bridge=bridge,  # type: ignore[arg-type]
        dispatcher_factory=build_scheduled_run_dispatcher_factory(
            _Runtime(),  # type: ignore[arg-type]
        ),
        max_inflight=1,
    )
    blocker = await manager.submit_scheduled_run(
        _reservation(
            _make_task(task_id="task-blocker", policy=ConcurrencyPolicy.ALLOW),
            "reservation-blocker",
        )
    )
    while len(bridge.calls) < 1:
        await asyncio.sleep(0)

    replace_task = _make_task(
        task_id="task-replace-pending",
        policy=ConcurrencyPolicy.REPLACE,
    )
    old = await manager.submit_scheduled_run(_reservation(replace_task, "reservation-old"))
    await asyncio.sleep(0)
    new = await manager.submit_scheduled_run(_reservation(replace_task, "reservation-new"))
    old_result = await manager.wait_for_run(old.run_id)

    bridge.release_by_run[blocker.run_id].set()
    await manager.wait_for_run(blocker.run_id)
    while len(bridge.calls) < 2:
        await asyncio.sleep(0)
    bridge.release_by_run[new.run_id].set()
    new_result = await manager.wait_for_run(new.run_id)

    assert old_result.status is RunStatus.CANCELLED
    assert old_result.cancel_reason == "replaced_by_new_run"
    assert [call.run_id for call in bridge.calls] == [blocker.run_id, new.run_id]
    assert new_result.status is RunStatus.COMPLETED
    await manager.aclose()


@pytest.mark.asyncio
async def test_allow_keeps_independent_fresh_sessions() -> None:
    bridge = _ControlledBridge()
    manager = ScheduledRunManager(
        bridge=bridge,  # type: ignore[arg-type]
        dispatcher_factory=build_scheduled_run_dispatcher_factory(
            _Runtime(),  # type: ignore[arg-type]
        ),
        max_inflight=2,
    )
    task = _make_task(policy=ConcurrencyPolicy.ALLOW)
    first = await manager.submit_scheduled_run(_reservation(task, "reservation-a"))
    second = await manager.submit_scheduled_run(_reservation(task, "reservation-b"))
    while len(bridge.calls) < 2:
        await asyncio.sleep(0)
    bridge.release_by_run[first.run_id].set()
    bridge.release_by_run[second.run_id].set()

    await manager.wait_for_run(first.run_id)
    await manager.wait_for_run(second.run_id)

    assert first.run_id != second.run_id
    assert first.session_id != second.session_id
    assert manager.live_run_ids(task.task_id) == ()
    await manager.aclose()


@pytest.mark.asyncio
async def test_close_collects_running_and_pending_and_rejects_new_submissions() -> None:
    bridge = _ControlledBridge()
    manager = ScheduledRunManager(
        bridge=bridge,  # type: ignore[arg-type]
        dispatcher_factory=build_scheduled_run_dispatcher_factory(
            _Runtime(),  # type: ignore[arg-type]
        ),
        max_inflight=1,
    )
    running = await manager.submit_scheduled_run(
        _reservation(
            _make_task(task_id="task-running", policy=ConcurrencyPolicy.ALLOW),
            "reservation-running",
        )
    )
    while len(bridge.calls) < 1:
        await asyncio.sleep(0)
    pending = await manager.submit_scheduled_run(
        _reservation(
            _make_task(task_id="task-pending", policy=ConcurrencyPolicy.ALLOW),
            "reservation-pending",
        )
    )

    await asyncio.wait_for(manager.aclose(), timeout=2.0)
    running_result = await manager.wait_for_run(running.run_id)
    pending_result = await manager.wait_for_run(pending.run_id)

    assert running_result.status is RunStatus.CANCELLED
    assert running_result.cancel_reason == "scheduler_shutdown"
    assert pending_result.status is RunStatus.CANCELLED
    assert pending_result.cancel_reason == "scheduler_shutdown"
    assert manager.live_run_ids("task-running") == ()
    assert manager.live_run_ids("task-pending") == ()
    with pytest.raises(RuntimeError, match="closed"):
        await manager.submit_scheduled_run(
            _reservation(
                _make_task(task_id="task-new", policy=ConcurrencyPolicy.ALLOW),
                "reservation-new",
            )
        )


@pytest.mark.asyncio
async def test_finished_idempotency_cells_are_pruned_to_fixed_limit() -> None:
    bridge = _ControlledBridge()
    manager = ScheduledRunManager(
        bridge=bridge,  # type: ignore[arg-type]
        dispatcher_factory=build_scheduled_run_dispatcher_factory(
            _Runtime(),  # type: ignore[arg-type]
        ),
        max_inflight=1,
        max_retained_runs=2,
    )
    receipts = []
    for index in range(3):
        receipt = await manager.submit_scheduled_run(
            _reservation(
                _make_task(
                    task_id=f"task-retention-{index}",
                    policy=ConcurrencyPolicy.ALLOW,
                ),
                f"reservation-retention-{index}",
            )
        )
        receipts.append(receipt)
        while receipt.run_id not in bridge.release_by_run:
            await asyncio.sleep(0)
        bridge.release_by_run[receipt.run_id].set()
        await manager.wait_for_run(receipt.run_id)
        await asyncio.sleep(0)

    with pytest.raises(KeyError):
        await manager.wait_for_run(receipts[0].run_id)
    assert (await manager.wait_for_run(receipts[1].run_id)).status is RunStatus.COMPLETED
    assert (await manager.wait_for_run(receipts[2].run_id)).status is RunStatus.COMPLETED
    assert manager._task_locks == {}
    await manager.aclose()


@pytest.mark.asyncio
async def test_active_cells_do_not_consume_finished_idempotency_retention() -> None:
    """活跃 run 超过 cap 时，最近完成 reservation 仍保持幂等回执。"""
    bridge = _ControlledBridge()
    manager = ScheduledRunManager(
        bridge=bridge,  # type: ignore[arg-type]
        dispatcher_factory=build_scheduled_run_dispatcher_factory(
            _Runtime(),  # type: ignore[arg-type]
        ),
        max_inflight=3,
        max_retained_runs=1,
    )
    active_receipts = [
        await manager.submit_scheduled_run(
            _reservation(
                _make_task(
                    task_id=f"task-retention-active-{index}",
                    policy=ConcurrencyPolicy.ALLOW,
                ),
                f"reservation-retention-active-{index}",
            )
        )
        for index in range(2)
    ]
    completed_reservation = _reservation(
        _make_task(
            task_id="task-retention-completed",
            policy=ConcurrencyPolicy.ALLOW,
        ),
        "reservation-retention-completed",
    )
    completed = await manager.submit_scheduled_run(completed_reservation)
    while completed.run_id not in bridge.release_by_run:
        await asyncio.sleep(0)
    bridge.release_by_run[completed.run_id].set()
    await manager.wait_for_run(completed.run_id)
    await asyncio.sleep(0)

    duplicate = await manager.submit_scheduled_run(completed_reservation)

    assert duplicate.disposition is ScheduledRunSubmitDisposition.DUPLICATE
    assert duplicate.run_id == completed.run_id
    for receipt in active_receipts:
        bridge.release_by_run[receipt.run_id].set()
        await manager.wait_for_run(receipt.run_id)
    await manager.aclose()
