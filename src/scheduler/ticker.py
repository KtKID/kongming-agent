"""scheduler ticker：只负责领取 reservation 并提交给 live owner。

关键流程：
- ``tick`` 调 Store.reserve_due_tasks 原子领取到期窗口。
- 每个 reservation 只调用 ``submit_scheduled_run``，获得业务回执后立即继续。
- live Task、并发闸门、取消与 shutdown 全部归 ScheduledRunManager。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from scheduler.run_portal import ScheduledRunSubmitter
from scheduler.store import Store
from scheduler.timing import to_iso, utc_now

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL = 1.0


async def tick(
    store: Store,
    scheduled_run_manager: ScheduledRunSubmitter,
    *,
    now: str | None = None,
) -> dict[str, int]:
    """领取一次到期窗口并提交；返回 due 与成功提交数量。"""
    tick_at = now or to_iso(utc_now())
    try:
        reservations = store.reserve_due_tasks(now=tick_at)
    except Exception as exc:
        logger.exception("tick: reserve_due_tasks failed")
        payload = {
            "tick_at": tick_at,
            "due_count": 0,
            "spawned": 0,
            "error": str(exc),
        }
        store.write_ticker_status(status="error", payload=payload)
        store.append_incident(
            action="tick_failed",
            task_id=None,
            actor="ticker",
            payload={"tick_at": tick_at, "error": str(exc)},
        )
        return {"due_count": 0, "spawned": 0}

    submitted = 0
    for reservation in reservations:
        try:
            await scheduled_run_manager.submit_scheduled_run(reservation)
            submitted += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "tick: task %s submit failed",
                reservation.task.task_id,
            )
            store.append_incident(
                action="tick_submit_error",
                task_id=reservation.task.task_id,
                actor="ticker",
                payload={
                    "reservation_id": reservation.reservation_id,
                    "tick_at": tick_at,
                    "error": str(exc),
                },
            )

    payload = {
        "tick_at": tick_at,
        "due_count": len(reservations),
        "spawned": submitted,
    }
    store.write_ticker_status(status="ok", payload=payload)
    if reservations:
        store.append_audit(
            action="tick_dispatched",
            task_id=None,
            actor="ticker",
            payload=payload,
        )
    return {"due_count": len(reservations), "spawned": submitted}


async def run_ticker_loop(
    store: Store,
    scheduled_run_manager: ScheduledRunSubmitter,
    stop_event: asyncio.Event,
    *,
    interval: float = _DEFAULT_INTERVAL,
) -> None:
    """按 interval 反复 reserve+submit；停止时直接退出触发循环。"""
    logger.info("scheduler ticker loop started (interval=%.2fs)", interval)
    try:
        while not stop_event.is_set():
            try:
                await tick(store, scheduled_run_manager)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ticker loop: unexpected tick error; will retry next interval")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
    finally:
        logger.info("scheduler ticker loop stopped")


__all__ = ["ScheduledRunSubmitter", "run_ticker_loop", "tick"]
