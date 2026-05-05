"""unit：scheduler.ticker（v0.2 — 1s tick + Semaphore + 不阻塞 fire）。

覆盖矩阵：

§1 :func:`tick` 单次扫描（v0.2 接口：返回 ``due_count`` + ``spawned``，
异步执行，单任务异常落到 audit ``tick_task_error``）

- 空 store → due=0/spawned=0；audit 写 tick_started + tick_finished
- 1 个 due → bridge.execute 调一次（异步）
- 3 个 due，第 2 个抛异常 → 1/3 都被调用，audit 含 tick_task_error
- bridge.execute 超时（mock policy.inactivity_timeout_seconds 极小）→ 超时
  落到 audit ``tick_task_error``
- store.reserve_due_tasks 抛异常 → 写 tick_failed，返回 spawned=0，不写 tick_finished
- 单任务失败时 audit 含 tick_task_error
- tick_finished audit 含 due_count + spawned
- 显式 ``now`` 写 audit

§2 :func:`run_ticker_loop` 后台循环

- 跑 2-3 次后 stop_event.set() → 自然退出
- tick 抛异常时不退出循环（仍调下一次）
- CancelledError 透传
- 启动前 stop_event 已 set → 立即退出
- Semaphore 限流：max_inflight=2，10 个 due → in-flight 同时最多 2
- create_task 不阻塞：bridge 单任务跑 0.5s，2 个 due，tick 返回耗时 < 0.1s
- 优雅退出：stop_event.set() 后 in-flight 任务能跑完
- shutdown_timeout 兜底：模拟 in-flight 死循环，shutdown_timeout 极小，任务被 cancel
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scheduler.domain import (
    ConcurrencyPolicy,
    DueTaskReservation,
    MisfirePolicy,
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
from scheduler.ticker import run_ticker_loop, tick
from scheduler.timing import to_iso, utc_now

# ---------------------------------------------------------------------------
# 测试 stub
# ---------------------------------------------------------------------------


def _make_task(*, task_id: str = "task-tick0001", inactivity_timeout: int = 600) -> ScheduledTask:
    now = to_iso(utc_now())
    return ScheduledTask(
        task_id=task_id,
        name=f"name-{task_id}",
        enabled=True,
        state=TaskState.SCHEDULED,
        origin=TaskOrigin.CLI,
        trigger=ScheduleTrigger(
            trigger_type=TriggerType.ONCE,
            expr=now,
            timezone="UTC",
        ),
        policy=TaskExecutionPolicy(
            session_mode=SessionMode.FRESH_SESSION,
            concurrency_policy=ConcurrencyPolicy.FORBID,
            misfire_policy=MisfirePolicy.SKIP,
            max_turns=None,
            inactivity_timeout_seconds=inactivity_timeout,
            wall_timeout_seconds=None,
            retry_limit=0,
            silent_marker_enabled=True,
        ),
        target=TaskTarget(agent_name="default", input_text="hi", metadata={}),
        next_run_at=now,
        last_run_at=None,
        created_by="test",
        created_at=now,
        updated_at=now,
    )


def _make_reservation(task: ScheduledTask) -> DueTaskReservation:
    now = to_iso(utc_now())
    return DueTaskReservation(task=task, scheduled_for=now, reserved_at=now)


@dataclass
class _StubBridge:
    """记录每次调用的 reservation；可控制 raise / sleep / 测限流并发上限。"""

    error_indices: tuple[int, ...] = ()
    sleep_seconds: float | None = None
    track_concurrency: bool = False

    def __post_init__(self) -> None:
        self.calls: list[str] = []
        self.completed: list[str] = []
        self._inflight = 0
        self.max_inflight_observed = 0

    async def execute(self, reservation: DueTaskReservation) -> object:
        idx = len(self.calls)
        self.calls.append(reservation.task.task_id)
        if self.track_concurrency:
            self._inflight += 1
            if self._inflight > self.max_inflight_observed:
                self.max_inflight_observed = self._inflight
        try:
            if self.sleep_seconds is not None:
                await asyncio.sleep(self.sleep_seconds)
            if idx in self.error_indices:
                raise RuntimeError(f"stub bridge boom @ idx={idx}")
            self.completed.append(reservation.task.task_id)
            return object()
        finally:
            if self.track_concurrency:
                self._inflight -= 1


@dataclass
class _SlowBridge:
    """每个任务 await 一个独立 Event；测试自己 set() 决定何时完成。"""

    completed: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    release: asyncio.Event | None = None

    async def execute(self, reservation: DueTaskReservation) -> object:
        try:
            if self.release is not None:
                await self.release.wait()
            self.completed.append(reservation.task.task_id)
            return object()
        except asyncio.CancelledError:
            self.cancelled.append(reservation.task.task_id)
            raise


class _RealStoreWithReservations:
    """复用真 :class:`Store` 的 audit / lock，但 ``reserve_due_tasks`` 可控。"""

    def __init__(self, base: Store, reservations: list[DueTaskReservation]) -> None:
        self._base = base
        self._reservations = reservations
        self.reserve_calls = 0
        self.reserve_should_raise: Exception | None = None

    def reserve_due_tasks(self, *, now: str) -> list[DueTaskReservation]:
        self.reserve_calls += 1
        if self.reserve_should_raise is not None:
            raise self.reserve_should_raise
        return list(self._reservations)

    def append_audit(self, **kwargs: object) -> None:
        self._base.append_audit(**kwargs)  # type: ignore[arg-type]

    def list_audits(self, **kwargs: object) -> list[dict[str, object]]:
        return self._base.list_audits(**kwargs)  # type: ignore[arg-type, return-value]


async def _wait_all(tasks: set[asyncio.Task[None]]) -> None:
    """等 in-flight 集合里所有 task 跑完（吞异常）。"""
    if tasks:
        await asyncio.gather(*list(tasks), return_exceptions=True)


# ---------------------------------------------------------------------------
# §1 tick — 单次扫描
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_empty_returns_zero_and_writes_started_finished(tmp_path: Path) -> None:
    base = Store(tmp_path)
    store = _RealStoreWithReservations(base, reservations=[])
    bridge = _StubBridge()

    stats = await tick(store, bridge)  # type: ignore[arg-type]

    assert stats == {"due_count": 0, "spawned": 0}
    assert bridge.calls == []
    actions = [a["action"] for a in base.list_audits()]
    assert "tick_started" in actions
    assert "tick_finished" in actions


@pytest.mark.asyncio
async def test_tick_one_due_runs_bridge_once(tmp_path: Path) -> None:
    base = Store(tmp_path)
    task = _make_task(task_id="task-only0001")
    store = _RealStoreWithReservations(base, reservations=[_make_reservation(task)])
    bridge = _StubBridge()
    inflight: set[asyncio.Task[None]] = set()

    stats = await tick(store, bridge, inflight=inflight)  # type: ignore[arg-type]

    assert stats == {"due_count": 1, "spawned": 1}
    # 等 in-flight 异步任务跑完
    await _wait_all(inflight)
    assert bridge.calls == ["task-only0001"]
    assert bridge.completed == ["task-only0001"]


@pytest.mark.asyncio
async def test_tick_three_due_second_raises_does_not_block_others(tmp_path: Path) -> None:
    base = Store(tmp_path)
    reservations = [
        _make_reservation(_make_task(task_id="task-mid0001")),
        _make_reservation(_make_task(task_id="task-mid0002")),
        _make_reservation(_make_task(task_id="task-mid0003")),
    ]
    store = _RealStoreWithReservations(base, reservations=reservations)
    bridge = _StubBridge(error_indices=(1,))
    inflight: set[asyncio.Task[None]] = set()

    stats = await tick(store, bridge, inflight=inflight)  # type: ignore[arg-type]

    assert stats == {"due_count": 3, "spawned": 3}
    await _wait_all(inflight)

    # 关键：1/2/3 都被调用了（异常不阻塞其它任务）
    assert sorted(bridge.calls) == ["task-mid0001", "task-mid0002", "task-mid0003"]
    # 1/3 跑完，2 抛异常没 completed
    assert sorted(bridge.completed) == ["task-mid0001", "task-mid0003"]
    # audit 含一条 tick_task_error
    err_audits = [a for a in base.list_audits() if a["action"] == "tick_task_error"]
    assert len(err_audits) == 1
    assert err_audits[0]["task_id"] == "task-mid0002"


@pytest.mark.asyncio
async def test_tick_bridge_execute_timeout_marks_failed(tmp_path: Path) -> None:
    base = Store(tmp_path)
    # inactivity_timeout=1 → outer_timeout=2s；让 bridge sleep 5s 触发 wait_for 超时
    task = _make_task(task_id="task-slow0001", inactivity_timeout=1)
    store = _RealStoreWithReservations(base, reservations=[_make_reservation(task)])
    bridge = _StubBridge(sleep_seconds=5.0)
    inflight: set[asyncio.Task[None]] = set()

    stats = await tick(store, bridge, inflight=inflight)  # type: ignore[arg-type]

    assert stats == {"due_count": 1, "spawned": 1}
    # 等 in-flight 完成（包括 wait_for 超时被吞）
    await _wait_all(inflight)
    err_audits = [a for a in base.list_audits() if a["action"] == "tick_task_error"]
    assert len(err_audits) == 1
    assert err_audits[0]["task_id"] == "task-slow0001"


@pytest.mark.asyncio
async def test_tick_reserve_raises_writes_tick_failed_and_returns(tmp_path: Path) -> None:
    base = Store(tmp_path)
    store = _RealStoreWithReservations(base, reservations=[])
    store.reserve_should_raise = RuntimeError("reserve boom")
    bridge = _StubBridge()

    stats = await tick(store, bridge)  # type: ignore[arg-type]

    assert stats == {"due_count": 0, "spawned": 0}
    assert bridge.calls == []
    actions = [a["action"] for a in base.list_audits()]
    assert "tick_started" in actions
    assert "tick_failed" in actions
    # tick_failed 已经收尾，不应再写 tick_finished（避免误导）
    assert "tick_finished" not in actions


@pytest.mark.asyncio
async def test_tick_finished_audit_records_counts(tmp_path: Path) -> None:
    base = Store(tmp_path)
    reservations = [
        _make_reservation(_make_task(task_id="task-cnt0001")),
        _make_reservation(_make_task(task_id="task-cnt0002")),
    ]
    store = _RealStoreWithReservations(base, reservations=reservations)
    bridge = _StubBridge(error_indices=(0,))
    inflight: set[asyncio.Task[None]] = set()

    await tick(store, bridge, inflight=inflight)  # type: ignore[arg-type]

    finished = next(a for a in base.list_audits() if a["action"] == "tick_finished")
    payload = finished["payload"]
    assert payload["due_count"] == 2
    assert payload["spawned"] == 2

    # 等任务跑完后再断言 audit；执行结果异步落 tick_task_error
    await _wait_all(inflight)
    err_audits = [a for a in base.list_audits() if a["action"] == "tick_task_error"]
    assert len(err_audits) == 1


@pytest.mark.asyncio
async def test_tick_uses_explicit_now_in_audit(tmp_path: Path) -> None:
    base = Store(tmp_path)
    store = _RealStoreWithReservations(base, reservations=[])
    bridge = _StubBridge()
    pinned = "2099-12-31T23:59:59+00:00"

    await tick(store, bridge, now=pinned)  # type: ignore[arg-type]

    started = next(a for a in base.list_audits() if a["action"] == "tick_started")
    assert started["payload"]["tick_at"] == pinned


# ---------------------------------------------------------------------------
# §2 run_ticker_loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_stops_when_event_set_and_runs_at_least_once(tmp_path: Path) -> None:
    base = Store(tmp_path)
    store = _RealStoreWithReservations(base, reservations=[])
    bridge = _StubBridge()
    stop_event = asyncio.Event()

    async def _stop_after(delay: float) -> None:
        await asyncio.sleep(delay)
        stop_event.set()

    # interval=0.05s；0.18s 后 stop → 至少能跑 2-3 次
    stopper = asyncio.create_task(_stop_after(0.18))
    await run_ticker_loop(store, bridge, stop_event, interval=0.05)  # type: ignore[arg-type]
    await stopper

    # store.reserve_calls 至少 2 次
    assert store.reserve_calls >= 2


@pytest.mark.asyncio
async def test_loop_continues_when_tick_raises(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """tick 内部抛裸异常时 loop 不退出，仍能跑下一次。"""
    base = Store(tmp_path)
    store = _RealStoreWithReservations(base, reservations=[])
    bridge = _StubBridge()
    stop_event = asyncio.Event()

    call_count = {"n": 0}

    async def fake_tick(_store, _bridge, *, now=None, semaphore=None, inflight=None):  # type: ignore[no-untyped-def]
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("first tick boom")
        # 第二次 tick 设置 stop_event 让 loop 退出
        stop_event.set()
        return {"due_count": 0, "spawned": 0}

    import scheduler.ticker as ticker_mod

    monkeypatch.setattr(ticker_mod, "tick", fake_tick)

    await run_ticker_loop(store, bridge, stop_event, interval=0.01)  # type: ignore[arg-type]

    assert call_count["n"] >= 2  # 第一次失败后第二次还跑了


@pytest.mark.asyncio
async def test_loop_propagates_cancelled_error(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """tick 抛 CancelledError 时 loop 透传不吞。"""
    base = Store(tmp_path)
    store = _RealStoreWithReservations(base, reservations=[])
    bridge = _StubBridge()
    stop_event = asyncio.Event()

    async def fake_tick(_store, _bridge, *, now=None, semaphore=None, inflight=None):  # type: ignore[no-untyped-def]
        raise asyncio.CancelledError()

    import scheduler.ticker as ticker_mod

    monkeypatch.setattr(ticker_mod, "tick", fake_tick)

    with pytest.raises(asyncio.CancelledError):
        await run_ticker_loop(store, bridge, stop_event, interval=10.0)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_loop_stops_immediately_if_event_already_set(tmp_path: Path) -> None:
    """启动前 stop_event 已 set → 立即退出，不跑任何 tick。"""
    base = Store(tmp_path)
    store = _RealStoreWithReservations(base, reservations=[])
    bridge = _StubBridge()
    stop_event = asyncio.Event()
    stop_event.set()

    await run_ticker_loop(store, bridge, stop_event, interval=10.0)  # type: ignore[arg-type]

    assert store.reserve_calls == 0


# ---------------------------------------------------------------------------
# §3 v0.2 新增：Semaphore 限流 + create_task 不阻塞 + 优雅退出
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_create_task_does_not_block(tmp_path: Path) -> None:
    """create_task 不阻塞：bridge 单任务跑 0.5s，2 个 due，tick 返回耗时 < 0.1s。

    证明 v0.2 不再串行 await，而是 fire-and-forget。
    """
    base = Store(tmp_path)
    reservations = [
        _make_reservation(_make_task(task_id="task-fast0001")),
        _make_reservation(_make_task(task_id="task-fast0002")),
    ]
    store = _RealStoreWithReservations(base, reservations=reservations)
    bridge = _StubBridge(sleep_seconds=0.5)
    inflight: set[asyncio.Task[None]] = set()

    started = time.monotonic()
    stats = await tick(store, bridge, inflight=inflight)  # type: ignore[arg-type]
    elapsed = time.monotonic() - started

    assert stats == {"due_count": 2, "spawned": 2}
    # tick 本身不应等任何 bridge.execute 完成；< 0.1s 比 0.5s 单任务时长小很多
    assert elapsed < 0.1, f"tick should not block on bridge.execute; took {elapsed:.3f}s"
    # in-flight 还在跑
    assert any(not t.done() for t in inflight)

    # 等所有任务跑完
    await _wait_all(inflight)
    assert sorted(bridge.completed) == ["task-fast0001", "task-fast0002"]


@pytest.mark.asyncio
async def test_tick_semaphore_limits_concurrent_inflight(tmp_path: Path) -> None:
    """Semaphore 限流：max_inflight=2，10 个 due 同时 fire → in-flight 最多 2。"""
    base = Store(tmp_path)
    reservations = [_make_reservation(_make_task(task_id=f"task-sem{i:04d}")) for i in range(10)]
    store = _RealStoreWithReservations(base, reservations=reservations)
    # 每个任务 sleep 50ms，给 Semaphore 足够时间观察并发
    bridge = _StubBridge(sleep_seconds=0.05, track_concurrency=True)
    inflight: set[asyncio.Task[None]] = set()
    semaphore = asyncio.Semaphore(2)

    stats = await tick(store, bridge, semaphore=semaphore, inflight=inflight)  # type: ignore[arg-type]
    assert stats == {"due_count": 10, "spawned": 10}

    # 等所有任务跑完
    await _wait_all(inflight)
    assert len(bridge.completed) == 10
    # 关键断言：观察到的最大并发不超过 Semaphore 上限
    assert bridge.max_inflight_observed <= 2, (
        f"semaphore violated: max_inflight_observed={bridge.max_inflight_observed}"
    )
    assert bridge.max_inflight_observed >= 1


@pytest.mark.asyncio
async def test_run_ticker_loop_graceful_shutdown_waits_for_inflight(tmp_path: Path) -> None:
    """优雅退出：stop_event.set() 后，in-flight 任务能正常跑完。"""
    base = Store(tmp_path)
    release = asyncio.Event()
    bridge = _SlowBridge(release=release)
    reservations = [
        _make_reservation(_make_task(task_id="task-grace0001")),
        _make_reservation(_make_task(task_id="task-grace0002")),
    ]

    # 让 store 第一次 tick 返回 2 个 due，之后返回空（避免持续 fire）
    fired = {"first": False}

    class _OneShotStore(_RealStoreWithReservations):
        def reserve_due_tasks(self, *, now: str) -> list[DueTaskReservation]:
            self.reserve_calls += 1
            if not fired["first"]:
                fired["first"] = True
                return list(reservations)
            return []

    store = _OneShotStore(base, reservations=[])
    stop_event = asyncio.Event()

    async def _drive() -> None:
        # 等第一次 tick 把任务 fire 出去（in-flight 中），再 set stop
        await asyncio.sleep(0.05)
        stop_event.set()
        # 短暂延迟后释放 in-flight，让它们能在 shutdown_timeout 内跑完
        await asyncio.sleep(0.05)
        release.set()

    driver = asyncio.create_task(_drive())
    await run_ticker_loop(  # type: ignore[arg-type]
        store,
        bridge,
        stop_event,
        interval=0.02,
        max_inflight=8,
        shutdown_timeout=2.0,
    )
    await driver

    # 关键断言：所有 in-flight 任务都跑完了，没有被强制 cancel
    assert sorted(bridge.completed) == ["task-grace0001", "task-grace0002"]
    assert bridge.cancelled == []


@pytest.mark.asyncio
async def test_run_ticker_loop_shutdown_timeout_cancels_inflight(tmp_path: Path) -> None:
    """shutdown_timeout 兜底：in-flight 死循环时，超时后任务被 cancel。"""
    base = Store(tmp_path)
    # release 永远不 set → bridge.execute 永远不返回
    release = asyncio.Event()
    bridge = _SlowBridge(release=release)
    reservations = [
        _make_reservation(_make_task(task_id="task-stuck0001")),
    ]

    fired = {"first": False}

    class _OneShotStore(_RealStoreWithReservations):
        def reserve_due_tasks(self, *, now: str) -> list[DueTaskReservation]:
            self.reserve_calls += 1
            if not fired["first"]:
                fired["first"] = True
                return list(reservations)
            return []

    store = _OneShotStore(base, reservations=[])
    stop_event = asyncio.Event()

    async def _stop_after_first_tick() -> None:
        await asyncio.sleep(0.08)
        stop_event.set()

    stopper = asyncio.create_task(_stop_after_first_tick())
    started = time.monotonic()
    await run_ticker_loop(  # type: ignore[arg-type]
        store,
        bridge,
        stop_event,
        interval=0.02,
        max_inflight=8,
        shutdown_timeout=0.1,  # 极短，强制走 cancel 分支
    )
    elapsed = time.monotonic() - started
    await stopper

    # 关键断言：任务被 cancel；run_ticker_loop 整体耗时不应远超 shutdown_timeout + 一两次 interval
    assert bridge.cancelled == ["task-stuck0001"]
    assert bridge.completed == []
    assert elapsed < 1.0, f"shutdown took too long: {elapsed:.3f}s"
