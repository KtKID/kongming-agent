"""v0.5 scheduler-approval-task-level：schema v4 → v5 迁移测试。

覆盖：
- M1: v4 tasks.json 加载 → 自动备份（``.v4.bak.<ts>``）+ 写空 v5
- M2: v5 tasks.json 加载 → 不迁移、不备份
- M3: v0.5 新建 task 含 ``approval_mode`` 字段往返序列化（write → reload → read 不丢）

参考 ``test_schema_v3_migration.py`` 的 fixture 风格（tmp_path + Store 实例化）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scheduler.domain import (
    SCHEMA_VERSION,
    ApprovalMode,
    ConcurrencyPolicy,
    DeliveryChannel,
    MisfirePolicy,
    ScheduleDelivery,
    ScheduledTask,
    ScheduleTrigger,
    SessionMode,
    TaskExecutionPolicy,
    TaskLifecycleState,
    TaskOrigin,
    TaskTarget,
    TriggerType,
)
from scheduler.store import Store


def _make_task(
    *,
    task_id: str = "t-v5",
    approval_mode: ApprovalMode | None = None,
) -> ScheduledTask:
    """构造 v5 任务（可选 approval_mode）。"""
    return ScheduledTask(
        task_id=task_id,
        name=f"task-{task_id}",
        lifecycle=TaskLifecycleState.SCHEDULED,
        origin=TaskOrigin.TOOL,
        trigger=ScheduleTrigger(trigger_type=TriggerType.INTERVAL, expr="10", timezone="UTC"),
        policy=TaskExecutionPolicy(
            session_mode=SessionMode.FRESH_SESSION,
            concurrency_policy=ConcurrencyPolicy.FORBID,
            misfire_policy=MisfirePolicy.SKIP,
            approval_mode=approval_mode,
        ),
        target=TaskTarget(agent_name="default", input_text="ping", metadata={}),
        next_run_at=None,
        last_run_at=None,
        created_by="tester",
        created_at="2026-05-04T00:00:00+00:00",
        updated_at="2026-05-04T00:00:00+00:00",
        delivery=ScheduleDelivery(channel=DeliveryChannel.WEB),
    )


# ---------------------------------------------------------------------------
# M1: v4 → v5 重置式迁移
# ---------------------------------------------------------------------------


def test_v4_data_migrates_to_v5_with_backup(tmp_path: Path) -> None:
    """M1: 显式 schema_version=4 → 备份为 .v4.bak.<ts>，新文件写空 v5。

    断言要点：
    - 备份文件名包含 ``.v4.bak.`` 子串
    - 备份保留原始 v4 内容（tasks 数组完整保留）
    - 当前 ``scheduled_tasks.json`` 已升级到 SCHEMA_VERSION 且 tasks=[]
    - audit 记录中含 ``schema_migrated_v4_to_v<SCHEMA_VERSION>``
    """
    v4_data: dict[str, Any] = {
        "schema_version": 4,
        "updated_at": "2026-05-04T00:00:00+00:00",
        "tasks": [
            {
                "task_id": "old-v4",
                "name": "old-v4",
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
                "next_run_at": None,
                "last_run_at": None,
                "created_by": "tester",
                "created_at": "2026-05-04T00:00:00+00:00",
                "updated_at": "2026-05-04T00:00:00+00:00",
            }
        ],
    }
    tasks_path = tmp_path / "scheduled_tasks.json"
    tasks_path.write_text(json.dumps(v4_data), encoding="utf-8")

    Store(tmp_path)

    # 备份文件存在，命名带 v4 标签
    backups = sorted(tmp_path.glob("scheduled_tasks.json.v4.bak.*"))
    assert len(backups) == 1
    backup_payload = json.loads(backups[0].read_text(encoding="utf-8"))
    assert backup_payload["schema_version"] == 4
    assert backup_payload["tasks"][0]["task_id"] == "old-v4"

    # 当前文件升级到 v5（SCHEMA_VERSION）且 tasks 清空
    current = json.loads(tasks_path.read_text(encoding="utf-8"))
    assert current["schema_version"] == SCHEMA_VERSION
    assert current["tasks"] == []

    # audit 记录迁移事件
    audits = json.loads(
        "["
        + ",".join(
            line
            for line in (tmp_path / "audits.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        + "]"
    )
    expected_action = f"schema_migrated_v4_to_v{SCHEMA_VERSION}"
    migrated = [a for a in audits if a["action"] == expected_action]
    assert len(migrated) == 1
    assert migrated[0]["payload"]["detected_version"] == 4
    assert migrated[0]["payload"]["current_schema_version"] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# M2: v5 → v5 不再迁移
# ---------------------------------------------------------------------------


def test_v5_data_no_re_migration(tmp_path: Path) -> None:
    """M2: 已经是 v5 schema → 不触发迁移，不产生 .bak 文件。"""
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-keep"))

    # 重建 store
    Store(tmp_path)

    # 不应有任何备份文件
    backups = list(tmp_path.glob("scheduled_tasks.json.v*.bak.*"))
    assert backups == []

    # tasks.json schema_version 仍是 SCHEMA_VERSION
    current = json.loads((tmp_path / "scheduled_tasks.json").read_text(encoding="utf-8"))
    assert current["schema_version"] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# M3: v0.5 新字段 approval_mode 持久化往返
# ---------------------------------------------------------------------------


def test_v5_task_with_approval_mode_trust_round_trip(tmp_path: Path) -> None:
    """M3: 新建 task 带 ``approval_mode=TRUST`` → 写盘 → reload → 读回值一致。

    关键：v0.5 必须能持久化和反序列化 approval_mode。
    """
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-trust", approval_mode=ApprovalMode.TRUST))
    store.create_task(_make_task(task_id="t-fail-closed", approval_mode=ApprovalMode.FAIL_CLOSED))
    store.create_task(_make_task(task_id="t-none", approval_mode=None))

    # 重建 store 验证持久化
    store2 = Store(tmp_path)
    fetched_trust = store2.get_task("t-trust")
    fetched_fail_closed = store2.get_task("t-fail-closed")
    fetched_none = store2.get_task("t-none")

    assert fetched_trust is not None
    assert fetched_trust.policy.approval_mode is ApprovalMode.TRUST

    assert fetched_fail_closed is not None
    assert fetched_fail_closed.policy.approval_mode is ApprovalMode.FAIL_CLOSED

    assert fetched_none is not None
    assert fetched_none.policy.approval_mode is None
