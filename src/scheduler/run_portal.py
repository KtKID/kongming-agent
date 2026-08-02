"""定时任务 live run 跨模块协议。

Ticker 和 ScheduleTool 只依赖本模块的最小 Protocol；具体 live owner 由
``application.scheduled_runs.manager.ScheduledRunManager`` 实现并在宿主装配。
"""

from __future__ import annotations

from typing import Protocol

from scheduler.domain import (
    DueTaskReservation,
    ScheduledRun,
    ScheduledRunSubmitReceipt,
)


class ScheduledRunSubmitter(Protocol):
    """ticker 消费的快速提交合同。"""

    async def submit_scheduled_run(
        self,
        reservation: DueTaskReservation,
    ) -> ScheduledRunSubmitReceipt:
        """接收 reservation 并返回稳定业务回执。"""
        ...


class ScheduledRunPortal(ScheduledRunSubmitter, Protocol):
    """ScheduleTool 消费的提交并等待结果合同。"""

    async def wait_for_run(self, run_id: str) -> ScheduledRun:
        """等待指定业务 run 的 durable 终态。"""
        ...


__all__ = ["ScheduledRunPortal", "ScheduledRunSubmitter"]
