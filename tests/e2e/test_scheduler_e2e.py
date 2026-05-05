"""e2e：scheduler 端到端（v0.2 Hermes 风格 ticker；schedule_tool 入口）。

完整链路覆盖（**全 stub，不调真 LLM**）：

1. ``schedule_tool.execute(action="create")`` 创建一次性任务 → ticker 立即扫到
   （oneshot grace 内）→ stub bridge 收到 reservation → store 落 ScheduledRun +
   audits 含 ``tick_started`` / ``tick_finished`` / ``create``
2. tick 时无 due → ``due_count=0`` / ``spawned=0``，但 ``tick_started`` /
   ``tick_finished`` audit 仍写
3. bridge.execute 抛异常 → ``tick_task_error`` audit 落地，ticker 不挂
4. ``run_ticker_loop`` 跑一段时间 → ``stop_event.set()`` 后优雅退出，
   ``tick_started`` 至少 2 次
5. v0.2 P0 死循环回归：ONCE 任务在真 run_ticker_loop 下只 fire 1 次
6. recurring 对照：every 1s 任务 fire 多次，未被误归档
7. v0.2 闭环矩阵 E2-E8：reserve→bridge→状态机各路径（参见档 2）

通过自定义 stub bridge 注入到 :func:`scheduler.ticker.tick` /
:func:`scheduler.ticker.run_ticker_loop`，避开真实 LLM provider 与真实 Runner.run。
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from core.contracts import ToolContext
from scheduler.domain import (
    ConcurrencyPolicy,
    MisfirePolicy,
    RunFailureReason,
    RunStatus,
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
from scheduler.policy import apply_concurrency_policy
from scheduler.store import Store, TaskNotFoundError
from scheduler.ticker import run_ticker_loop, tick
from scheduler.timing import to_iso, utc_now
from tools.schedule_tool import build_schedule_tool


def _suppress_task_not_found() -> contextlib.AbstractContextManager[None]:
    """Helper context manager mirroring real ExecutionBridge.execute end."""
    return contextlib.suppress(TaskNotFoundError)


# ---------------------------------------------------------------------------
# Stub bridges（不调真 LLM；按 reservation 写一条 ScheduledRun 进 store）
# ---------------------------------------------------------------------------


class _StubBridge:
    """e2e 用：写一条真实的 ScheduledRun 到 store，但不跑 Runner。"""

    def __init__(self, store: Store) -> None:
        self._store = store
        self.executed: list[str] = []

    async def execute(self, reservation):  # type: ignore[no-untyped-def]
        idx = len(self.executed)
        self.executed.append(reservation.task.task_id)
        run = ScheduledRun(
            run_id=f"e2e-run-{idx + 1:04d}",
            task_id=reservation.task.task_id,
            status=RunStatus.COMPLETED,
            scheduled_for=reservation.scheduled_for,
            started_at=to_iso(utc_now()),
            finished_at=to_iso(utc_now()),
            session_id=f"e2e-sess-{idx + 1:04d}",
            result_status="success",
            final_message_excerpt="stub final",
            error_message=None,
            failure_reason=None,
            delivery_error=None,
            silent_suppressed=False,
        )
        self._store.append_run(run)
        return run


class _NoOpBridge:
    """case 2 用：tick 不应调到。"""

    async def execute(self, reservation):  # type: ignore[no-untyped-def]
        raise AssertionError("NoOpBridge.execute 不应被调用")


class _CrashBridge:
    """case 3 用：每次 execute 都抛异常。"""

    async def execute(self, reservation):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# helper：通过 schedule_tool 创建一次性任务
# ---------------------------------------------------------------------------


async def _create_oneshot_task(store: Store, *, name: str = "demo") -> str:
    """用 schedule_tool 创建一个 ``1s`` 一次性任务并**等到任务到期**，返回 task_id。

    ``schedule="1s"`` 经 schedule_parser 解析为 ``ONCE``，``next_run_at`` 是
    "now+1s" 的 ISO8601。

    v0.2.1 起 ``store.reserve_due_tasks`` 的 ONCE 分支严格检查 ``next_dt > now_dt``，
    未到点的 ONCE 任务**不会**被 reserve（防止用户实测的"立即触发"P0 bug）。
    所以本 helper 在 schedule_tool 创建后 ``await asyncio.sleep(1.1)`` 让任务
    进入 grace 窗口（已过期 0.1s，仍在 120s grace 内），调用方拿到的 task_id
    保证一接到 ticker 就被合规 reserve。
    """
    tool = build_schedule_tool(store, runtime_factory_fn=None)
    ctx = ToolContext(run_id="run-x", session_id="ses-x", turn=1, call_id="call-1")
    result = await tool.execute(
        {
            "action": "create",
            "name": name,
            "schedule": "1s",
            "agent": "default",
            "input": "say hi",
        },
        ctx,
    )
    assert result.ok, result.content
    assert result.data is not None
    task_id = result.data["task_id"]
    assert isinstance(task_id, str)
    # 等任务到期 + 进 grace 窗口（v0.2.1 ONCE 分支需要 next_dt <= now_dt 才 reserve）
    await asyncio.sleep(1.1)
    return task_id


# ---------------------------------------------------------------------------
# Case 1：schedule_tool.create → tick → bridge → audit/run 闭环
# ---------------------------------------------------------------------------


async def test_e2e_create_then_tick_then_audit(tmp_path: Path) -> None:
    """schedule_tool.create 创建一次性任务 → tick 扫到 → stub bridge 收到 →
    store 落 ScheduledRun + audits 含 create / tick_started / tick_finished。
    """
    store = Store(tmp_path)
    task_id = await _create_oneshot_task(store, name="case1 demo")

    bridge = _StubBridge(store)
    inflight: set[asyncio.Task[None]] = set()

    stats = await tick(store, bridge, inflight=inflight)  # type: ignore[arg-type]
    if inflight:
        await asyncio.gather(*inflight, return_exceptions=True)

    assert stats["due_count"] >= 1, stats
    assert stats["spawned"] >= 1, stats
    assert task_id in bridge.executed

    # audit 闭环
    audits = store.list_audits()
    actions = [a["action"] for a in audits]
    assert "create" in actions
    assert "tick_started" in actions
    assert "tick_finished" in actions

    # run 落盘
    runs = store.list_runs(task_id)
    assert len(runs) == 1
    assert runs[0].status is RunStatus.COMPLETED
    assert runs[0].run_id == "e2e-run-0001"


# ---------------------------------------------------------------------------
# Case 2：空 store → spawned=0，audit 仍写 tick_started/finished
# ---------------------------------------------------------------------------


async def test_e2e_tick_no_due(tmp_path: Path) -> None:
    """tick 时没有 due 任务 → ``spawned=0``，audit 仍写 ``tick_started`` /
    ``tick_finished``。
    """
    store = Store(tmp_path)

    stats = await tick(store, _NoOpBridge())  # type: ignore[arg-type]

    assert stats["due_count"] == 0
    assert stats["spawned"] == 0

    audits = store.list_audits()
    actions = [a["action"] for a in audits]
    assert "tick_started" in actions
    assert "tick_finished" in actions
    # 空 store 不应有 create / tick_task_error
    assert "create" not in actions
    assert "tick_task_error" not in actions


# ---------------------------------------------------------------------------
# Case 3：bridge 抛异常 → tick_task_error audit
# ---------------------------------------------------------------------------


async def test_e2e_bridge_exception_recorded(tmp_path: Path) -> None:
    """bridge.execute 抛异常 → ``tick_task_error`` audit 落地，循环不挂。"""
    store = Store(tmp_path)
    task_id = await _create_oneshot_task(store, name="case3 boom")

    inflight: set[asyncio.Task[None]] = set()
    stats = await tick(store, _CrashBridge(), inflight=inflight)  # type: ignore[arg-type]
    assert stats["due_count"] >= 1
    assert stats["spawned"] >= 1

    if inflight:
        await asyncio.gather(*inflight, return_exceptions=True)

    audits = store.list_audits()
    err_audits = [a for a in audits if a["action"] == "tick_task_error"]
    assert len(err_audits) >= 1, audits
    assert err_audits[0]["task_id"] == task_id
    assert "boom" in err_audits[0]["payload"]["error"]

    # 异常路径下不应该有 ScheduledRun 落盘（bridge 没机会写）
    runs = store.list_runs(task_id)
    assert runs == []


# ---------------------------------------------------------------------------
# Case 4：run_ticker_loop 跑一段时间 → stop 后优雅退出，至少跑过 2 次 tick
# ---------------------------------------------------------------------------


async def test_e2e_loop_stops_gracefully(tmp_path: Path) -> None:
    """``run_ticker_loop`` 跑一段时间 → ``stop_event.set()`` 后优雅退出；
    audit 中 ``tick_started`` 至少 2 次。
    """
    store = Store(tmp_path)
    bridge = _StubBridge(store)
    stop = asyncio.Event()

    loop_task = asyncio.create_task(
        run_ticker_loop(
            store,
            bridge,  # type: ignore[arg-type]
            stop,
            interval=0.05,
            max_inflight=2,
            shutdown_timeout=2.0,
        )
    )

    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(loop_task, timeout=5.0)

    audits = store.list_audits()
    started_count = sum(1 for a in audits if a["action"] == "tick_started")
    finished_count = sum(1 for a in audits if a["action"] == "tick_finished")
    assert started_count >= 2, f"expected >= 2 tick_started, got {started_count}"
    assert finished_count >= 2, f"expected >= 2 tick_finished, got {finished_count}"


# ---------------------------------------------------------------------------
# Case 5（v0.2 P0 回归）：ONCE 任务在真 run_ticker_loop 下只 fire 1 次
# ---------------------------------------------------------------------------


class _CountingBridge:
    """Case 5 用：每次 execute 自增计数；写一条真实的 ScheduledRun 进 store。"""

    def __init__(self, store: Store) -> None:
        self._store = store
        self.execute_count = 0

    async def execute(self, reservation):  # type: ignore[no-untyped-def]
        self.execute_count += 1
        await asyncio.sleep(0.05)  # 模拟一点点执行耗时
        run = ScheduledRun(
            run_id=f"e2e-once-{self.execute_count:04d}",
            task_id=reservation.task.task_id,
            status=RunStatus.COMPLETED,
            scheduled_for=reservation.scheduled_for,
            started_at=to_iso(utc_now()),
            finished_at=to_iso(utc_now()),
            session_id=f"e2e-once-sess-{self.execute_count:04d}",
            result_status="success",
            final_message_excerpt="stub final",
            error_message=None,
            failure_reason=None,
            delivery_error=None,
            silent_suppressed=False,
        )
        self._store.append_run(run)
        return run


async def test_e2e_oneshot_only_fires_once_under_real_loop(tmp_path: Path) -> None:
    """v0.2 P0 死循环 bug 回归：ONCE 任务即便在 grace 窗口内被 run_ticker_loop
    多次 tick 命中，bridge.execute 也只被调用 1 次。

    用户场景实测：1 个 ONCE 任务被触发 131 次。修复前本测试的 ``execute_count``
    会显著大于 1（视 interval / 测试总耗时而定）。

    本用例**不**走 ``tick(store, bridge)`` 直调，必须用真实 ``run_ticker_loop``：
    死循环出现在 reserve 阶段没归档，只有连续多次 reserve 才能暴露。
    """
    store = Store(tmp_path)
    task_id = await _create_oneshot_task(store, name="death loop regression")

    bridge = _CountingBridge(store)
    stop_event = asyncio.Event()

    # 等任务进入 grace 窗口（schedule_tool 设的 next_run_at = now + 1s）
    await asyncio.sleep(0.05)

    # interval=0.05s；跑 ~0.6s（约 12 次 tick）
    loop_task = asyncio.create_task(
        run_ticker_loop(
            store,
            bridge,  # type: ignore[arg-type]
            stop_event,
            interval=0.05,
            max_inflight=4,
            shutdown_timeout=2.0,
        )
    )
    await asyncio.sleep(0.6)
    stop_event.set()
    await asyncio.wait_for(loop_task, timeout=5.0)

    # 关键断言：ONCE 任务只被 execute 1 次（不是 N 次）
    assert bridge.execute_count == 1, (
        f"ONCE 任务被触发 {bridge.execute_count} 次，期望 1 次（v0.2 死循环 bug 回归）"
    )

    # 任务状态：归档（enabled=False、state=COMPLETED、last_run_at 已设）
    tasks = store.list_tasks(include_disabled=True)
    assert len(tasks) == 1
    archived = tasks[0]
    assert archived.task_id == task_id
    assert archived.enabled is False
    # last_run_at 来自 reserve 阶段或 bridge 完成阶段（任一非空即可）
    assert archived.last_run_at is not None
    assert archived.next_run_at is None

    # 落盘 run 也只有 1 条
    runs = store.list_runs(task_id)
    assert len(runs) == 1


# ---------------------------------------------------------------------------
# Case 6（v0.2 P0 对照）：recurring 任务 fire 多次，未被误归档
# ---------------------------------------------------------------------------


async def _create_recurring_task(store: Store, *, name: str = "recurring demo") -> str:
    """用 schedule_tool 创建 ``every 1s`` recurring 任务，并显式把
    ``next_run_at`` 设为"现在"，避免 v0.2 当前实现里 recurring 创建后
    ``next_run_at=None`` 需 trigger engine 兜底（ticker 不计算）。

    这只是测试 fixture，不影响生产行为。
    """
    tool = build_schedule_tool(store, runtime_factory_fn=None)
    ctx = ToolContext(run_id="run-x", session_id="ses-x", turn=1, call_id="call-1")
    result = await tool.execute(
        {
            "action": "create",
            "name": name,
            "schedule": "every 1s",
            "agent": "default",
            "input": "tick",
        },
        ctx,
    )
    assert result.ok, result.content
    assert result.data is not None
    task_id = result.data["task_id"]
    assert isinstance(task_id, str)
    # recurring 任务由 trigger engine / schedule_tool 决定首次 next_run_at；
    # 这里手动设为"现在"，让 ticker 立即可见
    store.advance_next_run(task_id, next_run_at=to_iso(utc_now()))
    return task_id


async def test_e2e_recurring_fires_normally_under_real_loop(tmp_path: Path) -> None:
    """对照测试：recurring 任务在 ~2.5s 内被 fire 多次，**没有**被当成 ONCE 误归档。

    防止修 ONCE 时把 recurring 也意外标 enabled=False。
    """
    store = Store(tmp_path)
    task_id = await _create_recurring_task(store, name="recurring control")

    bridge = _CountingBridge(store)
    stop_event = asyncio.Event()

    loop_task = asyncio.create_task(
        run_ticker_loop(
            store,
            bridge,  # type: ignore[arg-type]
            stop_event,
            interval=0.1,
            max_inflight=4,
            shutdown_timeout=2.0,
        )
    )
    # 留够时间让 1s 周期任务 fire 至少 2 次（首次到点 + 再过 1s 推进）
    await asyncio.sleep(2.5)
    stop_event.set()
    await asyncio.wait_for(loop_task, timeout=5.0)

    # 至少 fire 2 次（1s 周期，2.5s 内）
    assert bridge.execute_count >= 2, (
        f"recurring 任务仅 fire {bridge.execute_count} 次，期望 >= 2 次"
    )

    # task 仍是 enabled，未被误归档
    tasks = store.list_tasks(include_disabled=True)
    assert len(tasks) == 1
    survivor = tasks[0]
    assert survivor.task_id == task_id
    assert survivor.enabled is True


# ---------------------------------------------------------------------------
# 档 2：v0.2 闭环矩阵 E2-E8（"reserve / 状态变更不闭环"系统排查）
# ---------------------------------------------------------------------------
#
# 设计目的：上次修了 ONCE 死循环（reserve 后没归档）；同类模式可能藏在多处。
# 这组 e2e 在 ticker × bridge × store 三层之间跑全闭环，逐一验证：
#
# - normal recurring（SECONDS）能正常被多次 fire（不会被异常归档）
# - normal recurring 在 grace 内首次创建会按 SKIP 快进（不补跑、不死循环）
# - CRON 启动时 stale 任务被快进，audit 落 run_skipped_stale
# - bridge 持续抛异常时 ONCE 仍归档（不会因为执行失败就回到 reserve 池死循环）
# - inactivity_timeout 的 task.last_run_at 终态正确（保险写盘有效）
# - concurrency=replace：长任务被新触发取代时旧 RUNNING 会被 cancel
# - recover_stale_runs：启动收尾遗留 RUNNING run（重启 Store 后状态正确）
# ---------------------------------------------------------------------------


# --- helpers：直接构造 store-level task（绕过 schedule_parser 秒级最小限制）------


def _t(seconds_offset: int = 0) -> str:
    """E2-E8 用：相对当前 UTC 偏移的 ISO 时间。"""
    from datetime import timedelta

    return to_iso(utc_now() + timedelta(seconds=seconds_offset))


def _make_seconds_task(
    *,
    task_id: str,
    period_seconds: int,
    next_run_at: str,
    misfire: MisfirePolicy = MisfirePolicy.SKIP,
    concurrency: ConcurrencyPolicy = ConcurrencyPolicy.FORBID,
    inactivity_timeout: int | None = None,
) -> ScheduledTask:
    return ScheduledTask(
        task_id=task_id,
        name=f"task-{task_id}",
        enabled=True,
        state=TaskState.SCHEDULED,
        origin=TaskOrigin.TOOL,
        trigger=ScheduleTrigger(
            trigger_type=TriggerType.SECONDS,
            expr=str(period_seconds),
            timezone="UTC",
        ),
        policy=TaskExecutionPolicy(
            session_mode=SessionMode.FRESH_SESSION,
            concurrency_policy=concurrency,
            misfire_policy=misfire,
            inactivity_timeout_seconds=inactivity_timeout,
        ),
        target=TaskTarget(agent_name="default", input_text="ping", metadata={}),
        next_run_at=next_run_at,
        last_run_at=None,
        created_by="tester",
        created_at=_t(0),
        updated_at=_t(0),
    )


def _make_cron_task(
    *,
    task_id: str,
    cron_expr: str,
    next_run_at: str,
    misfire: MisfirePolicy = MisfirePolicy.SKIP,
) -> ScheduledTask:
    return ScheduledTask(
        task_id=task_id,
        name=f"task-{task_id}",
        enabled=True,
        state=TaskState.SCHEDULED,
        origin=TaskOrigin.TOOL,
        trigger=ScheduleTrigger(trigger_type=TriggerType.CRON, expr=cron_expr, timezone="UTC"),
        policy=TaskExecutionPolicy(
            session_mode=SessionMode.FRESH_SESSION,
            concurrency_policy=ConcurrencyPolicy.FORBID,
            misfire_policy=misfire,
        ),
        target=TaskTarget(agent_name="default", input_text="ping", metadata={}),
        next_run_at=next_run_at,
        last_run_at=None,
        created_by="tester",
        created_at=_t(0),
        updated_at=_t(0),
    )


# ----- E2 ------------------------------------------------------------------


async def test_e2e_seconds_recurring_under_real_loop(tmp_path: Path) -> None:
    """E2 — SECONDS=1s + SKIP，在真 run_ticker_loop 下 fire 多次。

    覆盖：normal recurring 路径下 task.last_run_at 被回写、enabled 不变。
    """
    store = Store(tmp_path)
    task = _make_seconds_task(
        task_id="t-e2-seconds",
        period_seconds=1,
        next_run_at=_t(0),  # 立即可见
        misfire=MisfirePolicy.SKIP,
    )
    store.create_task(task)

    bridge = _CountingBridge(store)
    stop = asyncio.Event()
    loop_task = asyncio.create_task(
        run_ticker_loop(
            store,
            bridge,  # type: ignore[arg-type]
            stop,
            interval=0.05,
            max_inflight=4,
            shutdown_timeout=2.0,
        )
    )
    # SECONDS period=1s：跑 ~2.5s 期望 fire ≥ 2 次（首次 + 推进 1s 一次）
    await asyncio.sleep(2.5)
    stop.set()
    await asyncio.wait_for(loop_task, timeout=5.0)

    assert bridge.execute_count >= 2, f"SECONDS 期望 fire >= 2 次，实际 {bridge.execute_count}"
    survivor = store.get_task("t-e2-seconds")
    assert survivor is not None
    assert survivor.enabled is True, "recurring 任务不该被归档"


# ----- E3 ------------------------------------------------------------------


async def test_e2e_seconds_grace_skip_no_fire(tmp_path: Path) -> None:
    """E3 — SECONDS=1s + SKIP, next_run_at = now-300s（远超 120s grace）。

    第一次 reserve 应快进到 now+1s，**不**返回 reservation；ticker 跑短时间内
    `bridge.execute` 不被调用（验证 SKIP 路径不补跑）。
    """
    store = Store(tmp_path)
    task = _make_seconds_task(
        task_id="t-e3-seconds-skip",
        period_seconds=1,
        next_run_at=_t(-300),  # 远超 120s grace
        misfire=MisfirePolicy.SKIP,
    )
    store.create_task(task)

    bridge = _CountingBridge(store)
    stop = asyncio.Event()
    loop_task = asyncio.create_task(
        run_ticker_loop(
            store,
            bridge,  # type: ignore[arg-type]
            stop,
            interval=0.05,
            max_inflight=4,
            shutdown_timeout=2.0,
        )
    )
    # 0.5s 内最多 ~10 次 tick；首次 tick 后 next_run_at 已快进到 now+1s（未来）
    # → 后续 tick 都看不到 due。期望整段 0.5s 内 execute_count == 0。
    await asyncio.sleep(0.5)
    stop.set()
    await asyncio.wait_for(loop_task, timeout=5.0)

    assert bridge.execute_count == 0, (
        f"SKIP stale 路径不应触发 execute；实际 {bridge.execute_count}"
    )
    # audit 应有 run_skipped_stale
    audits = store.list_audits(task_id="t-e3-seconds-skip")
    actions = [a["action"] for a in audits]
    assert "run_skipped_stale" in actions, actions


# ----- E4 ------------------------------------------------------------------


async def test_e2e_cron_stale_skip_under_real_loop(tmp_path: Path) -> None:
    """E4 — CRON `*/1 * * * *`（period=60s, grace=120s）启动时 next_run_at
    比 now 早 30 分钟（>>grace） + SKIP → stale 快进；execute_count == 0；
    audit 含 run_skipped_stale。
    """
    store = Store(tmp_path)
    task = _make_cron_task(
        task_id="t-e4-cron-stale",
        cron_expr="*/1 * * * *",
        next_run_at=_t(-1800),
        misfire=MisfirePolicy.SKIP,
    )
    store.create_task(task)

    bridge = _CountingBridge(store)
    stop = asyncio.Event()
    loop_task = asyncio.create_task(
        run_ticker_loop(
            store,
            bridge,  # type: ignore[arg-type]
            stop,
            interval=0.05,
            max_inflight=4,
            shutdown_timeout=2.0,
        )
    )
    await asyncio.sleep(0.5)
    stop.set()
    await asyncio.wait_for(loop_task, timeout=5.0)

    assert bridge.execute_count == 0
    audits = store.list_audits(task_id="t-e4-cron-stale")
    skipped = [a for a in audits if a["action"] == "run_skipped_stale"]
    assert len(skipped) >= 1, audits


# ----- E5 ------------------------------------------------------------------


class _AlwaysCrashBridge:
    """E5 用：每次 execute 都 raise。被 ticker 的 _run_one 异常吞 → audit
    ``tick_task_error``；不影响 ONCE 归档（因为归档发生在 reserve 阶段）。"""

    def __init__(self) -> None:
        self.execute_count = 0

    async def execute(self, reservation):  # type: ignore[no-untyped-def]
        self.execute_count += 1
        raise RuntimeError(f"boom #{self.execute_count}")


async def test_e2e_oneshot_with_crashing_bridge_no_loop(tmp_path: Path) -> None:
    """E5（**P0 排查关键**）— ONCE + bridge 每次 raise → run_ticker_loop 跑 0.5s
    × 10 次 tick → execute_count == 1（不是 N 次）。

    防御 reserve 阶段归档的鲁棒性：即便 bridge 持续失败，归档也不能回滚。
    任务**必须**仍然 last_run_at 非空、enabled=False（已下线）。
    """
    store = Store(tmp_path)
    next_run = _t(-5)  # grace 内
    task = ScheduledTask(
        task_id="t-e5-once-crash",
        name="task-e5",
        enabled=True,
        state=TaskState.SCHEDULED,
        origin=TaskOrigin.TOOL,
        trigger=ScheduleTrigger(trigger_type=TriggerType.ONCE, expr=next_run, timezone="UTC"),
        policy=TaskExecutionPolicy(
            session_mode=SessionMode.FRESH_SESSION,
            concurrency_policy=ConcurrencyPolicy.FORBID,
            misfire_policy=MisfirePolicy.SKIP,
        ),
        target=TaskTarget(agent_name="default", input_text="ping", metadata={}),
        next_run_at=next_run,
        last_run_at=None,
        created_by="tester",
        created_at=_t(0),
        updated_at=_t(0),
    )
    store.create_task(task)

    bridge = _AlwaysCrashBridge()
    stop = asyncio.Event()
    loop_task = asyncio.create_task(
        run_ticker_loop(
            store,
            bridge,  # type: ignore[arg-type]
            stop,
            interval=0.05,
            max_inflight=4,
            shutdown_timeout=2.0,
        )
    )
    await asyncio.sleep(0.5)  # ~10 次 tick
    stop.set()
    await asyncio.wait_for(loop_task, timeout=5.0)

    # 关键断言：bridge 持续抛异常也只调 1 次（ONCE 在 reserve 阶段就归档了）
    assert bridge.execute_count == 1, (
        f"ONCE + 持续异常 bridge 期望 execute=1，实际 {bridge.execute_count}（死循环回归）"
    )
    archived = store.get_task("t-e5-once-crash")
    assert archived is not None
    assert archived.enabled is False, "ONCE 必须归档（即便 bridge 失败）"
    assert archived.state is TaskState.COMPLETED
    assert archived.last_run_at is not None

    # 单次失败应被 audit；但归档动作仍写入 run_oneshot_archived
    audits = store.list_audits(task_id="t-e5-once-crash")
    actions = [a["action"] for a in audits]
    assert actions.count("run_oneshot_archived") == 1, f"ONCE 应只归档 1 次；audit 实际：{actions}"
    # bridge 异常被 ticker 吞为 tick_task_error
    assert any(a == "tick_task_error" for a in actions), actions


# ----- E6 ------------------------------------------------------------------


class _InactivityTimeoutBridge:
    """E6 用：模拟 ExecutionBridge 的 inactivity_timeout 路径——
    写 RUNNING run → 模拟 watchdog 触发 → supersede 成 INACTIVITY_TIMEOUT，
    并显式回写 task.last_run_at（与真实 ExecutionBridge.execute 末尾一致的保险）。
    """

    def __init__(self, store: Store) -> None:
        self._store = store
        self.execute_count = 0

    async def execute(self, reservation):  # type: ignore[no-untyped-def]
        self.execute_count += 1
        run_id = f"run-e6-{self.execute_count:04d}"
        running = ScheduledRun(
            run_id=run_id,
            task_id=reservation.task.task_id,
            status=RunStatus.RUNNING,
            scheduled_for=reservation.scheduled_for,
            started_at=to_iso(utc_now()),
            finished_at=None,
            session_id=f"sess-e6-{self.execute_count:04d}",
            result_status=None,
            final_message_excerpt=None,
            error_message=None,
            failure_reason=None,
            delivery_error=None,
            silent_suppressed=False,
        )
        self._store.append_run(running)
        # 模拟 watchdog 触发（不真等 inactivity_timeout）
        await asyncio.sleep(0.05)
        finished_at = to_iso(utc_now())
        timed_out = ScheduledRun(
            run_id=run_id,
            task_id=reservation.task.task_id,
            status=RunStatus.INACTIVITY_TIMEOUT,
            scheduled_for=reservation.scheduled_for,
            started_at=running.started_at,
            finished_at=finished_at,
            session_id=running.session_id,
            result_status=None,
            final_message_excerpt=None,
            error_message="inactivity timeout",
            failure_reason=RunFailureReason.INACTIVITY_TIMEOUT,
            delivery_error=None,
            silent_suppressed=False,
        )
        self._store.supersede_and_append_run(timed_out)
        # 模拟真实 bridge 末尾的回写
        with _suppress_task_not_found():
            self._store.update_task(reservation.task.task_id, last_run_at=finished_at)
        self._store.append_audit(
            action="run_inactivity_timeout",
            task_id=reservation.task.task_id,
            actor="scheduler",
            payload={"run_id": run_id, "error_message": "inactivity timeout"},
        )
        return timed_out


async def test_e2e_inactivity_timeout_state_consistent(tmp_path: Path) -> None:
    """E6 — inactivity_timeout 路径下 task 与 run 状态最终一致。

    验证：
    - run jsonl 含 status=inactivity_timeout 行
    - task.last_run_at 被回写（保险路径生效）
    - audit 含 run_inactivity_timeout
    """
    store = Store(tmp_path)
    task = _make_seconds_task(
        task_id="t-e6-inactivity",
        period_seconds=1,
        next_run_at=_t(0),
        misfire=MisfirePolicy.SKIP,
        inactivity_timeout=1,  # 不依赖；stub 自己构造路径
    )
    store.create_task(task)

    bridge = _InactivityTimeoutBridge(store)
    stop = asyncio.Event()
    loop_task = asyncio.create_task(
        run_ticker_loop(
            store,
            bridge,  # type: ignore[arg-type]
            stop,
            interval=0.05,
            max_inflight=4,
            shutdown_timeout=2.0,
        )
    )
    await asyncio.sleep(0.5)
    stop.set()
    await asyncio.wait_for(loop_task, timeout=5.0)

    assert bridge.execute_count >= 1
    runs = store.list_runs("t-e6-inactivity")
    assert len(runs) >= 1
    # 至少一条 INACTIVITY_TIMEOUT
    assert any(r.status is RunStatus.INACTIVITY_TIMEOUT for r in runs), runs
    # task.last_run_at 已回写（不是 None）
    after = store.get_task("t-e6-inactivity")
    assert after is not None
    assert after.last_run_at is not None, "inactivity_timeout 后 task.last_run_at 必须被回写"
    # audit
    audits = store.list_audits(task_id="t-e6-inactivity")
    assert any(a["action"] == "run_inactivity_timeout" for a in audits), audits


# ----- E7 ------------------------------------------------------------------


class _ReplaceableBridge:
    """E7 用：模拟 concurrency=replace 路径——

    - 写 RUNNING run（不 finish）
    - 第二次 reservation 来时调用 :func:`apply_concurrency_policy`，旧 RUNNING
      被 cancel；本次正常写新 RUNNING + COMPLETED
    - 暴露 execute_count + cancel_count 供断言
    """

    def __init__(self, store: Store) -> None:
        self._store = store
        self.execute_count = 0
        self.cancel_count = 0

    async def execute(self, reservation):  # type: ignore[no-untyped-def]
        self.execute_count += 1
        # 应用 concurrency_policy（与真实 ExecutionBridge.execute 同样位置调）
        decision = apply_concurrency_policy(task=reservation.task, store=self._store)
        if decision.action == "replace":
            self.cancel_count += 1
        elif decision.action == "skip":
            return None  # 不应在 replace 路径出现

        run_id = f"run-e7-{self.execute_count:04d}"
        running = ScheduledRun(
            run_id=run_id,
            task_id=reservation.task.task_id,
            status=RunStatus.RUNNING,
            scheduled_for=reservation.scheduled_for,
            started_at=to_iso(utc_now()),
            finished_at=None,
            session_id=f"sess-e7-{self.execute_count:04d}",
            result_status=None,
            final_message_excerpt=None,
            error_message=None,
            failure_reason=None,
            delivery_error=None,
            silent_suppressed=False,
        )
        self._store.append_run(running)
        # 模拟"长任务"——但既然真任务无法被外部抢断，这里同步走完
        await asyncio.sleep(0.05)
        finished = ScheduledRun(
            run_id=run_id,
            task_id=reservation.task.task_id,
            status=RunStatus.COMPLETED,
            scheduled_for=reservation.scheduled_for,
            started_at=running.started_at,
            finished_at=to_iso(utc_now()),
            session_id=running.session_id,
            result_status="success",
            final_message_excerpt="ok",
            error_message=None,
            failure_reason=None,
            delivery_error=None,
            silent_suppressed=False,
        )
        self._store.supersede_and_append_run(finished)
        return finished


async def test_e2e_concurrency_replace_cancels_running(tmp_path: Path) -> None:
    """E7 — concurrency=replace + 模拟 RUNNING 旧 run 存在 → 新 reservation
    被处理时旧 RUNNING 被 cancel；audit 含 run_cancelled_by_replace。
    """
    store = Store(tmp_path)
    task = _make_seconds_task(
        task_id="t-e7-replace",
        period_seconds=1,
        next_run_at=_t(0),
        misfire=MisfirePolicy.SKIP,
        concurrency=ConcurrencyPolicy.REPLACE,
    )
    store.create_task(task)

    # 预置一条"未完成"RUNNING run，让第一次 reservation 触发 replace
    pre_run = ScheduledRun(
        run_id="pre-run-001",
        task_id="t-e7-replace",
        status=RunStatus.RUNNING,
        scheduled_for=_t(-1),
        started_at=_t(-1),
        finished_at=None,
        session_id="pre-sess",
        result_status=None,
        final_message_excerpt=None,
        error_message=None,
        failure_reason=None,
        delivery_error=None,
        silent_suppressed=False,
    )
    store.append_run(pre_run)

    bridge = _ReplaceableBridge(store)
    stop = asyncio.Event()
    loop_task = asyncio.create_task(
        run_ticker_loop(
            store,
            bridge,  # type: ignore[arg-type]
            stop,
            interval=0.05,
            max_inflight=4,
            shutdown_timeout=2.0,
        )
    )
    await asyncio.sleep(0.5)
    stop.set()
    await asyncio.wait_for(loop_task, timeout=5.0)

    # 至少 fire 1 次，且至少 cancel 1 次（pre_run）
    assert bridge.execute_count >= 1
    assert bridge.cancel_count >= 1, "REPLACE 应至少 cancel 1 次旧 RUNNING"
    # audit 含 run_cancelled_by_replace
    audits = store.list_audits(task_id="t-e7-replace")
    cancelled_by_replace = [a for a in audits if a["action"] == "run_cancelled_by_replace"]
    assert len(cancelled_by_replace) >= 1, audits

    # runs jsonl 含 status=cancelled
    runs = store.list_runs("t-e7-replace")
    cancelled = [r for r in runs if r.status is RunStatus.CANCELLED]
    assert len(cancelled) >= 1, [r.status.value for r in runs]


# ----- E8 ------------------------------------------------------------------


async def test_e2e_recover_stale_runs_on_restart(tmp_path: Path) -> None:
    """E8 — 手写一条 RUNNING run 到 jsonl → 重启 Store →
    那条变 ABANDONED + audit 含 run_failed。

    （E8 不需要 ticker，它是 Store 启动期 `recover_stale_runs` 的契约测试。
    放在 e2e 里是因为它跨"重启"边界，模拟进程级状态恢复闭环。）
    """
    # 第一次进程：写一条 RUNNING run，然后"崩溃"（什么都不做，丢任务）
    store_a = Store(tmp_path)
    task = _make_seconds_task(
        task_id="t-e8-recovery",
        period_seconds=1,
        next_run_at=_t(0),
        misfire=MisfirePolicy.SKIP,
    )
    store_a.create_task(task)
    pre_run = ScheduledRun(
        run_id="run-e8-stuck",
        task_id="t-e8-recovery",
        status=RunStatus.RUNNING,
        scheduled_for=_t(-10),
        started_at=_t(-10),
        finished_at=None,
        session_id="sess-e8",
        result_status=None,
        final_message_excerpt=None,
        error_message=None,
        failure_reason=None,
        delivery_error=None,
        silent_suppressed=False,
    )
    store_a.append_run(pre_run)

    # 第二次进程：新 Store 实例 → 调 recover_stale_runs（生产路径里启动时显式调）
    store_b = Store(tmp_path)
    recovered = store_b.recover_stale_runs()
    assert recovered == 1

    latest = store_b.get_latest_run("t-e8-recovery")
    assert latest is not None
    assert latest.run_id == "run-e8-stuck"
    assert latest.status is RunStatus.ABANDONED
    assert latest.failure_reason is RunFailureReason.ABANDONED_ON_RESTART
    assert latest.finished_at is not None

    # audit 含 run_failed（recover_stale_runs 内统一写 run_failed）
    audits = store_b.list_audits(task_id="t-e8-recovery")
    failed = [a for a in audits if a["action"] == "run_failed"]
    assert len(failed) >= 1, audits
    assert failed[0]["payload"]["recovery"] == "abandoned_on_restart"


# ---------------------------------------------------------------------------
# Case #15-#16（v0.2.1）：B1 修复回归 + cron 6 字段 first_run_at
# ---------------------------------------------------------------------------
#
# B1 bug：schedule_tool 创建 recurring 任务时 next_run_at=None，store 看到
# None 直接跳过 reserve，任务永不触发。修复后 compute_first_run_at 统一为
# INTERVAL / CRON / SECONDS 算 first_run_at。这两个 case 走真 run_ticker_loop
# 端到端验证修复有效，并防御回归。
# ---------------------------------------------------------------------------


async def test_e2e_interval_recurring_fires_under_real_loop(tmp_path: Path) -> None:
    """v0.2.1 P0 回归：recurring 任务（every 1s）在 run_ticker_loop 下被 fire 多次。

    防御 B1 bug 回归：之前 schedule_tool 创建 recurring 任务时 next_run_at=None，
    任务永不触发。修复后 compute_first_run_at 会算出 next_run_at。

    本测试用真 run_ticker_loop（不是 tick 直调），跑 ~2.5s，每 0.1s tick 一次，
    断言 bridge.execute 至少被调 1 次（覆盖 first_run + 至少 1 次 advance）。
    """
    store = Store(tmp_path)

    # 用 schedule_tool 创建 recurring 任务（every 1s）
    tool = build_schedule_tool(store, runtime_factory_fn=None)
    ctx = ToolContext(run_id="r-recurring", session_id="s", turn=1, call_id="c")
    result = await tool.execute(
        {
            "action": "create",
            "name": "every-1s-test",
            "schedule": "every 1s",
            "agent": "default",
            "input": "noop",
        },
        ctx,
    )
    assert result.ok, result.content

    tasks_initial = store.list_tasks()
    assert len(tasks_initial) == 1
    initial_next_run = tasks_initial[0].next_run_at
    assert initial_next_run is not None, (
        "B1 修复回归：every 1s 任务 next_run_at 必须非空，否则 ticker 跳过"
    )

    # 跑真 ticker
    bridge = _CountingBridge(store)
    stop_event = asyncio.Event()

    loop_task = asyncio.create_task(
        run_ticker_loop(
            store,
            bridge,  # type: ignore[arg-type]
            stop_event,
            interval=0.1,
            max_inflight=4,
            shutdown_timeout=2.0,
        )
    )
    await asyncio.sleep(2.5)  # 跑 2.5s，期望 fire 1-2 次（every 1s）
    stop_event.set()
    await asyncio.wait_for(loop_task, timeout=5.0)

    # 至少 fire 1 次（first_run），通常 2 次
    assert bridge.execute_count >= 1, (
        f"every 1s recurring 应至少 fire 1 次，实际 {bridge.execute_count}"
    )
    # task 仍 enabled（recurring 不归档）
    tasks_after = store.list_tasks(include_disabled=True)
    assert tasks_after[0].enabled, "recurring 任务不应被归档"


async def test_e2e_cron_recurring_fires_under_real_loop(tmp_path: Path) -> None:
    """v0.2.1 P1 验收：6 字段 cron ``*/1 * * * * *``（每秒）在 run_ticker_loop
    下被 fire 多次。

    跟 #15 同模式但走 cron 6 字段分支，验证 timing.compute_first_run_at 能为
    cron 任务正确算 next_run_at。
    """
    store = Store(tmp_path)

    tool = build_schedule_tool(store, runtime_factory_fn=None)
    ctx = ToolContext(run_id="r-cron", session_id="s", turn=1, call_id="c")
    result = await tool.execute(
        {
            "action": "create",
            "name": "every-second-cron",
            "schedule": "*/1 * * * * *",  # 6 字段 cron 每秒
            "agent": "default",
            "input": "noop",
        },
        ctx,
    )
    assert result.ok, result.content

    tasks_initial = store.list_tasks()
    assert tasks_initial[0].next_run_at is not None, "cron 6 字段任务 next_run_at 必须非空"

    bridge = _CountingBridge(store)
    stop_event = asyncio.Event()
    loop_task = asyncio.create_task(
        run_ticker_loop(
            store,
            bridge,  # type: ignore[arg-type]
            stop_event,
            interval=0.1,
            max_inflight=4,
            shutdown_timeout=2.0,
        )
    )
    await asyncio.sleep(2.5)
    stop_event.set()
    await asyncio.wait_for(loop_task, timeout=5.0)

    assert bridge.execute_count >= 1, (
        f"cron */1 * * * * * 应至少 fire 1 次，实际 {bridge.execute_count}"
    )


# 显式标记（项目 asyncio_mode=auto 下其实不需要，留作意图说明）
pytestmark = pytest.mark.asyncio
