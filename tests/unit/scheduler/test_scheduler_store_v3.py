"""v0.3 cron-delivery：``Store`` 新增的 4 个查询方法测试。

覆盖：

- ``list_recent_runs(limit, since, task_id)``：跨 task 聚合 + 按 started_at
  倒序 + since 过滤
- ``mark_run_seen(run_id)``：找到→更新；找不到→None；幂等
- ``mark_all_runs_seen_until(timestamp)``：批量更新 + 不影响未来 / 已读
- ``count_unseen_runs()``：仅 ``DELIVERED + seen_at is None`` 入计数
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


def _make_task(*, task_id: str = "t-v3") -> ScheduledTask:
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
    task_id: str = "t-v3",
    started_at: str = "2026-05-04T00:00:00+00:00",
    delivery_status: DeliveryStatus = DeliveryStatus.PENDING,
    seen_at: str | None = None,
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
        seen_at=seen_at,
    )


# ---------------------------------------------------------------------------
# list_recent_runs
# ---------------------------------------------------------------------------


def test_list_recent_runs_empty_store(tmp_path: Path) -> None:
    store = Store(tmp_path)
    assert store.list_recent_runs() == []


def test_list_recent_runs_single_task_ordering(tmp_path: Path) -> None:
    """同一 task 内按 started_at 倒序。"""
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-1"))
    store.append_run(
        _make_run(run_id="r-old", task_id="t-1", started_at="2026-05-01T00:00:00+00:00")
    )
    store.append_run(
        _make_run(run_id="r-new", task_id="t-1", started_at="2026-05-04T00:00:00+00:00")
    )
    store.append_run(
        _make_run(run_id="r-mid", task_id="t-1", started_at="2026-05-02T00:00:00+00:00")
    )

    runs = store.list_recent_runs()
    ids = [r.run_id for r in runs]
    assert ids == ["r-new", "r-mid", "r-old"]


def test_list_recent_runs_cross_task_aggregation(tmp_path: Path) -> None:
    """跨 task 聚合：所有 runs/*.jsonl 都被扫到。"""
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-a"))
    store.create_task(_make_task(task_id="t-b"))
    store.append_run(
        _make_run(run_id="ra-1", task_id="t-a", started_at="2026-05-01T00:00:00+00:00")
    )
    store.append_run(
        _make_run(run_id="rb-1", task_id="t-b", started_at="2026-05-02T00:00:00+00:00")
    )

    runs = store.list_recent_runs()
    ids = [r.run_id for r in runs]
    # 倒序：t-b 比 t-a 新
    assert ids == ["rb-1", "ra-1"]


def test_list_recent_runs_task_id_filter(tmp_path: Path) -> None:
    """``task_id`` 限定单 task 范围。"""
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-a"))
    store.create_task(_make_task(task_id="t-b"))
    store.append_run(
        _make_run(run_id="ra-1", task_id="t-a", started_at="2026-05-01T00:00:00+00:00")
    )
    store.append_run(
        _make_run(run_id="rb-1", task_id="t-b", started_at="2026-05-02T00:00:00+00:00")
    )

    runs = store.list_recent_runs(task_id="t-a")
    assert [r.run_id for r in runs] == ["ra-1"]


def test_list_recent_runs_since_filter(tmp_path: Path) -> None:
    """``since`` 过滤：只保留 started_at >= since 的 run。"""
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-1"))
    store.append_run(
        _make_run(run_id="r-2025", task_id="t-1", started_at="2025-01-01T00:00:00+00:00")
    )
    store.append_run(
        _make_run(run_id="r-2026", task_id="t-1", started_at="2026-05-04T00:00:00+00:00")
    )

    runs = store.list_recent_runs(since="2026-01-01T00:00:00+00:00")
    assert [r.run_id for r in runs] == ["r-2026"]


def test_list_recent_runs_limit(tmp_path: Path) -> None:
    """``limit`` 取前 N 条（倒序后切片）。"""
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-1"))
    for i in range(10):
        store.append_run(
            _make_run(
                run_id=f"r-{i:02d}",
                task_id="t-1",
                started_at=f"2026-05-04T00:00:{i:02d}+00:00",
            )
        )

    runs = store.list_recent_runs(limit=3)
    # 倒序后取前 3：r-09, r-08, r-07
    assert [r.run_id for r in runs] == ["r-09", "r-08", "r-07"]


def test_list_recent_runs_limit_zero_or_negative_raises(tmp_path: Path) -> None:
    """``limit <= 0`` 越界（web-cron-router-v0.1 收紧契约）→ ``ValueError``。

    v0.3 旧契约 "limit <= 0 不限制" 已被 web-cron-router-v0.1 替换为严格
    1..200；越界值（``0`` / ``-1`` / ``> 200``）一律抛 ``ValueError``。
    详见 ``tests/unit/scheduler/test_store_recent_runs.py``。
    """
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-1"))
    with pytest.raises(ValueError, match="limit"):
        store.list_recent_runs(limit=0)


# ---------------------------------------------------------------------------
# mark_run_seen
# ---------------------------------------------------------------------------


def test_mark_run_seen_not_found_returns_none(tmp_path: Path) -> None:
    store = Store(tmp_path)
    assert store.mark_run_seen("missing-id") is None


def test_mark_run_seen_marks_unseen_run(tmp_path: Path) -> None:
    """未读 run → 写入 seen_at；返回更新后的 run。"""
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-1"))
    store.append_run(
        _make_run(run_id="r-1", task_id="t-1", delivery_status=DeliveryStatus.DELIVERED)
    )

    updated = store.mark_run_seen("r-1")
    assert updated is not None
    assert updated.seen_at is not None  # ISO 时间字符串

    # 重建 store 验证落盘
    store2 = Store(tmp_path)
    fetched = store2.get_latest_run("t-1")
    assert fetched is not None
    assert fetched.seen_at is not None


def test_mark_run_seen_idempotent(tmp_path: Path) -> None:
    """已读 run → 不重复写盘；返回原 run。"""
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-1"))
    store.append_run(
        _make_run(
            run_id="r-1",
            task_id="t-1",
            delivery_status=DeliveryStatus.DELIVERED,
            seen_at="2026-05-04T09:00:00+00:00",
        )
    )

    updated = store.mark_run_seen("r-1")
    assert updated is not None
    assert updated.seen_at == "2026-05-04T09:00:00+00:00"  # 没被改


# ---------------------------------------------------------------------------
# mark_all_runs_seen_until
# ---------------------------------------------------------------------------


def test_mark_all_runs_seen_until_marks_old_unseen_runs(tmp_path: Path) -> None:
    """before until_ts 的未读 → 标记为已读；之后的不动。"""
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-1"))
    store.append_run(
        _make_run(
            run_id="r-old",
            task_id="t-1",
            started_at="2026-05-01T00:00:00+00:00",
            delivery_status=DeliveryStatus.DELIVERED,
        )
    )
    store.append_run(
        _make_run(
            run_id="r-new",
            task_id="t-1",
            started_at="2026-05-10T00:00:00+00:00",
            delivery_status=DeliveryStatus.DELIVERED,
        )
    )

    updated_count = store.mark_all_runs_seen_until("2026-05-05T00:00:00+00:00")
    assert updated_count == 1

    # r-old 已读；r-new 未读
    runs_by_id = {r.run_id: r for r in store.list_recent_runs()}
    assert runs_by_id["r-old"].seen_at is not None
    assert runs_by_id["r-new"].seen_at is None


def test_mark_all_runs_seen_until_skips_already_seen(tmp_path: Path) -> None:
    """已读 run 不被重复更新。"""
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-1"))
    store.append_run(
        _make_run(
            run_id="r-already",
            task_id="t-1",
            started_at="2026-05-01T00:00:00+00:00",
            delivery_status=DeliveryStatus.DELIVERED,
            seen_at="2026-05-02T00:00:00+00:00",
        )
    )

    updated_count = store.mark_all_runs_seen_until("2026-05-10T00:00:00+00:00")
    assert updated_count == 0  # 已读的不计


# ---------------------------------------------------------------------------
# count_unseen_runs
# ---------------------------------------------------------------------------


def test_count_unseen_runs_empty_store(tmp_path: Path) -> None:
    store = Store(tmp_path)
    assert store.count_unseen_runs() == 0


def test_count_unseen_runs_only_delivered_unseen_count(tmp_path: Path) -> None:
    """计数口径：仅 ``delivery_status==DELIVERED and seen_at is None``。"""
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-1"))

    # 这条计数：delivered + 未读
    store.append_run(
        _make_run(
            run_id="r-counted",
            task_id="t-1",
            delivery_status=DeliveryStatus.DELIVERED,
        )
    )
    # 这条不计：已读
    store.append_run(
        _make_run(
            run_id="r-seen",
            task_id="t-1",
            delivery_status=DeliveryStatus.DELIVERED,
            seen_at="2026-05-04T09:00:00+00:00",
        )
    )
    # 这条不计：pending
    store.append_run(
        _make_run(
            run_id="r-pending",
            task_id="t-1",
            delivery_status=DeliveryStatus.PENDING,
        )
    )
    # 这条不计：skipped
    store.append_run(
        _make_run(
            run_id="r-skipped",
            task_id="t-1",
            delivery_status=DeliveryStatus.SKIPPED,
        )
    )
    # 这条不计：failed
    store.append_run(
        _make_run(
            run_id="r-failed",
            task_id="t-1",
            delivery_status=DeliveryStatus.FAILED,
        )
    )

    assert store.count_unseen_runs() == 1


def test_count_unseen_runs_after_mark_seen_decrements(tmp_path: Path) -> None:
    """mark_run_seen 后 count 减 1。"""
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-1"))
    store.append_run(
        _make_run(
            run_id="r-1",
            task_id="t-1",
            delivery_status=DeliveryStatus.DELIVERED,
        )
    )
    store.append_run(
        _make_run(
            run_id="r-2",
            task_id="t-1",
            delivery_status=DeliveryStatus.DELIVERED,
        )
    )
    assert store.count_unseen_runs() == 2

    store.mark_run_seen("r-1")
    assert store.count_unseen_runs() == 1
