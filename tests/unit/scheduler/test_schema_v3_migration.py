"""v0.3 cron-delivery：schema v2→v3 迁移加强测试。

与 ``test_scheduler_store.py`` 内现有迁移用例的分工：
- 现有用例：覆盖各 detected_version（v1 / v2 / missing / unparseable / future）的
  备份后缀 + audit action 命名。
- 本文件：覆盖 v3 字段的兼容性（delivery / delivered_at / delivery_status /
  seen_at）—— 迁移后新建的任务 / run 必须能正常持久化和读出。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scheduler.domain import (
    SCHEMA_VERSION,
    DeliveryChannel,
    DeliveryStatus,
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


def _make_task_with_delivery(
    *, task_id: str, channel: DeliveryChannel | None
) -> ScheduledTask:
    delivery = ScheduleDelivery(channel=channel) if channel is not None else None
    return ScheduledTask(
        task_id=task_id,
        name=f"task-{task_id}",
        enabled=True,
        state=TaskState.SCHEDULED,
        origin=TaskOrigin.TOOL,
        trigger=ScheduleTrigger(
            trigger_type=TriggerType.INTERVAL, expr="30", timezone="UTC"
        ),
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
    run_id: str,
    task_id: str,
    delivery_status: DeliveryStatus = DeliveryStatus.PENDING,
    delivered_at: str | None = None,
    seen_at: str | None = None,
) -> ScheduledRun:
    return ScheduledRun(
        run_id=run_id,
        task_id=task_id,
        status=RunStatus.COMPLETED,
        scheduled_for="2026-05-04T00:00:00+00:00",
        started_at="2026-05-04T00:00:01+00:00",
        finished_at="2026-05-04T00:00:02+00:00",
        session_id=f"sess-{run_id}",
        result_status="completed",
        final_message_excerpt="ok",
        error_message=None,
        failure_reason=None,
        delivery_error=None,
        silent_suppressed=False,
        delivered_at=delivered_at,
        delivery_status=delivery_status,
        seen_at=seen_at,
    )


# ---------------------------------------------------------------------------
# v3 schema 字段在新建 store 中的端到端 round-trip
# ---------------------------------------------------------------------------


def test_v3_task_with_delivery_round_trip(tmp_path: Path) -> None:
    """v3 store：创建带 delivery 字段的任务 → 重建 store → 读回不丢字段。"""
    store = Store(tmp_path)
    store.create_task(_make_task_with_delivery(task_id="t-web", channel=DeliveryChannel.WEB))
    store.create_task(_make_task_with_delivery(task_id="t-cli", channel=DeliveryChannel.CLI))
    store.create_task(_make_task_with_delivery(task_id="t-none", channel=None))

    store2 = Store(tmp_path)
    fetched_web = store2.get_task("t-web")
    fetched_cli = store2.get_task("t-cli")
    fetched_none = store2.get_task("t-none")

    assert fetched_web is not None
    assert fetched_web.delivery is not None
    assert fetched_web.delivery.channel is DeliveryChannel.WEB

    assert fetched_cli is not None
    assert fetched_cli.delivery is not None
    assert fetched_cli.delivery.channel is DeliveryChannel.CLI

    assert fetched_none is not None
    assert fetched_none.delivery is None


def test_v3_run_with_delivery_fields_round_trip(tmp_path: Path) -> None:
    """v3 store：append 带新字段的 run → 重新 list_runs → 字段不丢。"""
    store = Store(tmp_path)
    store.create_task(_make_task_with_delivery(task_id="t-rt", channel=DeliveryChannel.WEB))

    run = _make_run(
        run_id="r-1",
        task_id="t-rt",
        delivery_status=DeliveryStatus.DELIVERED,
        delivered_at="2026-05-04T00:00:03+00:00",
        seen_at=None,
    )
    store.append_run(run)

    store2 = Store(tmp_path)
    runs = store2.list_runs("t-rt")
    assert len(runs) == 1
    assert runs[0].delivery_status is DeliveryStatus.DELIVERED
    assert runs[0].delivered_at == "2026-05-04T00:00:03+00:00"
    assert runs[0].seen_at is None


def test_v3_run_seen_field_round_trip(tmp_path: Path) -> None:
    """v3 store：seen_at 为非 None ISO 字符串时也能正确持久化。"""
    store = Store(tmp_path)
    store.create_task(_make_task_with_delivery(task_id="t-seen", channel=DeliveryChannel.WEB))

    run = _make_run(
        run_id="r-seen",
        task_id="t-seen",
        delivery_status=DeliveryStatus.DELIVERED,
        delivered_at="2026-05-04T00:00:03+00:00",
        seen_at="2026-05-04T09:00:00+00:00",
    )
    store.append_run(run)

    store2 = Store(tmp_path)
    fetched = store2.get_latest_run("t-seen")
    assert fetched is not None
    assert fetched.seen_at == "2026-05-04T09:00:00+00:00"


# ---------------------------------------------------------------------------
# v2 → v3 重置式迁移：详细字段验证
# ---------------------------------------------------------------------------


def test_v2_data_with_tasks_backed_up_intact(tmp_path: Path) -> None:
    """v2 数据迁移到 v3：备份文件**完整保留** v2 原始 bytes（包括所有字段）。"""
    v2_data: dict[str, Any] = {
        "schema_version": 2,
        "updated_at": "2026-05-04T00:00:00+00:00",
        "tasks": [
            {
                "task_id": "old-task",
                "name": "old-task",
                "enabled": True,
                "state": "scheduled",
                "origin": "tool",
                "trigger": {
                    "trigger_type": "cron",
                    "expr": "0 9 * * *",
                    "timezone": "Asia/Shanghai",
                },
                "policy": {},
                "target": {},
                "next_run_at": "2026-05-05T01:00:00+00:00",
                "last_run_at": None,
                "created_by": "tester",
                "created_at": "2026-05-04T00:00:00+00:00",
                "updated_at": "2026-05-04T00:00:00+00:00",
            }
        ],
    }
    (tmp_path / "scheduled_tasks.json").write_text(
        json.dumps(v2_data), encoding="utf-8"
    )

    Store(tmp_path)

    # 备份文件存在且内容完整保留 v2 数据
    backups = sorted(tmp_path.glob("scheduled_tasks.json.v2.bak.*"))
    assert len(backups) == 1
    backup_payload = json.loads(backups[0].read_text(encoding="utf-8"))
    assert backup_payload["schema_version"] == 2
    assert backup_payload["tasks"][0]["task_id"] == "old-task"
    assert backup_payload["tasks"][0]["trigger"]["expr"] == "0 9 * * *"


def test_v2_to_v3_post_migration_can_create_with_delivery(tmp_path: Path) -> None:
    """v2 → v3 迁移后，新 store 能正常接受带 delivery 字段的任务（关键回归）。"""
    (tmp_path / "scheduled_tasks.json").write_text(
        json.dumps({"schema_version": 2, "tasks": []}), encoding="utf-8"
    )

    store = Store(tmp_path)

    # 现在 schema_version 已经是 3
    current = json.loads(
        (tmp_path / "scheduled_tasks.json").read_text(encoding="utf-8")
    )
    assert current["schema_version"] == SCHEMA_VERSION

    # 能创建带 delivery 的新任务
    new_task = _make_task_with_delivery(task_id="new-v3", channel=DeliveryChannel.WEB)
    store.create_task(new_task)

    # 重建 store 验证持久化
    store2 = Store(tmp_path)
    fetched = store2.get_task("new-v3")
    assert fetched is not None
    assert fetched.delivery is not None
    assert fetched.delivery.channel is DeliveryChannel.WEB


def test_v3_to_v3_no_re_migration(tmp_path: Path) -> None:
    """已经是 v3 schema → 不应触发迁移；不产生 .bak 文件。"""
    store = Store(tmp_path)
    store.create_task(_make_task_with_delivery(task_id="t-keep", channel=DeliveryChannel.WEB))

    # 重建 store
    Store(tmp_path)

    backups = list(tmp_path.glob("scheduled_tasks.json.v*.bak.*"))
    assert backups == []


# ---------------------------------------------------------------------------
# legacy v2 任务（缺 delivery 字段）兼容性：靠 _dict_to_task 的 default
# ---------------------------------------------------------------------------


def test_legacy_task_dict_without_delivery_field_falls_back_to_none(
    tmp_path: Path,
) -> None:
    """模拟 legacy task dict（缺 delivery key）→ _dict_to_task 应填 None。

    场景：v2 数据本来不应该出现在 v3 store（已被重置），但 v0.3
    `_dict_to_task` 的兼容逻辑保证万一手动构造缺字段 dict 也能解析。"""
    from scheduler.store import _dict_to_task

    legacy_payload = {
        "task_id": "leg",
        "name": "legacy",
        "enabled": True,
        "state": "scheduled",
        "origin": "tool",
        "trigger": {"trigger_type": "interval", "expr": "30", "timezone": "UTC"},
        "policy": {
            "session_mode": "fresh_session",
            "concurrency_policy": "forbid",
            "misfire_policy": "skip",
        },
        "target": {"agent_name": "default", "input_text": "ping", "metadata": {}},
        "next_run_at": None,
        "last_run_at": None,
        "created_by": "tester",
        "created_at": "2026-05-04T00:00:00+00:00",
        "updated_at": "2026-05-04T00:00:00+00:00",
        # 故意缺 delivery 字段
    }
    task = _dict_to_task(legacy_payload)
    assert task.delivery is None


def test_legacy_run_dict_without_delivery_fields_falls_back_to_defaults(
    tmp_path: Path,
) -> None:
    """legacy run dict（缺 v0.3 三个字段）→ _dict_to_run 应填默认值。"""
    from scheduler.store import _dict_to_run

    legacy_payload = {
        "run_id": "leg-r",
        "task_id": "leg",
        "status": "completed",
        "scheduled_for": "2026-05-04T00:00:00+00:00",
        "started_at": "2026-05-04T00:00:01+00:00",
        "finished_at": "2026-05-04T00:00:02+00:00",
        "session_id": "sess-leg",
        "result_status": "completed",
        "final_message_excerpt": "ok",
        "error_message": None,
        "failure_reason": None,
        "delivery_error": None,
        "silent_suppressed": False,
        # 故意缺 delivered_at / delivery_status / seen_at
    }
    run = _dict_to_run(legacy_payload)
    assert run.delivered_at is None
    assert run.delivery_status is DeliveryStatus.PENDING
    assert run.seen_at is None
