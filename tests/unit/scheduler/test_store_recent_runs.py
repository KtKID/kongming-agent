"""unit：``Store.list_recent_runs(limit, cursor)`` 跨 task 全局查询测试
（web-cron-router-v0.1 · #2 P0）。

覆盖矩阵（dev-pipeline/tasks/web-cron-router-v0.1/dev-checklist.md #2）：

1. 空 store：返回 ``[]``
2. 单 task 多 run：按 ``started_at`` 倒序
3. 多 task 跨任务排序：全局倒序 + ``limit`` 截断 + ``limit`` 不溢出
4. cursor 分页边界：先取 N 条 → 用最后一条 ``started_at`` 作 cursor → 下一页不重叠
5. limit 越界：``0`` / ``300`` 抛 ``ValueError``

测试规范：
- 每条用例独立 ``tmp_path`` 隔离；不共享 ``Store`` 单例
- ``ScheduledRun.started_at`` 是 ISO8601 字符串；可直接字典序比较
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scheduler.domain import (
    ConcurrencyPolicy,
    DeliveryChannel,
    DeliveryStatus,
    MisfirePolicy,
    RunStatus,
    ScheduleDelivery,
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
from scheduler.store import Store

# ---------------------------------------------------------------------------
# fixture helpers（复用 test_scheduler_store_v3 的轻量风格）
# ---------------------------------------------------------------------------


def _make_task(*, task_id: str) -> ScheduledTask:
    return ScheduledTask(
        task_id=task_id,
        name=f"task-{task_id}",
        enabled=True,
        state=TaskState.SCHEDULED,
        origin=TaskOrigin.TOOL,
        trigger=ScheduleTrigger(trigger_type=TriggerType.INTERVAL, expr="10", timezone="UTC"),
        policy=TaskExecutionPolicy(
            session_mode=SessionMode.FRESH_SESSION,
            concurrency_policy=ConcurrencyPolicy.FORBID,
            misfire_policy=MisfirePolicy.SKIP,
        ),
        target=TaskTarget(agent_name="default", input_text="ping", metadata={}),
        next_run_at=None,
        last_run_at=None,
        created_by="tester",
        created_at="2026-05-04T00:00:00+00:00",
        updated_at="2026-05-04T00:00:00+00:00",
        delivery=ScheduleDelivery(channel=DeliveryChannel.WEB),
    )


def _make_run(
    *,
    run_id: str,
    task_id: str,
    started_at: str,
    delivery_status: DeliveryStatus = DeliveryStatus.PENDING,
) -> ScheduledRun:
    return ScheduledRun(
        run_id=run_id,
        task_id=task_id,
        status=RunStatus.COMPLETED,
        scheduled_for=started_at,
        started_at=started_at,
        finished_at=started_at,
        session_id=f"sess-{run_id}",
        result_status="completed",
        final_message_excerpt="ok",
        error_message=None,
        failure_reason=None,
        delivery_error=None,
        silent_suppressed=False,
        delivered_at=started_at if delivery_status == DeliveryStatus.DELIVERED else None,
        delivery_status=delivery_status,
    )


# ---------------------------------------------------------------------------
# 场景 1：空 store
# ---------------------------------------------------------------------------


def test_empty_store_returns_empty_list(tmp_path: Path) -> None:
    """完全空的 store（没有 tasks、没有 runs）应该返回 ``[]``。"""
    store = Store(tmp_path)
    assert store.list_recent_runs() == []


# ---------------------------------------------------------------------------
# 场景 2：单 task 多 run，按 started_at 倒序
# ---------------------------------------------------------------------------


def test_single_task_multiple_runs_descending_by_started_at(tmp_path: Path) -> None:
    """1 个 task 5 条 run（不同 started_at）→ 返回按 started_at 倒序的 5 条。"""
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-1"))

    # 故意乱序 append，验证排序键生效（不是写盘顺序）
    timestamps = [
        "2026-05-04T12:00:00+00:00",
        "2026-05-04T10:00:00+00:00",
        "2026-05-04T14:00:00+00:00",
        "2026-05-04T09:00:00+00:00",
        "2026-05-04T11:00:00+00:00",
    ]
    for i, ts in enumerate(timestamps):
        store.append_run(_make_run(run_id=f"r-{i}", task_id="t-1", started_at=ts))

    runs = store.list_recent_runs()
    started_ats = [r.started_at for r in runs]

    assert len(runs) == 5
    # 严格倒序：最新的在最前
    assert started_ats == sorted(timestamps, reverse=True)


# ---------------------------------------------------------------------------
# 场景 3：多 task 跨任务排序
# ---------------------------------------------------------------------------


def test_cross_task_global_ordering_with_limit(tmp_path: Path) -> None:
    """3 个 task 各 3 条 run，时间交错 → 返回全局倒序 9 条；
    ``limit=5`` 截断；``limit=20`` 不溢出（实际只有 9 条）。
    """
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-a"))
    store.create_task(_make_task(task_id="t-b"))
    store.create_task(_make_task(task_id="t-c"))

    # 9 个时间戳交错分布：t-a/t-b/t-c 三组，时间覆盖 09:00 ~ 17:00
    schedule = [
        ("t-a", "ra-1", "2026-05-04T09:00:00+00:00"),
        ("t-b", "rb-1", "2026-05-04T10:00:00+00:00"),
        ("t-c", "rc-1", "2026-05-04T11:00:00+00:00"),
        ("t-a", "ra-2", "2026-05-04T12:00:00+00:00"),
        ("t-b", "rb-2", "2026-05-04T13:00:00+00:00"),
        ("t-c", "rc-2", "2026-05-04T14:00:00+00:00"),
        ("t-a", "ra-3", "2026-05-04T15:00:00+00:00"),
        ("t-b", "rb-3", "2026-05-04T16:00:00+00:00"),
        ("t-c", "rc-3", "2026-05-04T17:00:00+00:00"),
    ]
    for task_id, run_id, ts in schedule:
        store.append_run(_make_run(run_id=run_id, task_id=task_id, started_at=ts))

    # 默认 limit=50，全 9 条
    all_runs = store.list_recent_runs()
    assert len(all_runs) == 9
    # 验证全局倒序
    expected_ids_desc = [
        run_id for _, run_id, _ in sorted(schedule, key=lambda x: x[2], reverse=True)
    ]
    assert [r.run_id for r in all_runs] == expected_ids_desc

    # limit=5 截断到前 5 条（最新的）
    top5 = store.list_recent_runs(limit=5)
    assert len(top5) == 5
    assert [r.run_id for r in top5] == expected_ids_desc[:5]

    # limit=20 不溢出（实际只有 9 条）
    top20 = store.list_recent_runs(limit=20)
    assert len(top20) == 9
    assert [r.run_id for r in top20] == expected_ids_desc


# ---------------------------------------------------------------------------
# 场景 4：cursor 分页边界
# ---------------------------------------------------------------------------


def test_cursor_pagination_boundary_no_overlap(tmp_path: Path) -> None:
    """先取前 3 条 → 记最后一条 started_at 作 cursor →
    第二批 ``cursor`` 给定时取 ``started_at`` **严格 <** cursor 的 run。
    验证两批不重叠 + 衔接正确。
    """
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-1"))

    # 创建 8 条 run，时间严格递增
    timestamps = [f"2026-05-04T{h:02d}:00:00+00:00" for h in range(8)]  # 00:00 ~ 07:00
    for i, ts in enumerate(timestamps):
        store.append_run(_make_run(run_id=f"r-{i:02d}", task_id="t-1", started_at=ts))

    # 第一页：limit=3，无 cursor → 取前 3 条（最新的：07:00 / 06:00 / 05:00）
    page1 = store.list_recent_runs(limit=3)
    assert len(page1) == 3
    assert [r.run_id for r in page1] == ["r-07", "r-06", "r-05"]

    # 用 page1 最后一条 started_at 作 cursor
    cursor_ts = page1[-1].started_at
    assert cursor_ts == "2026-05-04T05:00:00+00:00"

    # 第二页：limit=3，cursor=05:00 → 取严格 <05:00 的（04:00 / 03:00 / 02:00）
    page2 = store.list_recent_runs(limit=3, cursor=cursor_ts)
    assert len(page2) == 3
    assert [r.run_id for r in page2] == ["r-04", "r-03", "r-02"]

    # 验证两批不重叠
    page1_ids = {r.run_id for r in page1}
    page2_ids = {r.run_id for r in page2}
    assert page1_ids & page2_ids == set()

    # 验证衔接正确：所有 page2 的 started_at 严格 < cursor
    for r in page2:
        assert r.started_at is not None
        assert r.started_at < cursor_ts

    # 第三页：limit=3，cursor=02:00 → 取 <02:00 的（01:00 / 00:00 共 2 条）
    page3 = store.list_recent_runs(limit=3, cursor=page2[-1].started_at)
    assert [r.run_id for r in page3] == ["r-01", "r-00"]

    # 第四页：cursor=00:00 → 没有更早的，返回 []
    page4 = store.list_recent_runs(limit=3, cursor=page3[-1].started_at)
    assert page4 == []


# ---------------------------------------------------------------------------
# 场景 5（附加）：limit 越界
# ---------------------------------------------------------------------------


def test_limit_zero_raises_value_error(tmp_path: Path) -> None:
    """``limit=0`` 越界（< 1）→ ``ValueError``。"""
    store = Store(tmp_path)
    with pytest.raises(ValueError, match="limit"):
        store.list_recent_runs(limit=0)


def test_limit_above_max_raises_value_error(tmp_path: Path) -> None:
    """``limit=300`` 越界（> 200）→ ``ValueError``。"""
    store = Store(tmp_path)
    with pytest.raises(ValueError, match="limit"):
        store.list_recent_runs(limit=300)


def test_limit_negative_raises_value_error(tmp_path: Path) -> None:
    """``limit=-1`` 越界 → ``ValueError``。"""
    store = Store(tmp_path)
    with pytest.raises(ValueError, match="limit"):
        store.list_recent_runs(limit=-1)
