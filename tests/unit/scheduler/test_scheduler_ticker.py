"""ticker reserve+submit 单一职责测试。

验证 ticker 只领取 reservation 并调用 ScheduledRunManager 门户；live Task、
并发限制、取消和 shutdown 均不由本模块持有。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from scheduler.domain import (
    DueTaskReservation,
    ScheduledTask,
    ScheduleTrigger,
    TaskExecutionPolicy,
    TaskLifecycleState,
    TaskOrigin,
    TaskTarget,
    TriggerType,
)
from scheduler.ticker import run_ticker_loop, tick


def _task(task_id: str) -> ScheduledTask:
    """构造最小 due task。"""
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
        policy=TaskExecutionPolicy(),
        target=TaskTarget(agent_name="default", input_text="run"),
        next_run_at=timestamp,
        last_run_at=None,
        created_by="test",
        created_at=timestamp,
        updated_at=timestamp,
    )


def _reservation(task_id: str) -> DueTaskReservation:
    """构造带显式 identity 的 reservation。"""
    task = _task(task_id)
    return DueTaskReservation(
        task=task,
        scheduled_for=task.next_run_at or task.created_at,
        reserved_at=task.created_at,
        reservation_id=f"reservation-{task_id}",
    )


@dataclass
class _Store:
    """ticker 所需 Store 门户 fake。"""

    reservations: list[DueTaskReservation] = field(default_factory=list)
    reserve_error: Exception | None = None
    reserve_calls: list[str] = field(default_factory=list)
    ticker_statuses: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    audits: list[dict[str, object]] = field(default_factory=list)
    incidents: list[dict[str, object]] = field(default_factory=list)

    def reserve_due_tasks(self, *, now: str) -> list[DueTaskReservation]:
        self.reserve_calls.append(now)
        if self.reserve_error is not None:
            raise self.reserve_error
        reservations = list(self.reservations)
        self.reservations.clear()
        return reservations

    def write_ticker_status(self, *, status: str, payload: dict[str, object]) -> None:
        self.ticker_statuses.append((status, payload))

    def append_audit(self, **payload: object) -> None:
        self.audits.append(payload)

    def append_incident(self, **payload: object) -> None:
        self.incidents.append(payload)


@dataclass
class _Submitter:
    """记录 submit 调用，可按 task id 注入失败。"""

    fail_task_ids: set[str] = field(default_factory=set)
    calls: list[DueTaskReservation] = field(default_factory=list)

    async def submit_scheduled_run(self, reservation: DueTaskReservation) -> object:
        self.calls.append(reservation)
        if reservation.task.task_id in self.fail_task_ids:
            raise RuntimeError(f"submit failed: {reservation.task.task_id}")
        return object()


@pytest.mark.asyncio
async def test_tick_empty_store_writes_ok_status() -> None:
    store = _Store()
    submitter = _Submitter()

    stats = await tick(store, submitter, now="2026-07-31T00:00:00+00:00")  # type: ignore[arg-type]

    assert stats == {"due_count": 0, "spawned": 0}
    assert submitter.calls == []
    assert store.ticker_statuses[0][0] == "ok"


@pytest.mark.asyncio
async def test_tick_submits_each_reservation_without_creating_live_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reservations = [_reservation("task-a"), _reservation("task-b")]
    store = _Store(reservations=list(reservations))
    submitter = _Submitter()

    def _unexpected_create_task(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("ticker must not create asyncio Task")

    monkeypatch.setattr(asyncio, "create_task", _unexpected_create_task)
    stats = await tick(store, submitter)  # type: ignore[arg-type]

    assert stats == {"due_count": 2, "spawned": 2}
    assert submitter.calls == reservations
    assert store.audits[0]["action"] == "tick_dispatched"


@pytest.mark.asyncio
async def test_tick_submit_failure_isolated_and_next_reservation_continues() -> None:
    store = _Store(
        reservations=[
            _reservation("task-a"),
            _reservation("task-b"),
            _reservation("task-c"),
        ]
    )
    submitter = _Submitter(fail_task_ids={"task-b"})

    stats = await tick(store, submitter)  # type: ignore[arg-type]

    assert stats == {"due_count": 3, "spawned": 2}
    assert [call.task.task_id for call in submitter.calls] == [
        "task-a",
        "task-b",
        "task-c",
    ]
    assert store.incidents[0]["action"] == "tick_submit_error"
    incident_payload = store.incidents[0]["payload"]
    assert isinstance(incident_payload, dict)
    assert incident_payload["reservation_id"] == "reservation-task-b"


@pytest.mark.asyncio
async def test_tick_reserve_failure_records_error_without_submit() -> None:
    store = _Store(reserve_error=RuntimeError("locked"))
    submitter = _Submitter()

    stats = await tick(store, submitter)  # type: ignore[arg-type]

    assert stats == {"due_count": 0, "spawned": 0}
    assert submitter.calls == []
    assert store.ticker_statuses[0][0] == "error"
    assert store.incidents[0]["action"] == "tick_failed"


@pytest.mark.asyncio
async def test_tick_explicit_now_is_reservation_clock() -> None:
    store = _Store()

    await tick(
        store,  # type: ignore[arg-type]
        _Submitter(),
        now="2026-07-31T12:34:56+00:00",
    )

    assert store.reserve_calls == ["2026-07-31T12:34:56+00:00"]


@pytest.mark.asyncio
async def test_run_ticker_loop_repeats_until_stop() -> None:
    store = _Store()
    submitter = _Submitter()
    stop_event = asyncio.Event()

    async def _stop_after_ticks() -> None:
        while len(store.reserve_calls) < 3:
            await asyncio.sleep(0)
        stop_event.set()

    stopper = asyncio.create_task(_stop_after_ticks())
    await run_ticker_loop(
        store,  # type: ignore[arg-type]
        submitter,
        stop_event,
        interval=0.001,
    )
    await stopper

    assert len(store.reserve_calls) >= 3


@pytest.mark.asyncio
async def test_run_ticker_loop_pre_stopped_does_not_scan() -> None:
    store = _Store()
    stop_event = asyncio.Event()
    stop_event.set()

    await run_ticker_loop(
        store,  # type: ignore[arg-type]
        _Submitter(),
        stop_event,
        interval=0.001,
    )

    assert store.reserve_calls == []


@pytest.mark.asyncio
async def test_run_ticker_loop_cancelled_error_propagates() -> None:
    store = _Store()
    stop_event = asyncio.Event()
    loop_task = asyncio.create_task(
        run_ticker_loop(
            store,  # type: ignore[arg-type]
            _Submitter(),
            stop_event,
            interval=10.0,
        )
    )
    await asyncio.sleep(0)
    loop_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await loop_task
