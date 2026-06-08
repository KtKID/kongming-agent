"""scheduler v0.2 — Hermes 风格 1s tick loop + Semaphore 限流 + 不阻塞 fire。

v0.1 基线：单后台循环每 ``interval`` 秒调一次 :func:`tick`，单次 tick 用
try/except 吞所有异常，不让单次失败弄崩整个循环（参考 ``other/hermes-agent/cron/scheduler.py:tick``）。

v0.2 升级：

- 默认 ``interval`` 从 60s 降到 1s（支持秒级 SECONDS 触发器；跟
  :class:`infrastructure.config.SchedulerConfig.interval` 联动）
- 单 tick 内不再 ``await bridge.execute(...)`` 串行，而是
  ``asyncio.create_task(...)`` 让多个 due 任务并行 fire；防止单个长任务把后续
  due 拖到下一个 interval
- 用 :class:`asyncio.Semaphore` 限制同时运行的任务数（默认 8），避免"100 个秒级
  任务一齐 fire 拖死单进程"
- 维护 in-flight :class:`asyncio.Task` 集合：任务 done 自动 ``discard``；
  ``stop_event.set()`` 后 ``gather`` 等所有 in-flight 完成，``shutdown_timeout``
  到了就 ``cancel`` 兜底

设计要点：

- :func:`tick`：单次扫描——调 :meth:`scheduler.store.Store.reserve_due_tasks`
  拿到 due reservation 列表；每个 reservation 通过 :func:`_run_one` 派 task；
  统计 ``due_count`` + ``spawned``（执行结果是异步的，tick 返回时还没完成，因此
  不再统计 ``executed`` / ``failed``——单任务异常落到 audit ``tick_task_error``）
- :func:`_run_one`：包一层 ``Semaphore`` + ``asyncio.wait_for`` + 异常吞 + audit；
  ``semaphore=None`` 时不限流（保留无限流语义，给单元测试用）
- :func:`run_ticker_loop`：``while not stop_event.is_set(): try: await tick(...)
  except: log; await sleep(interval)``，3 层异常兜底（单任务 / 单 tick / 整个循环）；
  退出时等待所有 in-flight 任务完成，超时则 cancel
- 每次 tick 都写 audit ``tick_started`` + ``tick_finished``（含 due 数 / spawned 数），
  便于运维查"循环到底有没有在跑 / 哪个任务挂了"
- 单任务 :func:`asyncio.wait_for` 兜底超时 = ``inactivity_timeout_seconds * 2``
  （bridge 内部本身有 watchdog，本层只是防止 bridge 异常 hang）

边界：

- 不引入新 EventSink Protocol，不直接 import safety / core 内部
- 不在本模块装配 :class:`ExecutionBridge` —— 装配由
  :mod:`scheduler.runtime_factory` 或调用方自己负责，本模块仅消费已经装好的 bridge
- :func:`tick` 对外签名向后兼容：``semaphore`` / ``inflight`` 都是可选
  kwarg；不传时 = 无限流 + 不跟踪 in-flight，跟之前 v0.1 行为一样（除了不再
  串行 await）
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from scheduler.timing import to_iso, utc_now

if TYPE_CHECKING:
    from scheduler.domain import DueTaskReservation
    from scheduler.execution_bridge import ExecutionBridge
    from scheduler.store import Store

logger = logging.getLogger(__name__)


_DEFAULT_TASK_TIMEOUT_SECONDS = 600
"""单任务 :func:`asyncio.wait_for` 兜底超时（秒），仅在 ``task.policy.inactivity_timeout_seconds``
不可用时使用；bridge 内部本身有 watchdog，本层只是防止 bridge 异常 hang。"""

_DEFAULT_INTERVAL = 1.0
"""默认 tick 间隔（秒）；v0.2 从 60s 降到 1s 以支持秒级 SECONDS 触发器。"""

_DEFAULT_MAX_INFLIGHT = 8
"""默认最多同时 in-flight 的任务数。"""

_DEFAULT_SHUTDOWN_TIMEOUT = 30.0
"""``stop_event.set()`` 后等待所有 in-flight 任务收尾的最大秒数；超过则 cancel。"""


async def tick(
    store: Store,
    bridge: ExecutionBridge,
    *,
    now: str | None = None,
    semaphore: asyncio.Semaphore | None = None,
    inflight: set[asyncio.Task[None]] | None = None,
) -> dict[str, int]:
    """单次 tick：扫一次 due 任务并 *派发* 执行（不阻塞）。

    Args:
        store: 调用 :meth:`Store.reserve_due_tasks` 拣选 due reservation。
        bridge: 已装配的 :class:`ExecutionBridge`。
        now: 触发时间（ISO8601）；缺省取 :func:`utc_now`。仅作为 audit / store
            判断 due 的"现在"基准，不影响真实执行时刻。
        semaphore: 给每个单任务 :meth:`bridge.execute` 加并发上限的 Semaphore；
            ``None`` 时不限流（无限并发——只在单元测试场景使用）。
        inflight: 调用方维护的 in-flight task 集合；新派发的 task 会 ``add``
            进去，并通过 ``add_done_callback`` 在 done 时自动 ``discard``。
            ``None`` 时不跟踪（v0.1 兼容）。

    Returns:
        ``{"due_count": ..., "spawned": ...}``：``due_count`` 是 store 返回的
        due 数；``spawned`` 是真正派出去的 task 数。注意：这两个值在正常路径
        下相等；只有派发本身异常（极少见）会让 ``spawned < due_count``。
        异步执行结果（成功 / 失败）不在 tick 同步返回——查 audit
        ``tick_task_error`` / store 里的 :class:`ScheduledRun` 记录。

    Note:
        :meth:`store.reserve_due_tasks` 自身抛异常时返回
        ``{"due_count": 0, "spawned": 0}``，并写 audit ``tick_failed``，不写
        ``tick_finished``（避免误导）。
    """
    if now is None:
        now = to_iso(utc_now())

    store.append_audit(
        action="tick_started",
        task_id=None,
        actor="ticker",
        payload={"tick_at": now},
    )

    try:
        reservations = store.reserve_due_tasks(now=now)
    except Exception as exc:
        logger.exception("tick: reserve_due_tasks failed")
        store.append_audit(
            action="tick_failed",
            task_id=None,
            actor="ticker",
            payload={"tick_at": now, "error": str(exc)},
        )
        return {"due_count": 0, "spawned": 0}

    spawned = 0
    for r in reservations:
        task = asyncio.create_task(_run_one(bridge, r, semaphore, store, now))
        if inflight is not None:
            inflight.add(task)
            task.add_done_callback(inflight.discard)
        spawned += 1

    store.append_audit(
        action="tick_finished",
        task_id=None,
        actor="ticker",
        payload={
            "tick_at": now,
            "due_count": len(reservations),
            "spawned": spawned,
        },
    )
    return {"due_count": len(reservations), "spawned": spawned}


async def _run_one(
    bridge: ExecutionBridge,
    reservation: DueTaskReservation,
    semaphore: asyncio.Semaphore | None,
    store: Store,
    tick_at: str,
) -> None:
    """单任务执行 wrapper：Semaphore 限流 + ``wait_for`` 超时 + 异常吞 + audit。

    任何异常都被吞为 audit ``tick_task_error``，不向上抛——因为这个 task 是
    ``asyncio.create_task(...)`` 出来的，向上抛会变成"Task exception was never
    retrieved"警告而不是真的退出循环。
    """
    timeout_base = (
        reservation.task.policy.inactivity_timeout_seconds or _DEFAULT_TASK_TIMEOUT_SECONDS
    )
    # 2x 兜底：bridge 自己有 InactivityWatchdog；这一层只防止 bridge 整个 hang。
    outer_timeout = max(1, timeout_base) * 2

    async def _go() -> None:
        try:
            await asyncio.wait_for(bridge.execute(reservation), timeout=outer_timeout)
        except asyncio.CancelledError:
            # shutdown 触发的强制 cancel；安静退出，让 gather 收回。
            raise
        except Exception as exc:
            logger.exception("tick: task %s execute failed", reservation.task.task_id)
            store.append_audit(
                action="tick_task_error",
                task_id=reservation.task.task_id,
                actor="ticker",
                payload={"error": str(exc), "tick_at": tick_at},
            )

    if semaphore is not None:
        async with semaphore:
            await _go()
    else:
        await _go()


async def run_ticker_loop(
    store: Store,
    bridge: ExecutionBridge,
    stop_event: asyncio.Event,
    *,
    interval: float = _DEFAULT_INTERVAL,
    max_inflight: int = _DEFAULT_MAX_INFLIGHT,
    shutdown_timeout: float = _DEFAULT_SHUTDOWN_TIMEOUT,
) -> None:
    """后台循环：每 ``interval`` 秒调一次 :func:`tick`，``stop_event.set()`` 才退出。

    Args:
        store: 给 :func:`tick` 用。
        bridge: 给 :func:`tick` 用。
        stop_event: ``set()`` 后 loop 自然退出；退出前会等所有 in-flight
            任务收尾（最多 ``shutdown_timeout`` 秒）。
        interval: 两次 tick 之间的间隔（秒）；默认 1.0。
        max_inflight: 同时 in-flight 的任务数上限；默认 8。
        shutdown_timeout: ``stop_event.set()`` 后等所有 in-flight 任务完成的
            最大秒数；超过则 ``cancel`` 剩余任务并 ``gather`` 收回。

    3 层异常兜底（参考 Hermes ``tick``）：

    1. 单任务 try/except：在 :func:`_run_one` 内吞掉单 reservation 的异常。
    2. 单次 tick try/except：在本函数内吞掉 :func:`tick` 自身的意外异常，
       让循环继续到下一次 interval。
    3. 调用方 task 取消时透传 :class:`asyncio.CancelledError`，自然退出。
    """
    semaphore = asyncio.Semaphore(max_inflight)
    inflight: set[asyncio.Task[None]] = set()

    logger.info(
        "scheduler ticker loop started (interval=%.2fs, max_inflight=%d)",
        interval,
        max_inflight,
    )
    try:
        while not stop_event.is_set():
            try:
                await tick(store, bridge, semaphore=semaphore, inflight=inflight)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ticker loop: unexpected tick error; will retry next interval")

            # 正常 interval 到期返回；TimeoutError 表示"interval 到了，继续下一次 tick"。
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
    finally:
        # 优雅退出：等所有 in-flight 完成（带 shutdown_timeout），超时强制 cancel。
        await _drain_inflight(inflight, shutdown_timeout=shutdown_timeout)
        logger.info("scheduler ticker loop stopped")


async def _drain_inflight(
    inflight: set[asyncio.Task[None]],
    *,
    shutdown_timeout: float,
) -> None:
    """等所有 in-flight 任务收尾；超时则 ``cancel`` 剩下的并再次 ``gather``。

    实现细节：用 ``list(inflight)`` 快照避免迭代时 done callback 改集合。
    """
    if not inflight:
        return

    pending = list(inflight)
    logger.info(
        "ticker stopping; waiting for %d in-flight task(s) (timeout=%.1fs)",
        len(pending),
        shutdown_timeout,
    )
    try:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            timeout=shutdown_timeout,
        )
    except TimeoutError:
        # 取还没 done 的快照
        still_running = [t for t in pending if not t.done()]
        if still_running:
            logger.warning(
                "ticker shutdown timeout; %d task(s) still running, cancelling",
                len(still_running),
            )
            for t in still_running:
                t.cancel()
            # 再 gather 一次，吞 CancelledError；保证函数返回前没有挂着的 task。
            await asyncio.gather(*still_running, return_exceptions=True)


__all__ = ["run_ticker_loop", "tick"]
