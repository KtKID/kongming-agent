"""unit：scheduler.manager 门户创建定时任务专属 thread。

覆盖：
- 成功路径：先创建 thread，再把 thread_id / delivery.target 写入 task。
- 回滚路径：store 写入失败时删除刚创建的 thread。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scheduler.domain import (
    DeliveryChannel,
    ScheduleDelivery,
    ScheduledTask,
    ScheduleTrigger,
    TaskExecutionPolicy,
    TaskOrigin,
    TaskState,
    TaskTarget,
    TriggerType,
)
from scheduler.manager import SchedulerManager
from scheduler.store import Store


class _FakeThreadProvisioner:
    """测试用 thread provisioner：记录创建和删除调用。"""

    def __init__(self) -> None:
        self.created: list[dict[str, str]] = []
        self.deleted: list[str] = []

    async def create_scheduled_task_thread(
        self,
        *,
        task_id: str,
        name: str,
        preset_id: str,
        cwd: str = "",
    ) -> str:
        self.created.append({"task_id": task_id, "name": name, "preset_id": preset_id, "cwd": cwd})
        return "thread-aaaaaaaaaaaa"

    async def delete_thread(self, thread_id: str, *, keep_history: bool = False) -> None:
        self.deleted.append(f"{thread_id}:{keep_history}")


class _InvalidThreadProvisioner(_FakeThreadProvisioner):
    """返回非法 thread id，用于覆盖 bind 阶段补偿。"""

    async def create_scheduled_task_thread(
        self,
        *,
        task_id: str,
        name: str,
        preset_id: str,
        cwd: str = "",
    ) -> str:
        self.created.append({"task_id": task_id, "name": name, "preset_id": preset_id, "cwd": cwd})
        return "bad-thread-id"


def _make_task(task_id: str = "task-1") -> ScheduledTask:
    """构造最小合法任务。"""
    return ScheduledTask(
        task_id=task_id,
        name="weekly summary",
        enabled=True,
        state=TaskState.SCHEDULED,
        origin=TaskOrigin.WEB,
        trigger=ScheduleTrigger(
            trigger_type=TriggerType.CRON,
            expr="0 9 * * *",
            timezone="UTC",
        ),
        policy=TaskExecutionPolicy(),
        target=TaskTarget(agent_name="default", input_text="summarize"),
        next_run_at="2026-06-16T01:00:00+00:00",
        last_run_at=None,
        created_by="web",
        created_at="2026-06-15T01:00:00+00:00",
        updated_at="2026-06-15T01:00:00+00:00",
        delivery=ScheduleDelivery(channel=DeliveryChannel.WEB),
        preset_id="preset-default",
    )


@pytest.mark.asyncio
async def test_create_task_with_thread_binds_thread_and_delivery(tmp_path: Path) -> None:
    store = Store(tmp_path)
    provisioner = _FakeThreadProvisioner()
    manager = SchedulerManager(store, thread_provisioner=provisioner)

    created = await manager.create_task_with_thread(_make_task(), cwd="/work")

    assert created.thread_id == "thread-aaaaaaaaaaaa"
    assert created.delivery is not None
    assert created.delivery.target == "thread:thread-aaaaaaaaaaaa"
    assert provisioner.created == [
        {
            "task_id": "task-1",
            "name": "weekly summary",
            "preset_id": "preset-default",
            "cwd": "/work",
        }
    ]
    assert provisioner.deleted == []
    fetched = store.get_task("task-1")
    assert fetched is not None
    assert fetched.thread_id == "thread-aaaaaaaaaaaa"


@pytest.mark.asyncio
async def test_create_task_with_thread_can_use_separate_thread_preset(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    provisioner = _FakeThreadProvisioner()
    manager = SchedulerManager(store, thread_provisioner=provisioner)
    task = _make_task()
    task = ScheduledTask(
        task_id=task.task_id,
        name=task.name,
        enabled=task.enabled,
        state=task.state,
        origin=task.origin,
        trigger=task.trigger,
        policy=task.policy,
        target=task.target,
        next_run_at=task.next_run_at,
        last_run_at=task.last_run_at,
        created_by=task.created_by,
        created_at=task.created_at,
        updated_at=task.updated_at,
        delivery=task.delivery,
        preset_id="",
    )

    created = await manager.create_task_with_thread(
        task,
        thread_preset_id="preset-default",
    )

    assert created.preset_id == ""
    assert provisioner.created[0]["preset_id"] == "preset-default"


@pytest.mark.asyncio
async def test_create_task_with_thread_rolls_back_thread_on_store_failure(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.create_task(_make_task("task-1"))
    provisioner = _FakeThreadProvisioner()
    manager = SchedulerManager(store, thread_provisioner=provisioner)

    with pytest.raises(ValueError):
        await manager.create_task_with_thread(_make_task("task-1"))

    assert provisioner.created
    assert provisioner.deleted == ["thread-aaaaaaaaaaaa:False"]


@pytest.mark.asyncio
async def test_create_task_with_thread_rolls_back_thread_on_bind_failure(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    provisioner = _InvalidThreadProvisioner()
    manager = SchedulerManager(store, thread_provisioner=provisioner)

    with pytest.raises(ValueError):
        await manager.create_task_with_thread(_make_task("task-1"))

    assert provisioner.created
    assert provisioner.deleted == ["bad-thread-id:False"]
