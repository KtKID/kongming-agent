"""scheduler 业务门户。

本模块收口 scheduler 跨模块业务流程。当前主职责是创建带专属 thread 的
定时任务：先通过注入的 thread provisioner 创建 thread，再写入 scheduler
store；如果 store 写入失败，回滚刚创建的 thread。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Protocol

from scheduler.domain import DeliveryChannel, ScheduleDelivery, ScheduledTask
from scheduler.store import Store

_THREAD_TARGET_PREFIX = "thread:"
_SCHEDULED_TASK_SOURCE_KIND = "scheduled_task"

logger = logging.getLogger(__name__)


class ScheduleThreadProvisioner(Protocol):
    """定时任务 thread provisioner 协议。

    scheduler 模块只依赖这个协议，具体 thread 创建由 Web ThreadManager 或
    测试 fake 实现。
    """

    async def create_scheduled_task_thread(
        self,
        *,
        task_id: str,
        name: str,
        preset_id: str,
        cwd: str = "",
    ) -> str:
        """创建定时任务专属 thread 并返回 thread id。"""
        ...

    async def delete_thread(self, thread_id: str, *, keep_history: bool = False) -> None:
        """删除刚创建但尚未成功绑定任务的 thread。"""
        ...


class SchedulerManager:
    """scheduler 模块对外业务门户。"""

    def __init__(
        self,
        store: Store,
        *,
        thread_provisioner: ScheduleThreadProvisioner | None = None,
    ) -> None:
        self._store = store
        self._thread_provisioner = thread_provisioner

    @property
    def store(self) -> Store:
        """当前 manager 操作的 scheduler store。"""
        return self._store

    async def create_task_with_thread(
        self,
        task: ScheduledTask,
        *,
        cwd: str = "",
        thread_preset_id: str | None = None,
    ) -> ScheduledTask:
        """创建带专属 thread 的定时任务。

        Args:
            task: 已完成 trigger / policy / target 校验的任务对象。
            cwd: 专属 thread 的 workspace cwd；空串表示沿用 ThreadManager 默认。
            thread_preset_id: 专属 thread 使用的 preset。``None`` 表示沿用
                ``task.preset_id``；可用于保留任务默认模型语义，同时满足 Web
                generic_chat thread 的 preset 必填约束。

        Returns:
            已落盘的任务对象，包含 ``thread_id`` 与 ``delivery.target``。

        Raises:
            RuntimeError: 未注入 thread provisioner。
            ValueError: Store 拒绝创建任务或 thread id 不合法。
        """
        if self._thread_provisioner is None:
            raise RuntimeError("thread_provisioner required for scheduled task thread")

        thread_id = await self._thread_provisioner.create_scheduled_task_thread(
            task_id=task.task_id,
            name=task.name,
            preset_id=task.preset_id if thread_preset_id is None else thread_preset_id,
            cwd=cwd,
        )
        try:
            bound_task = bind_task_to_thread(task, thread_id=thread_id)
            return self._store.create_task(bound_task)
        except Exception:
            rollback_succeeded = False
            try:
                await self._thread_provisioner.delete_thread(thread_id, keep_history=False)
                rollback_succeeded = True
            except Exception as rollback_exc:
                logger.warning(
                    "failed to rollback scheduled task thread task_id=%s thread_id=%s: %s",
                    task.task_id,
                    thread_id,
                    rollback_exc,
                    exc_info=True,
                )
            if rollback_succeeded:
                logger.debug(
                    "rolled back scheduled task thread after create failure "
                    "task_id=%s thread_id=%s",
                    task.task_id,
                    thread_id,
                )
            raise


def bind_task_to_thread(task: ScheduledTask, *, thread_id: str) -> ScheduledTask:
    """返回绑定到指定 thread 的 ``ScheduledTask`` 副本。"""
    delivery = task.delivery
    if delivery is None:
        delivery = ScheduleDelivery(
            channel=DeliveryChannel.WEB,
            target=f"{_THREAD_TARGET_PREFIX}{thread_id}",
        )
    else:
        delivery = replace(delivery, target=f"{_THREAD_TARGET_PREFIX}{thread_id}")
    return replace(task, thread_id=thread_id, delivery=delivery)


__all__ = [
    "ScheduleThreadProvisioner",
    "SchedulerManager",
    "bind_task_to_thread",
]
