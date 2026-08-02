"""unit：scheduler.store 文件 store 覆盖（Wave B1，dev-checklist #3-#7）。

覆盖矩阵（参见 docs/agent-cron-module-v0.1/04-data-and-state.md §4）：

1. task CRUD
2. 原子写 + JSON 容错
3. run CRUD（含 supersede / list / latest）
4. due / advance（one-shot grace、recurring stale 快进、enabled 过滤）
5. file lock（同实例串行 + 跨进程互斥）
6. recovery（``running`` → ``abandoned``）
7. audit append / list（task_id / limit 过滤）

测试规范：
- 用 ``tmp_path`` 隔离每条用例的 ``home_dir``
- 每个测试函数独立 ``Store``，不复用
- 时间字符串通过 ``scheduler.timing.to_iso`` 构造，不硬编码 ISO 字串
- mock "现在" 用 ``monkeypatch`` 替换 ``scheduler.store._now_iso`` 与
  ``scheduler.timing.utc_now``
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from scheduler.domain import (
    GRACE_MIN_SECONDS,
    SCHEMA_VERSION,
    ConcurrencyPolicy,
    MisfirePolicy,
    RunFailureReason,
    RunStatus,
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
from scheduler.store import (
    SchedulerBusyError,
    Store,
    TaskNotFoundError,
    _dict_to_task,
    _period_seconds,
    _task_to_dict,
)
from scheduler.timing import to_iso

# ---------------------------------------------------------------------------
# 测试辅助：fixture 构造（不放 conftest，与 test_scheduler_domain 风格一致）
# ---------------------------------------------------------------------------


def _t(seconds_offset: int = 0, *, base: datetime | None = None) -> str:
    """构造 UTC ISO8601 时间字符串。``seconds_offset`` 相对 ``base`` 偏移。"""
    base = base or datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    return to_iso(base + timedelta(seconds=seconds_offset))


def _trigger_once(expr: str | None = None) -> ScheduleTrigger:
    return ScheduleTrigger(
        trigger_type=TriggerType.ONCE,
        expr=expr or _t(0),
        timezone="UTC",
    )


def _trigger_interval(minutes: int = 10) -> ScheduleTrigger:
    return ScheduleTrigger(
        trigger_type=TriggerType.INTERVAL,
        expr=str(minutes),
        timezone="UTC",
    )


def _policy(misfire: MisfirePolicy = MisfirePolicy.SKIP) -> TaskExecutionPolicy:
    return TaskExecutionPolicy(
        session_mode=SessionMode.FRESH_SESSION,
        concurrency_policy=ConcurrencyPolicy.FORBID,
        misfire_policy=misfire,
    )


def _target() -> TaskTarget:
    return TaskTarget(agent_name="default", input_text="ping", metadata={})


def _make_task(
    *,
    task_id: str = "t-1",
    lifecycle: TaskLifecycleState = TaskLifecycleState.SCHEDULED,
    trigger: ScheduleTrigger | None = None,
    next_run_at: str | None = None,
    last_run_at: str | None = None,
    misfire: MisfirePolicy = MisfirePolicy.SKIP,
    thread_id: str = "",
) -> ScheduledTask:
    return ScheduledTask(
        task_id=task_id,
        name=f"task-{task_id}",
        lifecycle=lifecycle,
        origin=TaskOrigin.CLI,
        trigger=trigger or _trigger_interval(10),
        policy=_policy(misfire=misfire),
        target=_target(),
        next_run_at=next_run_at,
        last_run_at=last_run_at,
        created_by="tester",
        created_at=_t(0),
        updated_at=_t(0),
        thread_id=thread_id,
    )


def _make_run(
    *,
    run_id: str = "r-1",
    task_id: str = "t-1",
    status: RunStatus = RunStatus.RUNNING,
    scheduled_for: str | None = None,
    finished_at: str | None = None,
    failure_reason: RunFailureReason | None = None,
    thread_id: str = "",
) -> ScheduledRun:
    return ScheduledRun(
        run_id=run_id,
        task_id=task_id,
        status=status,
        scheduled_for=scheduled_for or _t(0),
        started_at=_t(0),
        finished_at=finished_at,
        session_id=f"sess-{run_id}",
        result_status=None,
        final_message_excerpt=None,
        error_message=None,
        failure_reason=failure_reason,
        delivery_error=None,
        silent_suppressed=False,
        thread_id=thread_id,
    )


# ---------------------------------------------------------------------------
# 1. task CRUD
# ---------------------------------------------------------------------------


def test_create_and_get_task_round_trip(tmp_path: Path) -> None:
    store = Store(tmp_path)
    task = _make_task(task_id="t-create")
    store.create_task(task)
    fetched = store.get_task("t-create")
    assert fetched is not None
    assert fetched.task_id == "t-create"
    assert fetched.trigger.trigger_type is TriggerType.INTERVAL
    assert fetched.trigger.expr == "10"
    assert fetched.policy.silent_marker_enabled is True


def test_task_thread_id_round_trip(tmp_path: Path) -> None:
    store = Store(tmp_path)
    task = _make_task(task_id="t-thread", thread_id="thread-aaaaaaaaaaaa")
    store.create_task(task)

    fetched = store.get_task("t-thread")

    assert fetched is not None
    assert fetched.thread_id == "thread-aaaaaaaaaaaa"
    payload = json.loads((tmp_path / "scheduled_tasks.json").read_text(encoding="utf-8"))
    assert payload["tasks"][0]["thread_id"] == "thread-aaaaaaaaaaaa"


def test_run_thread_id_round_trip(tmp_path: Path) -> None:
    store = Store(tmp_path)
    run = _make_run(run_id="r-thread", thread_id="thread-dddddddddddd")
    store.append_run(run)

    fetched = store.list_runs("t-1")

    assert fetched[0].thread_id == "thread-dddddddddddd"
    payload = json.loads((tmp_path / "runs" / "t-1.jsonl").read_text(encoding="utf-8"))
    assert payload["thread_id"] == "thread-dddddddddddd"


def test_dict_to_task_recovers_thread_id_from_metadata() -> None:
    task = _make_task()
    payload = _task_to_dict(task)
    payload.pop("thread_id")
    payload["target"]["metadata"]["thread_id"] = "thread-bbbbbbbbbbbb"

    restored = _dict_to_task(payload)

    assert restored.thread_id == "thread-bbbbbbbbbbbb"


def test_dict_to_task_recovers_thread_id_from_delivery_target() -> None:
    task = _make_task()
    payload = _task_to_dict(task)
    payload.pop("thread_id")
    payload["delivery"] = {"channel": "web", "target": "thread:thread-cccccccccccc"}

    restored = _dict_to_task(payload)

    assert restored.thread_id == "thread-cccccccccccc"


def test_create_task_duplicate_raises_value_error(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-dup"))
    with pytest.raises(ValueError):
        store.create_task(_make_task(task_id="t-dup"))


def test_update_task_unknown_raises_task_not_found(tmp_path: Path) -> None:
    store = Store(tmp_path)
    with pytest.raises(TaskNotFoundError):
        store.update_task("missing", lifecycle=TaskLifecycleState.PAUSED)


def test_update_task_partial_fields(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-upd"))
    new_next = _t(600)
    updated = store.update_task(
        "t-upd",
        lifecycle=TaskLifecycleState.PAUSED,
        next_run_at=new_next,
    )
    assert updated.lifecycle is TaskLifecycleState.PAUSED
    assert updated.next_run_at == new_next
    # updated_at 自动刷新（与 created_at 不同）
    assert updated.updated_at != updated.created_at


def test_list_tasks_filters_disabled_by_default(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="alive"))
    store.create_task(
        _make_task(task_id="dead", lifecycle=TaskLifecycleState.DISABLED),
    )
    visible = store.list_tasks()
    assert [t.task_id for t in visible] == ["alive"]
    everything = store.list_tasks(include_disabled=True)
    assert {t.task_id for t in everything} == {"alive", "dead"}


def test_delete_task_returns_true_when_existed(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-del"))
    assert store.delete_task("t-del") is True
    assert store.get_task("t-del") is None


def test_delete_task_returns_false_when_missing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    assert store.delete_task("ghost") is False


# ---------------------------------------------------------------------------
# 2. 原子写 + JSON 容错
# ---------------------------------------------------------------------------


def test_create_task_writes_tasks_json_file(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-fs"))
    tasks_json = tmp_path / "scheduled_tasks.json"
    assert tasks_json.exists()
    payload = json.loads(tasks_json.read_text(encoding="utf-8"))
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["tasks"][0]["task_id"] == "t-fs"


def test_load_tasks_recovers_from_strict_false_parseable(tmp_path: Path) -> None:
    """文档 §4.1：JSONDecodeError 时尝试 ``strict=False`` 自动修复。

    构造一段含未转义控制字符的 metadata.note，正常 ``json.load`` 会失败，
    ``strict=False`` 仍能解析。
    """
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-strict"))

    raw = (tmp_path / "scheduled_tasks.json").read_text(encoding="utf-8")
    # 把一处合法字符替换为裸控制字符（U+0008 backspace）
    broken = raw.replace('"task_id": "t-strict"', '"task_id": "t-strict\b"')
    (tmp_path / "scheduled_tasks.json").write_text(broken, encoding="utf-8")

    # strict=False 容忍裸控制字符
    fetched = store.get_task("t-strict\b")
    assert fetched is not None


# ---------------------------------------------------------------------------
# 3. run CRUD
# ---------------------------------------------------------------------------


def test_append_run_then_get_latest(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-run"))
    run = _make_run(run_id="r1", task_id="t-run", status=RunStatus.RUNNING)
    store.append_run(run)
    latest = store.get_latest_run("t-run")
    assert latest is not None
    assert latest.run_id == "r1"
    assert latest.status is RunStatus.RUNNING


def test_supersede_appends_one_new_snapshot(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-sup"))
    first = _make_run(run_id="r1", task_id="t-sup", status=RunStatus.RUNNING)
    store.append_run(first)
    second = _make_run(
        run_id="r1",
        task_id="t-sup",
        status=RunStatus.COMPLETED,
        finished_at=_t(60),
    )
    store.supersede_and_append_run(second)

    # list_runs 按 run_id 折叠到最新有效快照。
    runs = store.list_runs("t-sup")
    assert len(runs) == 1
    assert runs[0].status is RunStatus.COMPLETED

    # 物理 jsonl append-only：原始 running + completed 完整快照，共两次 fsync。
    raw_lines = (tmp_path / "runs" / "t-sup.jsonl").read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(line) for line in raw_lines if line.strip()]
    assert len(parsed) == 2
    statuses = [(p["status"], bool(p.get("_superseded"))) for p in parsed]
    assert statuses == [("running", False), ("completed", False)]


def test_list_runs_recovers_previous_snapshot_from_dangling_legacy_tombstone(
    tmp_path: Path,
) -> None:
    """旧进程若在 tombstone 后崩溃，逻辑读取仍保留上一条 RUNNING 快照。"""
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-legacy-tombstone"))
    running = _make_run(
        run_id="r-legacy",
        task_id="t-legacy-tombstone",
        status=RunStatus.RUNNING,
    )
    store.append_run(running)
    path = tmp_path / "runs" / "t-legacy-tombstone.jsonl"
    payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    payload["_superseded"] = True
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")

    assert store.get_run("t-legacy-tombstone", "r-legacy") == running


def test_list_runs_limit_truncates_to_last_n(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-lim"))
    for i in range(5):
        store.append_run(
            _make_run(run_id=f"r{i}", task_id="t-lim", status=RunStatus.COMPLETED),
        )
    truncated = store.list_runs("t-lim", limit=2)
    assert [r.run_id for r in truncated] == ["r3", "r4"]


def test_get_latest_run_returns_none_when_empty(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-empty"))
    assert store.get_latest_run("t-empty") is None


# ---------------------------------------------------------------------------
# 4. due / advance
# ---------------------------------------------------------------------------


def test_oneshot_due_at_now(tmp_path: Path) -> None:
    store = Store(tmp_path)
    now = _t(0)
    task = _make_task(
        task_id="t-once-now",
        trigger=_trigger_once(expr=now),
        next_run_at=now,
    )
    store.create_task(task)
    reservations = store.reserve_due_tasks(now=now)
    assert len(reservations) == 1
    assert reservations[0].task.task_id == "t-once-now"
    assert reservations[0].scheduled_for == now


def test_oneshot_due_within_grace(tmp_path: Path) -> None:
    """next_run_at = now - 60s（grace 120s 内）→ due。"""
    store = Store(tmp_path)
    next_run = _t(-60)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-once-late",
            trigger=_trigger_once(expr=next_run),
            next_run_at=next_run,
        ),
    )
    reservations = store.reserve_due_tasks(now=now)
    assert len(reservations) == 1


def test_oneshot_outside_grace_not_due(tmp_path: Path) -> None:
    """next_run_at = now - 200s（>120s grace）→ 不 due。"""
    store = Store(tmp_path)
    next_run = _t(-200)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-once-stale",
            trigger=_trigger_once(expr=next_run),
            next_run_at=next_run,
        ),
    )
    reservations = store.reserve_due_tasks(now=now)
    assert reservations == []


def test_oneshot_already_ran_not_due(tmp_path: Path) -> None:
    """``EXHAUSTED`` one-shot 即使残留 next_run_at 也不再 due。"""
    store = Store(tmp_path)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-once-ran",
            lifecycle=TaskLifecycleState.EXHAUSTED,
            trigger=_trigger_once(expr=now),
            next_run_at=now,
            last_run_at=_t(-30),
        ),
    )
    reservations = store.reserve_due_tasks(now=now)
    assert reservations == []


# ---------------------------------------------------------------------------
# v0.2 P0 修复回归：ONCE reserve 时立即归档（避免死循环 fire 100+ 次）
# ---------------------------------------------------------------------------


def test_reserve_oneshot_archives_immediately(tmp_path: Path) -> None:
    """ONCE 任务 reserve 一次后立即归档：再调 reserve_due_tasks 不返回。

    回归 v0.2 P0 死循环 bug：grace 窗口内 ONCE 持续被 reserve / fire 100+ 次。
    """
    store = Store(tmp_path)
    next_run = _t(-5)  # 5s 前过期，仍在 120s grace 内
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-once-archive",
            trigger=_trigger_once(expr=next_run),
            next_run_at=next_run,
        ),
    )

    # 第一次 reserve：拿到 1 个 reservation
    res1 = store.reserve_due_tasks(now=now)
    assert len(res1) == 1
    assert res1[0].task.task_id == "t-once-archive"

    # 第二次 reserve（同一 now）：必须返回空（已归档）
    res2 = store.reserve_due_tasks(now=now)
    assert res2 == []

    # task 已被原子领取：lifecycle=EXHAUSTED，终态完成前不写 last_run_at
    archived = store.get_task("t-once-archive")
    assert archived is not None
    assert archived.lifecycle is TaskLifecycleState.EXHAUSTED
    assert archived.last_run_at is None
    assert archived.next_run_at is None


def test_reserve_oneshot_archived_audit_written(tmp_path: Path) -> None:
    """reserve 归档 ONCE 同时写 ``run_oneshot_archived`` audit。"""
    store = Store(tmp_path)
    next_run = _t(-10)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-once-audit",
            trigger=_trigger_once(expr=next_run),
            next_run_at=next_run,
        ),
    )

    store.reserve_due_tasks(now=now)

    audits = store.list_audits(task_id="t-once-audit")
    actions = [a["action"] for a in audits]
    assert "run_oneshot_archived" in actions, actions
    archived_audit = next(a for a in audits if a["action"] == "run_oneshot_archived")
    assert archived_audit["payload"]["original_next_run_at"] == next_run
    assert archived_audit["payload"]["reserved_at"] == now
    assert archived_audit["actor"] == "scheduler"


def test_reserve_oneshot_archive_persists_across_reload(tmp_path: Path) -> None:
    """归档后重新构造 Store（模拟进程重启）→ list 看到归档状态，
    再调 reserve 不返回。
    """
    next_run = _t(-30)
    now = _t(0)
    store = Store(tmp_path)
    store.create_task(
        _make_task(
            task_id="t-once-reload",
            trigger=_trigger_once(expr=next_run),
            next_run_at=next_run,
        ),
    )
    res1 = store.reserve_due_tasks(now=now)
    assert len(res1) == 1

    # 重启：新 Store 实例从同一目录加载（绝不读文件就能保证幂等）
    fresh_store = Store(tmp_path)
    persisted = fresh_store.get_task("t-once-reload")
    assert persisted is not None
    assert persisted.lifecycle is TaskLifecycleState.EXHAUSTED
    assert persisted.last_run_at is None
    assert persisted.next_run_at is None

    # 重启后再 reserve（哪怕用同一个 now）也不会返回
    res2 = fresh_store.reserve_due_tasks(now=now)
    assert res2 == []


def test_recurring_due_advances_next_run_at(tmp_path: Path) -> None:
    """interval=10min, next_run_at=now → due，next_run_at 推进 10 分钟。"""
    store = Store(tmp_path)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-recur",
            trigger=_trigger_interval(10),
            next_run_at=now,
        ),
    )
    reservations = store.reserve_due_tasks(now=now)
    assert len(reservations) == 1
    assert reservations[0].scheduled_for == now

    after = store.get_task("t-recur")
    assert after is not None
    # 推进了 10 分钟（600 秒）
    advanced = datetime.fromisoformat(after.next_run_at or "")
    base = datetime.fromisoformat(now)
    assert (advanced - base) == timedelta(minutes=10)


def test_recurring_stale_fast_forwards_without_due(tmp_path: Path) -> None:
    """interval=10min, next_run_at=now-30min（grace=5min）→ stale 快进，不返回 due。"""
    store = Store(tmp_path)
    now = _t(0)
    next_run = _t(-1800)  # 30 min ago
    store.create_task(
        _make_task(
            task_id="t-stale",
            trigger=_trigger_interval(10),
            next_run_at=next_run,
        ),
    )
    reservations = store.reserve_due_tasks(now=now)
    assert reservations == []
    after = store.get_task("t-stale")
    assert after is not None
    # 快进到 now + 10min
    advanced = datetime.fromisoformat(after.next_run_at or "")
    base = datetime.fromisoformat(now)
    assert (advanced - base) == timedelta(minutes=10)
    # 写了 audit，且 payload 含 misfire_policy=skip
    audits = store.list_audits(task_id="t-stale")
    skipped = [a for a in audits if a["action"] == "run_skipped_stale"]
    assert len(skipped) == 1
    assert skipped[0]["payload"]["misfire_policy"] == MisfirePolicy.SKIP.value


# --- misfire_policy=CATCH_UP_ONCE ---------------------------------------------


def test_recurring_stale_catch_up_once_returns_due_with_original_scheduled_for(
    tmp_path: Path,
) -> None:
    """interval=10min, stale + CATCH_UP_ONCE → 返回 reservation；scheduled_for 是原 next_run_at。"""
    store = Store(tmp_path)
    now = _t(0)
    next_run = _t(-1800)
    store.create_task(
        _make_task(
            task_id="t-catchup",
            trigger=_trigger_interval(10),
            next_run_at=next_run,
            misfire=MisfirePolicy.CATCH_UP_ONCE,
        ),
    )
    reservations = store.reserve_due_tasks(now=now)
    assert len(reservations) == 1
    res = reservations[0]
    assert res.task.task_id == "t-catchup"
    # CATCH_UP_ONCE 的关键：保留原 next_run_at
    assert res.scheduled_for == next_run
    # 但 store 中 next_run_at 已推进到 now + period
    after = store.get_task("t-catchup")
    assert after is not None
    advanced = datetime.fromisoformat(after.next_run_at or "")
    base = datetime.fromisoformat(now)
    assert (advanced - base) == timedelta(minutes=10)


def test_recurring_stale_catch_up_once_only_fires_once(tmp_path: Path) -> None:
    """连续两次 reserve_due_tasks：第一次补跑 1 次，第二次因 next_run_at 已推进而无 due。"""
    store = Store(tmp_path)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-catchup-once",
            trigger=_trigger_interval(10),
            next_run_at=_t(-1800),
            misfire=MisfirePolicy.CATCH_UP_ONCE,
        ),
    )
    first = store.reserve_due_tasks(now=now)
    assert [r.task.task_id for r in first] == ["t-catchup-once"]
    # 第二次：next_run_at 已被推进到 now + 10min，未来时间 → 无 due
    second = store.reserve_due_tasks(now=now)
    assert second == []


def test_recurring_stale_catch_up_once_audits_run_catch_up_once(tmp_path: Path) -> None:
    """CATCH_UP_ONCE 写 audit ``run_catch_up_once``，payload 含
    ``original_scheduled_for`` / ``advanced_to`` / ``misfire_policy``。"""
    store = Store(tmp_path)
    now = _t(0)
    next_run = _t(-1800)
    store.create_task(
        _make_task(
            task_id="t-catchup-aud",
            trigger=_trigger_interval(10),
            next_run_at=next_run,
            misfire=MisfirePolicy.CATCH_UP_ONCE,
        ),
    )
    store.reserve_due_tasks(now=now)
    audits = store.list_audits(task_id="t-catchup-aud")
    matched = [a for a in audits if a["action"] == "run_catch_up_once"]
    assert len(matched) == 1
    payload = matched[0]["payload"]
    assert payload["original_scheduled_for"] == next_run
    assert payload["misfire_policy"] == MisfirePolicy.CATCH_UP_ONCE.value
    # advanced_to 应等于 store 中的 next_run_at
    after = store.get_task("t-catchup-aud")
    assert after is not None
    assert payload["advanced_to"] == after.next_run_at
    # 不会同时写 run_skipped_stale / run_fire_now
    assert all(a["action"] != "run_skipped_stale" for a in audits)
    assert all(a["action"] != "run_fire_now" for a in audits)


def test_recurring_stale_catch_up_once_does_not_write_skip_audit(tmp_path: Path) -> None:
    """CATCH_UP_ONCE 不应写 SKIP 路径的 ``run_skipped_stale`` 审计。"""
    store = Store(tmp_path)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-catchup-noskip",
            trigger=_trigger_interval(10),
            next_run_at=_t(-1800),
            misfire=MisfirePolicy.CATCH_UP_ONCE,
        ),
    )
    store.reserve_due_tasks(now=now)
    audits = store.list_audits(task_id="t-catchup-noskip")
    actions = {a["action"] for a in audits}
    assert "run_skipped_stale" not in actions
    assert "run_catch_up_once" in actions


# --- misfire_policy=FIRE_NOW --------------------------------------------------


def test_recurring_stale_fire_now_returns_due_with_now_as_scheduled_for(
    tmp_path: Path,
) -> None:
    """FIRE_NOW：scheduled_for == now（不是原 next_run_at）。"""
    store = Store(tmp_path)
    now = _t(0)
    next_run = _t(-1800)
    store.create_task(
        _make_task(
            task_id="t-fire",
            trigger=_trigger_interval(10),
            next_run_at=next_run,
            misfire=MisfirePolicy.FIRE_NOW,
        ),
    )
    reservations = store.reserve_due_tasks(now=now)
    assert len(reservations) == 1
    res = reservations[0]
    assert res.task.task_id == "t-fire"
    # FIRE_NOW 的关键：scheduled_for 用 now，不是原 next_run_at
    assert res.scheduled_for == now
    assert res.scheduled_for != next_run


def test_recurring_stale_fire_now_advances_next_run_at(tmp_path: Path) -> None:
    """FIRE_NOW 的 next_run_at 推进到 now + period，与 SKIP / CATCH_UP_ONCE 一致。"""
    store = Store(tmp_path)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-fire-adv",
            trigger=_trigger_interval(10),
            next_run_at=_t(-1800),
            misfire=MisfirePolicy.FIRE_NOW,
        ),
    )
    store.reserve_due_tasks(now=now)
    after = store.get_task("t-fire-adv")
    assert after is not None
    advanced = datetime.fromisoformat(after.next_run_at or "")
    base = datetime.fromisoformat(now)
    assert (advanced - base) == timedelta(minutes=10)


def test_recurring_stale_fire_now_audits_run_fire_now(tmp_path: Path) -> None:
    """FIRE_NOW 写 audit ``run_fire_now``，payload 含 ``original_scheduled_for``
    / ``fired_at`` / ``advanced_to`` / ``misfire_policy``。"""
    store = Store(tmp_path)
    now = _t(0)
    next_run = _t(-1800)
    store.create_task(
        _make_task(
            task_id="t-fire-aud",
            trigger=_trigger_interval(10),
            next_run_at=next_run,
            misfire=MisfirePolicy.FIRE_NOW,
        ),
    )
    store.reserve_due_tasks(now=now)
    audits = store.list_audits(task_id="t-fire-aud")
    matched = [a for a in audits if a["action"] == "run_fire_now"]
    assert len(matched) == 1
    payload = matched[0]["payload"]
    assert payload["original_scheduled_for"] == next_run
    assert payload["fired_at"] == now
    assert payload["misfire_policy"] == MisfirePolicy.FIRE_NOW.value
    after = store.get_task("t-fire-aud")
    assert after is not None
    assert payload["advanced_to"] == after.next_run_at


def test_recurring_stale_fire_now_differs_from_catch_up_once(tmp_path: Path) -> None:
    """对比断言：同样的 stale 输入下，FIRE_NOW 与 CATCH_UP_ONCE 仅 scheduled_for 不同
    （前者用 now，后者用原 next_run_at），next_run_at 推进口径一致。"""
    next_run = _t(-1800)
    now = _t(0)

    store_a = Store(tmp_path / "a")
    store_a.create_task(
        _make_task(
            task_id="t-cmp-fire",
            trigger=_trigger_interval(10),
            next_run_at=next_run,
            misfire=MisfirePolicy.FIRE_NOW,
        ),
    )
    fire_res = store_a.reserve_due_tasks(now=now)

    store_b = Store(tmp_path / "b")
    store_b.create_task(
        _make_task(
            task_id="t-cmp-catchup",
            trigger=_trigger_interval(10),
            next_run_at=next_run,
            misfire=MisfirePolicy.CATCH_UP_ONCE,
        ),
    )
    catchup_res = store_b.reserve_due_tasks(now=now)

    assert len(fire_res) == 1
    assert len(catchup_res) == 1
    assert fire_res[0].scheduled_for == now
    assert catchup_res[0].scheduled_for == next_run
    # next_run_at 推进一致
    after_fire = store_a.get_task("t-cmp-fire")
    after_catchup = store_b.get_task("t-cmp-catchup")
    assert after_fire is not None and after_catchup is not None
    assert after_fire.next_run_at == after_catchup.next_run_at


def test_reserve_mixed_tasks(tmp_path: Path) -> None:
    store = Store(tmp_path)
    now = _t(0)
    # due
    store.create_task(
        _make_task(task_id="due", trigger=_trigger_interval(10), next_run_at=_t(-60)),
    )
    # stale
    store.create_task(
        _make_task(task_id="stale", trigger=_trigger_interval(10), next_run_at=_t(-1800)),
    )
    # future
    store.create_task(
        _make_task(task_id="future", trigger=_trigger_interval(10), next_run_at=_t(600)),
    )
    reservations = store.reserve_due_tasks(now=now)
    assert [r.task.task_id for r in reservations] == ["due"]


def test_reserve_skips_disabled_tasks(tmp_path: Path) -> None:
    store = Store(tmp_path)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="off",
            trigger=_trigger_interval(10),
            next_run_at=now,
            lifecycle=TaskLifecycleState.PAUSED,
        ),
    )
    reservations = store.reserve_due_tasks(now=now)
    assert reservations == []


def test_advance_next_run_updates_field(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-adv"))
    target = _t(3600)
    store.advance_next_run("t-adv", next_run_at=target)
    fetched = store.get_task("t-adv")
    assert fetched is not None
    assert fetched.next_run_at == target


def test_advance_next_run_missing_raises(tmp_path: Path) -> None:
    store = Store(tmp_path)
    with pytest.raises(TaskNotFoundError):
        store.advance_next_run("ghost", next_run_at=_t(0))


# ---------------------------------------------------------------------------
# 5. file lock
# ---------------------------------------------------------------------------


def test_same_store_reserve_serialized(tmp_path: Path) -> None:
    """同 Store 实例两次 reserve 串行 OK；锁是非阻塞但每次 with 内自然释放。"""
    store = Store(tmp_path)
    now = _t(0)
    store.create_task(
        _make_task(task_id="serial", trigger=_trigger_interval(10), next_run_at=now),
    )
    first = store.reserve_due_tasks(now=now)
    second = store.reserve_due_tasks(now=now)
    # 第一次拿到 due，第二次因 next_run_at 已被推进而无 due
    assert [r.task.task_id for r in first] == ["serial"]
    assert second == []


@pytest.mark.skipif(sys.platform.startswith("win"), reason="fcntl-only test")
def test_reserve_due_tasks_raises_busy_when_lock_held(tmp_path: Path) -> None:
    """另一进程持锁 → reserve_due_tasks 抛 :class:`SchedulerBusyError`。"""
    import fcntl

    store = Store(tmp_path)
    lock_path = tmp_path / ".scheduler.lock"
    lock_path.touch()
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SchedulerBusyError):
            store.reserve_due_tasks(now=_t(0))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---------------------------------------------------------------------------
# 6. recovery
# ---------------------------------------------------------------------------


def test_recover_stale_runs_marks_running_as_abandoned(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-rec"))
    store.append_run(
        _make_run(run_id="r1", task_id="t-rec", status=RunStatus.RUNNING),
    )
    recovered = store.recover_stale_runs()
    assert recovered == 1
    latest = store.get_latest_run("t-rec")
    assert latest is not None
    assert latest.status is RunStatus.ABANDONED
    assert latest.failure_reason is RunFailureReason.ABANDONED_ON_RESTART
    assert latest.finished_at is not None


def test_recover_stale_runs_returns_zero_when_no_running(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-clean"))
    store.append_run(
        _make_run(run_id="r1", task_id="t-clean", status=RunStatus.COMPLETED),
    )
    assert store.recover_stale_runs() == 0


# ---------------------------------------------------------------------------
# 7. audit
# ---------------------------------------------------------------------------


def test_append_audit_then_list(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.append_audit(
        action="create",
        task_id="t-aud",
        actor="tester",
        payload={"k": "v"},
    )
    audits = store.list_audits()
    assert len(audits) == 1
    assert audits[0]["action"] == "create"
    assert audits[0]["task_id"] == "t-aud"
    assert audits[0]["payload"] == {"k": "v"}


def test_list_audits_filter_by_task_id(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.append_audit(action="create", task_id="A", actor="x", payload={})
    store.append_audit(action="update", task_id="B", actor="x", payload={})
    store.append_audit(action="remove", task_id="A", actor="x", payload={})
    only_a = store.list_audits(task_id="A")
    assert [r["action"] for r in only_a] == ["create", "remove"]


def test_list_audits_limit(tmp_path: Path) -> None:
    store = Store(tmp_path)
    for i in range(5):
        store.append_audit(action="ping", task_id=None, actor="x", payload={"i": i})
    last_two = store.list_audits(limit=2)
    payloads: list[dict[str, Any]] = [r["payload"] for r in last_two]
    assert [p["i"] for p in payloads] == [3, 4]


# ---------------------------------------------------------------------------
# _period_seconds：v0.2 SECONDS 分支
# ---------------------------------------------------------------------------


def test_ticker_status_overwrites_latest_snapshot(tmp_path: Path) -> None:
    store = Store(tmp_path)

    store.write_ticker_status(status="ok", payload={"tick_at": "t1", "due_count": 0})
    store.write_ticker_status(status="ok", payload={"tick_at": "t2", "due_count": 3})

    status = store.read_ticker_status()
    assert status is not None
    assert status["status"] == "ok"
    assert status["tick_at"] == "t2"
    assert status["due_count"] == 3


def test_append_incident_then_list_and_filter(tmp_path: Path) -> None:
    store = Store(tmp_path)

    store.append_incident(
        action="tick_failed",
        task_id=None,
        actor="ticker",
        payload={"error": "boom"},
    )
    store.append_incident(
        action="tick_task_error",
        task_id="A",
        actor="ticker",
        payload={"error": "task boom"},
        severity="warning",
    )

    incidents = store.list_incidents()
    assert [r["action"] for r in incidents] == ["tick_failed", "tick_task_error"]
    assert incidents[0]["severity"] == "error"
    assert incidents[1]["severity"] == "warning"
    assert [r["task_id"] for r in store.list_incidents(task_id="A")] == ["A"]


def test_record_timestamps_use_configured_display_timezone(tmp_path: Path) -> None:
    store = Store(tmp_path, display_timezone="Asia/Shanghai")
    tick_at = "2026-06-15T08:00:00+00:00"

    store.append_audit(action="create", task_id="A", actor="x", payload={"tick_at": tick_at})
    store.append_incident(
        action="tick_failed",
        task_id=None,
        actor="ticker",
        payload={"tick_at": tick_at},
    )
    store.write_ticker_status(status="ok", payload={"tick_at": tick_at})

    audit = store.list_audits()[0]
    incident = store.list_incidents()[0]
    assert audit["created_at"].endswith("+08:00")
    assert incident["created_at"].endswith("+08:00")
    assert audit["payload"]["tick_at_display"] == "2026-06-15T16:00:00+08:00"
    assert incident["payload"]["tick_at_display"] == "2026-06-15T16:00:00+08:00"
    status = store.read_ticker_status()
    assert status is not None
    assert status["updated_at"].endswith("+08:00")
    assert status["tick_at"] == tick_at
    assert status["tick_at_display"] == "2026-06-15T16:00:00+08:00"


def test_period_seconds_seconds_returns_int_seconds() -> None:
    """SECONDS 分支：expr="10" → 10 秒（不再 *60）。"""
    trigger = ScheduleTrigger(trigger_type=TriggerType.SECONDS, expr="10", timezone="UTC")
    assert _period_seconds(trigger) == 10


def test_period_seconds_seconds_strips_whitespace() -> None:
    """SECONDS 分支：与 INTERVAL 一样支持去前后空白。"""
    trigger = ScheduleTrigger(trigger_type=TriggerType.SECONDS, expr=" 30 ", timezone="UTC")
    assert _period_seconds(trigger) == 30


def test_period_seconds_seconds_unparseable_falls_back_to_grace_min() -> None:
    """SECONDS expr 不可解析时退化到 GRACE_MIN_SECONDS（保持其他分支兜底语义）。

    注意：domain 层 ScheduleTrigger 构造时会拒掉非数字 SECONDS expr；这里直接调用
    ``_period_seconds`` 以校验函数本身的兜底分支，绕过 dataclass 校验只测纯函数。
    """
    trigger = ScheduleTrigger.__new__(ScheduleTrigger)
    object.__setattr__(trigger, "trigger_type", TriggerType.SECONDS)
    object.__setattr__(trigger, "expr", "abc")
    object.__setattr__(trigger, "timezone", "UTC")
    assert _period_seconds(trigger) == GRACE_MIN_SECONDS


# ---------------------------------------------------------------------------
# 8. schema 迁移（v0.2 启动检测）
# ---------------------------------------------------------------------------


def _read_tasks_json(home: Path) -> dict[str, Any]:
    return json.loads((home / "scheduled_tasks.json").read_text(encoding="utf-8"))


def _backup_files(home: Path, label: str = "*") -> list[Path]:
    """v0.3：备份后缀按 detected_version 命名（``v1`` / ``v2`` / ``vmissing``
    / ``vunparseable``）。``label="*"`` 匹配所有备份。"""
    return sorted(home.glob(f"scheduled_tasks.json.{label}.bak.*"))


def _migrated_audit_action_to_current() -> str:
    """构造迁移 audit action 关键字：``schema_migrated_*_to_v<SCHEMA_VERSION>``"""
    return f"_to_v{SCHEMA_VERSION}"


def test_init_no_tasks_json_skips_migration(tmp_path: Path) -> None:
    """tasks.json 不存在时不触发迁移：不写空文件、不写 audit、不报错。"""
    store = Store(tmp_path)
    assert not (tmp_path / "scheduled_tasks.json").exists()
    audits = store.list_audits()
    suffix = _migrated_audit_action_to_current()
    assert all(suffix not in a["action"] for a in audits)


def test_init_v2_tasks_json_no_migration(tmp_path: Path) -> None:
    """schema_version == SCHEMA_VERSION 的 tasks.json 不被迁移；现有任务可读。"""
    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-keep"))
    # 重建 store；不应触发迁移
    store2 = Store(tmp_path)
    fetched = store2.get_task("t-keep")
    assert fetched is not None
    audits = store2.list_audits()
    suffix = _migrated_audit_action_to_current()
    assert all(suffix not in a["action"] for a in audits)
    # 没有任何 .bak 备份
    assert _backup_files(tmp_path) == []


def test_init_v1_tasks_json_no_schema_version_field_migrates(tmp_path: Path) -> None:
    """缺少 schema_version 字段（旧 v0.1 ``version=1`` 落盘格式）→ 迁移：
    备份（``.vmissing.bak``）+ 写空 v3 + audit + stderr warning。"""
    home = tmp_path
    home.mkdir(parents=True, exist_ok=True)
    (home / "scheduled_tasks.json").write_text(
        json.dumps({"version": 1, "updated_at": _t(0), "tasks": [{"task_id": "old"}]}),
        encoding="utf-8",
    )

    store = Store(home)

    # 备份生成（exactly 1 个，label=vmissing 因为缺 schema_version 字段）
    backups = _backup_files(home, label="vmissing")
    assert len(backups) == 1
    # 备份内容是原始 v1 数据
    backup_payload = json.loads(backups[0].read_text(encoding="utf-8"))
    assert backup_payload["version"] == 1
    assert backup_payload["tasks"][0]["task_id"] == "old"

    # 当前 tasks.json 是空当前 schema
    current = _read_tasks_json(home)
    assert current["schema_version"] == SCHEMA_VERSION
    assert current["tasks"] == []

    # audit 有 schema_migrated_vmissing_to_v<SCHEMA_VERSION>
    audits = store.list_audits()
    expected_action = f"schema_migrated_vmissing_to_v{SCHEMA_VERSION}"
    migrated = [a for a in audits if a["action"] == expected_action]
    assert len(migrated) == 1
    payload = migrated[0]["payload"]
    assert payload["backup_path"] == str(backups[0])
    assert payload["detected_version"] == "missing"
    assert payload["current_schema_version"] == SCHEMA_VERSION


def test_init_v1_schema_version_one_migrates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """显式 schema_version=1 → 同样迁移；stderr warning 含 ``backed up to``。"""
    (tmp_path / "scheduled_tasks.json").write_text(
        json.dumps({"schema_version": 1, "tasks": []}), encoding="utf-8"
    )

    store = Store(tmp_path)
    captured = capsys.readouterr()
    assert "backed up to" in captured.err
    assert "scheduled_tasks.json.v1.bak." in captured.err

    # detected_version == 1 落到 audit；action 含来源 / 当前版本
    audits = store.list_audits()
    expected_action = f"schema_migrated_v1_to_v{SCHEMA_VERSION}"
    migrated = [a for a in audits if a["action"] == expected_action]
    assert len(migrated) == 1
    assert migrated[0]["payload"]["detected_version"] == 1


def test_init_v2_data_migrates_to_current(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """v0.3：显式 schema_version=2（v0.2 数据）→ 迁移到 v3。
    备份 ``.v2.bak.<ts>`` + audit ``schema_migrated_v2_to_v3``。"""
    (tmp_path / "scheduled_tasks.json").write_text(
        json.dumps({"schema_version": 2, "updated_at": _t(0), "tasks": [{"task_id": "old-v2"}]}),
        encoding="utf-8",
    )

    store = Store(tmp_path)
    captured = capsys.readouterr()
    assert "backed up to" in captured.err
    assert "scheduled_tasks.json.v2.bak." in captured.err

    backups = _backup_files(tmp_path, label="v2")
    assert len(backups) == 1
    backup_payload = json.loads(backups[0].read_text(encoding="utf-8"))
    assert backup_payload["schema_version"] == 2
    assert backup_payload["tasks"][0]["task_id"] == "old-v2"

    # 当前 tasks.json 是空 v3
    current = _read_tasks_json(tmp_path)
    assert current["schema_version"] == SCHEMA_VERSION
    assert current["tasks"] == []

    audits = store.list_audits()
    expected_action = f"schema_migrated_v2_to_v{SCHEMA_VERSION}"
    migrated = [a for a in audits if a["action"] == expected_action]
    assert len(migrated) == 1
    assert migrated[0]["payload"]["detected_version"] == 2


def test_init_corrupt_tasks_json_fails_without_overwrite(tmp_path: Path) -> None:
    """无法解析的 tasks.json 明确失败，并保留原文件供恢复。"""
    tasks_path = tmp_path / "scheduled_tasks.json"
    original = "{not json at all"
    tasks_path.write_text(original, encoding="utf-8")

    with pytest.raises(RuntimeError, match="无法解析迁移"):
        Store(tmp_path)

    assert tasks_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("scheduled_tasks.json.*.bak.*")) == []


def test_init_after_migration_can_create_task(tmp_path: Path) -> None:
    """迁移后能正常 create_task，新落盘文件 schema_version=SCHEMA_VERSION。"""
    (tmp_path / "scheduled_tasks.json").write_text(
        json.dumps({"version": 1, "tasks": []}), encoding="utf-8"
    )

    store = Store(tmp_path)
    store.create_task(_make_task(task_id="t-after"))

    fetched = store.get_task("t-after")
    assert fetched is not None
    payload = _read_tasks_json(tmp_path)
    assert payload["schema_version"] == SCHEMA_VERSION
    assert [t["task_id"] for t in payload["tasks"]] == ["t-after"]


def test_init_unknown_schema_version_migrates(tmp_path: Path) -> None:
    """schema_version > SCHEMA_VERSION（如未来版本）→ 同样按 != 触发迁移。

    保护用户不会用旧版本误读未来格式数据。
    """
    future_version = SCHEMA_VERSION + 99
    (tmp_path / "scheduled_tasks.json").write_text(
        json.dumps({"schema_version": future_version, "tasks": []}), encoding="utf-8"
    )

    store = Store(tmp_path)
    audits = store.list_audits()
    # v0.3：detected=future_version → label v<future_version>，audit action 含完整路径
    expected_action = f"schema_migrated_v{future_version}_to_v{SCHEMA_VERSION}"
    migrated = [a for a in audits if a["action"] == expected_action]
    assert len(migrated) == 1
    assert migrated[0]["payload"]["detected_version"] == future_version


# ---------------------------------------------------------------------------
# 9. reserve_due_tasks 幂等矩阵（v0.2 P0 排查回归）
# ---------------------------------------------------------------------------
#
# 矩阵覆盖：trigger ∈ {ONCE, INTERVAL, CRON, SECONDS}
#         × misfire ∈ {normal_due, stale-skip, stale-catch_up_once, stale-fire_now}
#
# ONCE 没有 stale 概念（要么 grace 内要么不 due），所以 ONCE 只测 1 case。
# 其它每个 trigger 测 4 case，共 1 + 4*3 = 13 case；
# 任务清单要求"覆盖 12 条路径"——这里把 normal_due 与 ONCE-archive 共用，
# 实际写出 13 个，多覆盖一条不亏。
#
# 每个 case 的不变量：reserve 后**再调一次同 now reserve_due_tasks**，必须返回空——
# 防止 grace 窗口内被多次 tick reserve 导致死循环 fire（参考 v0.2 ONCE P0 bug）。
# ---------------------------------------------------------------------------


def _trigger_cron(expr: str = "*/1 * * * *", *, timezone: str = "UTC") -> ScheduleTrigger:
    return ScheduleTrigger(trigger_type=TriggerType.CRON, expr=expr, timezone=timezone)


def _trigger_seconds(seconds: int = 1) -> ScheduleTrigger:
    return ScheduleTrigger(trigger_type=TriggerType.SECONDS, expr=str(seconds), timezone="UTC")


# --- ONCE (1 case) ----------------------------------------------------------


def test_reserve_once_normal_due_idempotent(tmp_path: Path) -> None:
    """ONCE in-grace：第一次 reserve 拿 1 个；第二次同 now 必须返回空。

    防御 v0.2 P0 死循环 bug——grace 窗口（120s）内 ONCE 不能被多次 reserve。
    """
    store = Store(tmp_path)
    next_run = _t(-30)  # 30s 前到期，仍在 120s grace 内
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-once-idem",
            trigger=_trigger_once(expr=next_run),
            next_run_at=next_run,
        ),
    )
    res1 = store.reserve_due_tasks(now=now)
    assert len(res1) == 1, f"ONCE in-grace: expected 1 reservation, got {len(res1)}"

    res2 = store.reserve_due_tasks(now=now)
    assert res2 == [], f"ONCE 二次 reserve 不应再返回（否则死循环 fire），got {len(res2)}"


def test_reserve_once_future_in_grace_not_reserved(tmp_path: Path) -> None:
    """ONCE 任务 next_run_at 在未来（30 秒后） → reserve 必须返回空。

    回归 v0.2 P0 bug：``is_within_oneshot_grace(future_run_at, now)`` 永远 True
    （因为 future >= now - 120s），但任务**还没到点**，不应被 reserve。
    用户实测场景：创建"5 分钟后提醒" → 任务立即被 reserve + fire。
    """
    store = Store(tmp_path)
    next_run = _t(30)  # 30 秒后才到点
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-once-future",
            trigger=_trigger_once(expr=next_run),
            next_run_at=next_run,
        ),
    )
    res = store.reserve_due_tasks(now=now)
    assert res == [], (
        f"ONCE 未到点不应被 reserve，但拿到 {len(res)} 个 reservation（"
        f"next_run_at={next_run}，now={now}，差 30s 还没到点）"
    )

    # 任务必须保持原状态（未被归档）
    task = store.get_task("t-once-future")
    assert task is not None
    assert task.lifecycle is TaskLifecycleState.SCHEDULED
    assert task.last_run_at is None


def test_reserve_once_far_future_not_reserved(tmp_path: Path) -> None:
    """ONCE 任务 next_run_at 在远未来（10 分钟后） → reserve 必须返回空。

    确保 grace 算法不会把"远在 grace 之外的未来时刻"也 reserve。
    """
    store = Store(tmp_path)
    next_run = _t(600)  # 10 分钟后
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-once-far-future",
            trigger=_trigger_once(expr=next_run),
            next_run_at=next_run,
        ),
    )
    res = store.reserve_due_tasks(now=now)
    assert res == [], f"远未来 ONCE 不应被 reserve，但拿到 {len(res)} 个"


# --- INTERVAL (4 cases) -----------------------------------------------------


def test_reserve_interval_normal_due_idempotent(tmp_path: Path) -> None:
    """INTERVAL=10min, next_run_at=now-60s（grace=300s 内）→ normal due。

    第一次拿 1 个；第二次因 next_run_at 已被推进到 now+10min（未来）→ 空。
    """
    store = Store(tmp_path)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-interval-normal",
            trigger=_trigger_interval(10),
            next_run_at=_t(-60),
            misfire=MisfirePolicy.SKIP,
        ),
    )
    res1 = store.reserve_due_tasks(now=now)
    assert len(res1) == 1

    res2 = store.reserve_due_tasks(now=now)
    assert res2 == [], "INTERVAL normal_due 二次 reserve 应为空"


def test_reserve_interval_stale_skip_idempotent(tmp_path: Path) -> None:
    """INTERVAL=10min, next_run_at=now-30min（>grace=300s）+ SKIP →
    第一次 stale 快进，**不**返回 reservation；第二次同样为空。
    """
    store = Store(tmp_path)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-interval-skip",
            trigger=_trigger_interval(10),
            next_run_at=_t(-1800),
            misfire=MisfirePolicy.SKIP,
        ),
    )
    res1 = store.reserve_due_tasks(now=now)
    assert res1 == [], "INTERVAL stale-skip 第一次 reserve 应为空（快进而不补跑）"

    res2 = store.reserve_due_tasks(now=now)
    assert res2 == [], "INTERVAL stale-skip 二次 reserve 应为空"


def test_reserve_interval_stale_catch_up_once_idempotent(tmp_path: Path) -> None:
    """INTERVAL=10min, stale + CATCH_UP_ONCE → 第一次补跑 1；第二次空。"""
    store = Store(tmp_path)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-interval-catchup",
            trigger=_trigger_interval(10),
            next_run_at=_t(-1800),
            misfire=MisfirePolicy.CATCH_UP_ONCE,
        ),
    )
    res1 = store.reserve_due_tasks(now=now)
    assert len(res1) == 1

    res2 = store.reserve_due_tasks(now=now)
    assert res2 == [], "INTERVAL stale-catch_up_once 二次 reserve 应为空"


def test_reserve_interval_stale_fire_now_idempotent(tmp_path: Path) -> None:
    """INTERVAL=10min, stale + FIRE_NOW → 第一次 fire 1；第二次空。"""
    store = Store(tmp_path)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-interval-fire",
            trigger=_trigger_interval(10),
            next_run_at=_t(-1800),
            misfire=MisfirePolicy.FIRE_NOW,
        ),
    )
    res1 = store.reserve_due_tasks(now=now)
    assert len(res1) == 1

    res2 = store.reserve_due_tasks(now=now)
    assert res2 == [], "INTERVAL stale-fire_now 二次 reserve 应为空"


# --- CRON (4 cases) ---------------------------------------------------------


def test_reserve_cron_normal_due_idempotent(tmp_path: Path) -> None:
    """CRON `*/1 * * * *`（period=60s, grace=120s）, next_run_at=now-30s →
    normal due（offset 30 ≤ grace）。

    第一次拿 1 个；第二次因 next_run_at 推进到 now+60s（未来）→ 空。
    """
    store = Store(tmp_path)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-cron-normal",
            trigger=_trigger_cron("*/1 * * * *"),
            next_run_at=_t(-30),
            misfire=MisfirePolicy.SKIP,
        ),
    )
    res1 = store.reserve_due_tasks(now=now)
    assert len(res1) == 1

    res2 = store.reserve_due_tasks(now=now)
    assert res2 == [], "CRON normal_due 二次 reserve 应为空"


def test_manual_run_preserves_cron_cursor(tmp_path: Path) -> None:
    """16:59 手动执行每日 22:00 任务后，当日 22:00 仍保留为正式触发点。"""
    store = Store(tmp_path)
    manual_at = to_iso(datetime(2026, 7, 12, 8, 59, 46, tzinfo=UTC))
    scheduled_at = to_iso(datetime(2026, 7, 12, 14, 0, 0, tzinfo=UTC))
    store.create_task(
        _make_task(
            task_id="t-cron-manual",
            trigger=_trigger_cron("0 22 * * *", timezone="Asia/Shanghai"),
            next_run_at=scheduled_at,
        ),
    )

    store.request_manual_run("t-cron-manual", requested_at=manual_at)
    reservations = store.reserve_due_tasks(now=manual_at)

    assert len(reservations) == 1
    assert reservations[0].scheduled_for == manual_at
    after = store.get_task("t-cron-manual")
    assert after is not None
    assert after.next_run_at == scheduled_at
    assert after.manual_run_requested_at is None


def test_cron_normal_due_advances_to_next_expression_match(tmp_path: Path) -> None:
    """每日 cron 在 22:00 触发后，下一次保持次日 22:00，不跟随 ticker 秒级漂移。"""
    store = Store(tmp_path)
    scheduled_at = to_iso(datetime(2026, 7, 12, 14, 0, 0, tzinfo=UTC))
    tick_at = to_iso(datetime(2026, 7, 12, 14, 0, 5, tzinfo=UTC))
    expected_next = to_iso(datetime(2026, 7, 13, 22, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    store.create_task(
        _make_task(
            task_id="t-cron-fixed-clock",
            trigger=_trigger_cron("0 22 * * *", timezone="Asia/Shanghai"),
            next_run_at=scheduled_at,
        ),
    )

    reservations = store.reserve_due_tasks(now=tick_at)

    assert len(reservations) == 1
    after = store.get_task("t-cron-fixed-clock")
    assert after is not None
    assert after.next_run_at == expected_next


def test_reserve_cron_stale_skip_idempotent(tmp_path: Path) -> None:
    """CRON, next_run_at=now-300s（>120s grace）+ SKIP → 快进，不返回。"""
    store = Store(tmp_path)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-cron-skip",
            trigger=_trigger_cron("*/1 * * * *"),
            next_run_at=_t(-300),
            misfire=MisfirePolicy.SKIP,
        ),
    )
    res1 = store.reserve_due_tasks(now=now)
    assert res1 == []

    res2 = store.reserve_due_tasks(now=now)
    assert res2 == []


def test_reserve_cron_stale_catch_up_once_idempotent(tmp_path: Path) -> None:
    """CRON stale + CATCH_UP_ONCE → 第一次补跑 1；第二次空。"""
    store = Store(tmp_path)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-cron-catchup",
            trigger=_trigger_cron("*/1 * * * *"),
            next_run_at=_t(-300),
            misfire=MisfirePolicy.CATCH_UP_ONCE,
        ),
    )
    res1 = store.reserve_due_tasks(now=now)
    assert len(res1) == 1

    res2 = store.reserve_due_tasks(now=now)
    assert res2 == [], "CRON stale-catch_up_once 二次 reserve 应为空"


def test_reserve_cron_stale_fire_now_idempotent(tmp_path: Path) -> None:
    """CRON stale + FIRE_NOW → 第一次 fire 1；第二次空。"""
    store = Store(tmp_path)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-cron-fire",
            trigger=_trigger_cron("*/1 * * * *"),
            next_run_at=_t(-300),
            misfire=MisfirePolicy.FIRE_NOW,
        ),
    )
    res1 = store.reserve_due_tasks(now=now)
    assert len(res1) == 1

    res2 = store.reserve_due_tasks(now=now)
    assert res2 == [], "CRON stale-fire_now 二次 reserve 应为空"


# --- SECONDS (4 cases) ------------------------------------------------------


def test_reserve_seconds_normal_due_idempotent(tmp_path: Path) -> None:
    """SECONDS=1（period=1s, grace=120s）, next_run_at=now-30s → normal due。

    第一次拿 1 个；第二次因 next_run_at 推进到 now+1s（未来）→ 空。
    """
    store = Store(tmp_path)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-seconds-normal",
            trigger=_trigger_seconds(1),
            next_run_at=_t(-30),
            misfire=MisfirePolicy.SKIP,
        ),
    )
    res1 = store.reserve_due_tasks(now=now)
    assert len(res1) == 1

    res2 = store.reserve_due_tasks(now=now)
    assert res2 == [], "SECONDS normal_due 二次 reserve 应为空"


def test_reserve_seconds_stale_skip_idempotent(tmp_path: Path) -> None:
    """SECONDS=1, next_run_at=now-300s（>120s grace）+ SKIP → 快进。"""
    store = Store(tmp_path)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-seconds-skip",
            trigger=_trigger_seconds(1),
            next_run_at=_t(-300),
            misfire=MisfirePolicy.SKIP,
        ),
    )
    res1 = store.reserve_due_tasks(now=now)
    assert res1 == []

    res2 = store.reserve_due_tasks(now=now)
    assert res2 == []


def test_reserve_seconds_stale_catch_up_once_idempotent(tmp_path: Path) -> None:
    """SECONDS stale + CATCH_UP_ONCE → 第一次补跑 1；第二次空。"""
    store = Store(tmp_path)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-seconds-catchup",
            trigger=_trigger_seconds(1),
            next_run_at=_t(-300),
            misfire=MisfirePolicy.CATCH_UP_ONCE,
        ),
    )
    res1 = store.reserve_due_tasks(now=now)
    assert len(res1) == 1

    res2 = store.reserve_due_tasks(now=now)
    assert res2 == [], "SECONDS stale-catch_up_once 二次 reserve 应为空"


def test_reserve_seconds_stale_fire_now_idempotent(tmp_path: Path) -> None:
    """SECONDS stale + FIRE_NOW → 第一次 fire 1；第二次空。"""
    store = Store(tmp_path)
    now = _t(0)
    store.create_task(
        _make_task(
            task_id="t-seconds-fire",
            trigger=_trigger_seconds(1),
            next_run_at=_t(-300),
            misfire=MisfirePolicy.FIRE_NOW,
        ),
    )
    res1 = store.reserve_due_tasks(now=now)
    assert len(res1) == 1

    res2 = store.reserve_due_tasks(now=now)
    assert res2 == [], "SECONDS stale-fire_now 二次 reserve 应为空"


# ---------------------------------------------------------------------------
# _period_seconds: cron 分支按 trigger.timezone 解释（v0.2 P1 修复）
# ---------------------------------------------------------------------------


def test_period_seconds_cron_uses_timezone() -> None:
    """cron ``0 9 * * *`` 在 UTC / Asia/Shanghai / America/New_York 下 period 都是 86400s。

    period 在跨时区时不变（每天间隔都是 86400s），本测试验证：
    1. 函数能在指定时区下不抛异常；
    2. period 计算正确，不被 base = utc_now() 干扰。
    """
    for tz in ["UTC", "Asia/Shanghai", "America/New_York"]:
        trigger = ScheduleTrigger(
            trigger_type=TriggerType.CRON,
            expr="0 9 * * *",
            timezone=tz,
        )
        period = _period_seconds(trigger)
        assert period == 86400, f"cron daily 应该是 86400s 不论时区，{tz} 下得到 {period}"


def test_period_seconds_cron_invalid_timezone_falls_back() -> None:
    """非法 timezone → period 计算不抛异常，按 UTC fallback 算 period。"""
    trigger = ScheduleTrigger(
        trigger_type=TriggerType.CRON,
        expr="0 9 * * *",
        timezone="不存在的时区",
    )
    period = _period_seconds(trigger)
    assert period == 86400


def test_period_seconds_cron_6field_with_timezone() -> None:
    """6 字段 cron ``*/30 * * * * *`` + Asia/Shanghai → period=30s。"""
    trigger = ScheduleTrigger(
        trigger_type=TriggerType.CRON,
        expr="*/30 * * * * *",
        timezone="Asia/Shanghai",
    )
    period = _period_seconds(trigger)
    assert period == 30


# ---------------------------------------------------------------------------
# 10. reserve_due_tasks 防御性分支（cron-test-coverage-hermes-align #4-#7）
# ---------------------------------------------------------------------------


def test_reserve_disabled_task_not_returned(tmp_path: Path) -> None:
    """暂停任务永远不被 reserve。

    防御 store.reserve_due_tasks 的 lifecycle 门禁。
    """
    store = Store(tmp_path)
    next_run = _t(-30)  # 过期 30s
    store.create_task(
        _make_task(
            task_id="t-disabled",
            lifecycle=TaskLifecycleState.PAUSED,
            trigger=_trigger_once(expr=next_run),
            next_run_at=next_run,
        ),
    )
    res = store.reserve_due_tasks(now=_t(0))
    assert res == [], "暂停任务不应被 reserve"


def test_reserve_skips_task_without_next_run_at(tmp_path: Path) -> None:
    """v0.2 P0 测试覆盖：next_run_at=None 任务跳过。

    防御 store.reserve_due_tasks 中 ``if not task.next_run_at: continue`` 路径。
    防御 #1 修复后回归（B1 修复后 schedule_tool create 总会算出 next_run_at，
    但 store 仍要保留对 None 的防御）。
    """
    store = Store(tmp_path)
    store.create_task(
        _make_task(
            task_id="t-no-next",
            trigger=_trigger_interval(10),
            next_run_at=None,  # 没算出来（防御性）
        ),
    )
    res = store.reserve_due_tasks(now=_t(0))
    assert res == [], "next_run_at=None 不应被 reserve"


def test_reserve_oneshot_archived_not_returned(tmp_path: Path) -> None:
    """EXHAUSTED 的 one-shot 永远不被 reserve。

    防御 one-shot 原子领取后的重复执行。
    """
    store = Store(tmp_path)
    store.create_task(
        _make_task(
            task_id="t-archived",
            lifecycle=TaskLifecycleState.EXHAUSTED,
            trigger=_trigger_once(expr=_t(-30)),
            next_run_at=None,  # 归档时被设为 None
            last_run_at=_t(-25),  # 归档时被设
        ),
    )
    res = store.reserve_due_tasks(now=_t(0))
    assert res == [], "归档 ONCE 任务不应被 reserve"


def test_reserve_oneshot_outside_grace_not_returned(tmp_path: Path) -> None:
    """v0.2 P0 测试覆盖：ONCE 任务过 grace（>120s 之前）不被 reserve。

    Hermes 同款测试 test_once_past_returns_none。
    """
    store = Store(tmp_path)
    next_run = _t(-200)  # 200s 前过期，超 grace=120s
    store.create_task(
        _make_task(
            task_id="t-stale-oneshot",
            trigger=_trigger_once(expr=next_run),
            next_run_at=next_run,
        ),
    )
    res = store.reserve_due_tasks(now=_t(0))
    assert res == [], "过 grace 的 ONCE 任务不应被 reserve"

    # 任务保持原状（不归档）
    task = store.get_task("t-stale-oneshot")
    assert task is not None
    assert task.lifecycle is TaskLifecycleState.SCHEDULED
    assert task.last_run_at is None


# ---------------------------------------------------------------------------
# 11. recurring 边界（cron-test-coverage-hermes-align #8-#10）
# ---------------------------------------------------------------------------


def test_advance_next_run_when_already_future_no_op(tmp_path: Path) -> None:
    """v0.2 P1 测试覆盖：advance_next_run 在 next_run_at 已是未来时保留该值。

    Hermes 同款 test_already_future_stays_future。

    注意：当前 advance_next_run 实现是"直接覆盖 next_run_at"，不做新旧比较。
    所以 caller 传入"未来时刻"时，结果就是该未来时刻——本测试断言这一行为。
    """
    store = Store(tmp_path)
    future = _t(600)  # 10 分钟后
    store.create_task(
        _make_task(
            task_id="t-already-future",
            trigger=_trigger_interval(10),
            next_run_at=future,
        ),
    )
    # 调 advance_next_run 用同样未来时刻
    store.advance_next_run("t-already-future", next_run_at=future)

    task = store.get_task("t-already-future")
    assert task is not None
    assert task.next_run_at == future, "已是未来的 next_run_at 不应被改动"


def test_advance_next_run_oneshot_no_op(tmp_path: Path) -> None:
    """v0.2 P1 测试覆盖：advance_next_run 对 ONCE 任务不抛异常，任务仍存。

    ONCE 任务由 reserve_due_tasks 的归档机制管理；advance_next_run 当前实现
    不区分 trigger 类型，直接覆盖 next_run_at（产品代码现状如此）。

    本测试只断言"不抛 + 任务仍在 + next_run_at 被覆盖到传入值"，
    不为 ONCE 增加特殊处理（边界外）。

    Hermes 同款 test_skips_oneshot_job 在 hermes 实现里有"ONCE 跳过"；
    我们当前没有此特殊处理，故按实际行为写。
    """
    store = Store(tmp_path)
    one_shot_at = _t(-30)
    store.create_task(
        _make_task(
            task_id="t-once-no-advance",
            trigger=_trigger_once(expr=one_shot_at),
            next_run_at=one_shot_at,
        ),
    )
    # 尝试 advance ONCE 任务
    new_time = _t(60)
    store.advance_next_run("t-once-no-advance", next_run_at=new_time)

    # 当前实现：ONCE 不被特殊处理，next_run_at 被直接覆盖
    task = store.get_task("t-once-no-advance")
    assert task is not None, "advance ONCE 不应让任务消失"
    assert task.next_run_at == new_time, (
        "当前实现不区分 trigger 类型，next_run_at 被直接覆盖；"
        "如未来引入 ONCE 跳过特殊处理，本断言需改为保留 one_shot_at"
    )


def test_recover_stale_runs_multi_task_mixed_status(tmp_path: Path) -> None:
    """v0.2 P1 测试覆盖：多任务混合（RUNNING / COMPLETED / FAILED），重启后
    recover_stale_runs 把所有 RUNNING 标记为 ABANDONED，其它状态不动。

    Hermes 同款 test_crash_safety_scenario。
    """
    store = Store(tmp_path)

    # 创建 3 个任务 + 各自 1 条 run
    store.create_task(_make_task(task_id="t-running"))
    store.create_task(_make_task(task_id="t-completed"))
    store.create_task(_make_task(task_id="t-failed"))

    store.append_run(
        _make_run(run_id="r-running", task_id="t-running", status=RunStatus.RUNNING),
    )
    store.append_run(
        _make_run(
            run_id="r-completed",
            task_id="t-completed",
            status=RunStatus.COMPLETED,
            finished_at=_t(0),
        ),
    )
    store.append_run(
        _make_run(
            run_id="r-failed",
            task_id="t-failed",
            status=RunStatus.FAILED,
            finished_at=_t(0),
            failure_reason=RunFailureReason.RUNNER_EXCEPTION,
        ),
    )

    # 模拟重启：调 recover
    recovered = store.recover_stale_runs()
    assert recovered == 1, "只有 1 条 RUNNING 应被收尾"

    # RUNNING → ABANDONED
    running_latest = store.get_latest_run("t-running")
    assert running_latest is not None
    assert running_latest.status is RunStatus.ABANDONED, "RUNNING 应被收尾为 ABANDONED"
    assert running_latest.failure_reason is RunFailureReason.ABANDONED_ON_RESTART

    # COMPLETED 不动
    completed_latest = store.get_latest_run("t-completed")
    assert completed_latest is not None
    assert completed_latest.status is RunStatus.COMPLETED, "已完成不该被改"

    # FAILED 不动
    failed_latest = store.get_latest_run("t-failed")
    assert failed_latest is not None
    assert failed_latest.status is RunStatus.FAILED, "已失败不该被改"
    assert failed_latest.failure_reason is RunFailureReason.RUNNER_EXCEPTION
