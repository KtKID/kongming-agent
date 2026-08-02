"""scheduler 业务门户。

本模块收口 scheduler 跨模块业务流程。当前主职责是创建带专属 thread 的
定时任务：先通过注入的 thread provisioner 创建 thread，再写入 scheduler
store；如果 store 写入失败，回滚刚创建的 thread。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Protocol

from scheduler.domain import (
    DeliveryChannel,
    RunStatus,
    ScheduleDelivery,
    ScheduledRun,
    ScheduledTask,
    TaskRuntimeStatus,
)
from scheduler.store import Store

_THREAD_TARGET_PREFIX = "thread:"
_SCHEDULED_TASK_SOURCE_KIND = "scheduled_task"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SchedulerTaskProjection:
    """任务生命周期、最近运行结果和 live 状态的只读投影。"""

    task: ScheduledTask
    latest_run_status: RunStatus | None
    live_runtime_status: TaskRuntimeStatus


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


class ScheduledRunLiveReader(Protocol):
    """定时任务 live owner 的只读投影合同。"""

    def has_live_task(self, task_id: str) -> bool:
        """返回指定 task 是否存在 pending/running run。"""
        ...


class SchedulerManager:
    """scheduler 模块对外业务门户。"""

    def __init__(
        self,
        store: Store,
        *,
        thread_provisioner: ScheduleThreadProvisioner | None = None,
        live_reader: ScheduledRunLiveReader | None = None,
    ) -> None:
        self._store = store
        self._thread_provisioner = thread_provisioner
        self._live_reader = live_reader

    def bind_live_reader(self, live_reader: ScheduledRunLiveReader) -> None:
        """绑定进程内唯一 ScheduledRunManager，供面板读取真实 live 状态。"""
        self._live_reader = live_reader

    @property
    def store(self) -> Store:
        """当前 manager 操作的 scheduler store。"""
        return self._store

    def recover_stale_runs_on_startup(self) -> int:
        """启动期收口旧进程遗留的 RUNNING run。

        返回被标记为 abandoned 的 run 数；调用方应在 ticker / run_now 入口暴露前调用。
        """
        return self._store.recover_stale_runs()

    def project_task(self, task: ScheduledTask) -> SchedulerTaskProjection:
        """从 task owner 与 run owner 组合稳定展示投影。"""
        runs = self._store.list_runs(task.task_id, limit=None)
        latest_terminal = next(
            (run for run in reversed(runs) if run.status is not RunStatus.RUNNING),
            None,
        )
        latest_status = latest_terminal.status if latest_terminal is not None else None
        has_live_run = (
            self._live_reader.has_live_task(task.task_id)
            if self._live_reader is not None
            else any(run.status is RunStatus.RUNNING for run in runs)
        )
        live_status = TaskRuntimeStatus.RUNNING if has_live_run else TaskRuntimeStatus.IDLE
        return SchedulerTaskProjection(
            task=task,
            latest_run_status=latest_status,
            live_runtime_status=live_status,
        )

    def list_task_projections(self) -> list[SchedulerTaskProjection]:
        """返回所有任务的生命周期与运行态投影。"""
        return [self.project_task(task) for task in self._store.list_tasks(include_disabled=True)]

    def get_task(self, task_id: str) -> ScheduledTask | None:
        """经 scheduler 门户读取单个 task。"""
        return self._store.get_task(task_id)

    def update_task(
        self,
        task_id: str,
        *,
        fields_to_update: dict[str, object],
    ) -> ScheduledTask:
        """经 scheduler 门户更新 task 持久化状态。"""
        return self._store.update_task(task_id, **fields_to_update)

    def delete_task(self, task_id: str) -> bool:
        """经 scheduler 门户删除 task。"""
        return self._store.delete_task(task_id)

    def request_manual_run(
        self,
        task_id: str,
        *,
        requested_at: str,
    ) -> ScheduledTask:
        """经 scheduler 门户登记手动运行请求。"""
        return self._store.request_manual_run(task_id, requested_at=requested_at)

    def list_runs(
        self,
        task_id: str,
        *,
        limit: int | None = None,
    ) -> list[ScheduledRun]:
        """经 scheduler 门户读取单 task 的逻辑 run 列表。"""
        return self._store.list_runs(task_id, limit=limit)

    def list_recent_runs(
        self,
        *,
        limit: int,
        cursor: str | None,
    ) -> list[ScheduledRun]:
        """经 scheduler 门户读取跨 task 最近 run。"""
        return self._store.list_recent_runs(limit=limit, cursor=cursor)

    def append_audit(
        self,
        *,
        action: str,
        task_id: str | None,
        actor: str,
        payload: dict[str, object],
    ) -> None:
        """经 scheduler 门户追加 task 审计。"""
        self._store.append_audit(
            action=action,
            task_id=task_id,
            actor=actor,
            payload=payload,
        )

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
            raise RuntimeError("thread manager provisioner required for scheduled task thread")

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
    "ScheduledRunLiveReader",
    "SchedulerManager",
    "SchedulerTaskProjection",
    "bind_task_to_thread",
]
