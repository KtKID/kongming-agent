"""v0.3 cron-delivery domain 单元测试。

覆盖 ``ScheduleDelivery`` / ``DeliveryChannel`` / ``DeliveryStatus`` /
``TaskLifecycleState.DELETED`` 的构造与不变量校验。

不测序列化——序列化路径在 ``test_scheduler_store.py`` 通过 round-trip 间接覆盖
（v0.2 起的约定：dataclass frozen + store 集中负责落盘）。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from scheduler.domain import (
    DeliveryChannel,
    DeliveryStatus,
    ScheduleDelivery,
    ScheduledRun,
    ScheduledTask,
    ScheduleTrigger,
    SessionMode,
    TaskExecutionPolicy,
    TaskLifecycleState,
    TaskOrigin,
    TaskTarget,
    TriggerType,
)

# ---------------------------------------------------------------------------
# DeliveryChannel
# ---------------------------------------------------------------------------


def test_delivery_channel_members():
    """v0.3 第一版仅含 web / cli 两个 channel；远端 channel 留扩展点不实现。"""
    assert DeliveryChannel.WEB.value == "web"
    assert DeliveryChannel.CLI.value == "cli"
    assert {c.value for c in DeliveryChannel} == {"web", "cli"}


def test_delivery_channel_str_compat():
    """:class:`DeliveryChannel` 继承 StrEnum，能直接做字符串比较，便于落盘。"""
    assert DeliveryChannel.WEB == "web"
    assert DeliveryChannel("cli") is DeliveryChannel.CLI


def test_delivery_channel_invalid_value_rejected():
    """未在枚举中的值直接抛 ValueError。"""
    with pytest.raises(ValueError):
        DeliveryChannel("feishu")


# ---------------------------------------------------------------------------
# DeliveryStatus
# ---------------------------------------------------------------------------


def test_delivery_status_members():
    """v0.3：4 个状态覆盖 run 完成 → 投递阶段的全部分支。"""
    expected = {"pending", "delivered", "failed", "skipped"}
    assert {s.value for s in DeliveryStatus} == expected


def test_delivery_status_str_compat():
    assert DeliveryStatus.DELIVERED == "delivered"
    assert DeliveryStatus("skipped") is DeliveryStatus.SKIPPED


# ---------------------------------------------------------------------------
# ScheduleDelivery
# ---------------------------------------------------------------------------


def test_schedule_delivery_construct_web():
    """构造 web channel 配置；只校验类型，不校验业务语义。"""
    d = ScheduleDelivery(channel=DeliveryChannel.WEB)
    assert d.channel is DeliveryChannel.WEB


def test_schedule_delivery_construct_cli():
    d = ScheduleDelivery(channel=DeliveryChannel.CLI)
    assert d.channel is DeliveryChannel.CLI


def test_schedule_delivery_invalid_channel_type_raises():
    """``channel`` 必须是 :class:`DeliveryChannel`；str 不接受（避免误传）。"""
    with pytest.raises(TypeError, match="channel must be DeliveryChannel"):
        ScheduleDelivery(channel="web")  # type: ignore[arg-type]


def test_schedule_delivery_is_frozen():
    """frozen dataclass：mutate 抛 FrozenInstanceError。"""
    d = ScheduleDelivery(channel=DeliveryChannel.WEB)
    with pytest.raises(FrozenInstanceError):
        d.channel = DeliveryChannel.CLI  # type: ignore[misc]


def test_schedule_delivery_equality_by_value():
    """两个相同 channel 的 ScheduleDelivery 相等（dataclass 默认行为）。"""
    a = ScheduleDelivery(channel=DeliveryChannel.WEB)
    b = ScheduleDelivery(channel=DeliveryChannel.WEB)
    c = ScheduleDelivery(channel=DeliveryChannel.CLI)
    assert a == b
    assert a != c


# ---------------------------------------------------------------------------
# TaskLifecycleState.DELETED
# ---------------------------------------------------------------------------


def test_task_state_has_deleted_member():
    """v0.3 新增 DELETED 状态用于软删除。"""
    assert TaskLifecycleState.DELETED.value == "deleted"


def test_task_lifecycle_deleted_is_valid():
    """``DELETED`` 是保留历史的软删除生命周期。"""
    task = _make_task(lifecycle=TaskLifecycleState.DELETED)
    assert task.lifecycle is TaskLifecycleState.DELETED


def test_task_lifecycle_exhausted_is_valid():
    """``EXHAUSTED`` 表示 one-shot 已被原子领取。"""
    task = _make_task(lifecycle=TaskLifecycleState.EXHAUSTED)
    assert task.lifecycle is TaskLifecycleState.EXHAUSTED


# ---------------------------------------------------------------------------
# ScheduledTask.delivery
# ---------------------------------------------------------------------------


def test_scheduled_task_delivery_default_none():
    """默认 ``delivery=None`` 兼容 v0.2 任务。"""
    task = _make_task()
    assert task.delivery is None


def test_scheduled_task_delivery_explicit_web():
    delivery = ScheduleDelivery(channel=DeliveryChannel.WEB)
    task = _make_task(delivery=delivery)
    assert task.delivery is delivery


def test_scheduled_task_delivery_invalid_type_raises():
    with pytest.raises(TypeError, match="delivery must be ScheduleDelivery"):
        _make_task(delivery="web")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ScheduledRun.delivered_at / delivery_status / seen_at
# ---------------------------------------------------------------------------


def test_scheduled_run_default_pending_status():
    """新建 run 默认 ``delivery_status=PENDING``，未投递未读。"""
    run = _make_run()
    assert run.delivery_status is DeliveryStatus.PENDING
    assert run.delivered_at is None
    assert run.seen_at is None


def test_scheduled_run_delivered_state():
    run = _make_run(
        delivery_status=DeliveryStatus.DELIVERED,
        delivered_at="2026-05-04T01:00:00+00:00",
    )
    assert run.delivery_status is DeliveryStatus.DELIVERED
    assert run.delivered_at == "2026-05-04T01:00:00+00:00"


def test_scheduled_run_seen_state():
    run = _make_run(
        delivery_status=DeliveryStatus.DELIVERED,
        delivered_at="2026-05-04T01:00:00+00:00",
        seen_at="2026-05-04T09:00:00+00:00",
    )
    assert run.seen_at == "2026-05-04T09:00:00+00:00"


def test_scheduled_run_delivery_status_must_be_enum():
    with pytest.raises(TypeError, match="delivery_status must be DeliveryStatus"):
        _make_run(delivery_status="delivered")  # type: ignore[arg-type]


def test_scheduled_run_delivered_at_must_be_str_or_none():
    with pytest.raises(TypeError, match="delivered_at"):
        _make_run(delivered_at=12345)  # type: ignore[arg-type]


def test_scheduled_run_seen_at_must_be_str_or_none():
    with pytest.raises(TypeError, match="seen_at"):
        _make_run(seen_at=12345)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# helpers（保持本测试文件自包含；不复用 test_scheduler_store.py 的内部 helper）
# ---------------------------------------------------------------------------


def _make_task(
    *,
    task_id: str = "t-d",
    lifecycle: TaskLifecycleState = TaskLifecycleState.SCHEDULED,
    delivery: ScheduleDelivery | None = None,
) -> ScheduledTask:
    return ScheduledTask(
        task_id=task_id,
        name=f"task-{task_id}",
        lifecycle=lifecycle,
        origin=TaskOrigin.CLI,
        trigger=ScheduleTrigger(trigger_type=TriggerType.INTERVAL, expr="10", timezone="UTC"),
        policy=TaskExecutionPolicy(
            session_mode=SessionMode.FRESH_SESSION,
            concurrency_policy=__import__(
                "scheduler.domain", fromlist=["ConcurrencyPolicy"]
            ).ConcurrencyPolicy.FORBID,
            misfire_policy=__import__(
                "scheduler.domain", fromlist=["MisfirePolicy"]
            ).MisfirePolicy.SKIP,
        ),
        target=TaskTarget(agent_name="default", input_text="ping", metadata={}),
        next_run_at=None,
        last_run_at=None,
        created_by="tester",
        created_at="2026-05-04T00:00:00+00:00",
        updated_at="2026-05-04T00:00:00+00:00",
        delivery=delivery,
    )


def _make_run(
    *,
    run_id: str = "r-d",
    task_id: str = "t-d",
    delivery_status: DeliveryStatus = DeliveryStatus.PENDING,
    delivered_at: str | None = None,
    seen_at: str | None = None,
) -> ScheduledRun:
    from scheduler.domain import RunStatus

    return ScheduledRun(
        run_id=run_id,
        task_id=task_id,
        status=RunStatus.RUNNING,
        scheduled_for="2026-05-04T00:00:00+00:00",
        started_at="2026-05-04T00:00:00+00:00",
        finished_at=None,
        session_id=f"sess-{run_id}",
        result_status=None,
        final_message_excerpt=None,
        error_message=None,
        failure_reason=None,
        delivery_error=None,
        silent_suppressed=False,
        delivered_at=delivered_at,
        delivery_status=delivery_status,
        seen_at=seen_at,
    )
