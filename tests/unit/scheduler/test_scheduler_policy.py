"""unit：scheduler.policy concurrency / misfire 应用器（Wave D1，#18/#19）。

覆盖矩阵（参见 docs/agent-cron-module-v0.1/04-data-and-state.md §3.2）：

§1 apply_concurrency_policy
  - ALLOW + 无 RUNNING → proceed
  - ALLOW + 有 RUNNING → proceed（不查 Store）
  - FORBID + 无 RUNNING → proceed
  - FORBID + 有 RUNNING → skip + reason 含 run_id
  - REPLACE + 无 RUNNING → proceed
  - REPLACE + 有 RUNNING → replace；旧 run 在 Store 已变 CANCELLED；audit 写 run_cancelled_by_replace
  - 非法策略 → ValueError（防御）

§2 misfire 支持查询（v0.1 三策略全部落地）
  - get_misfire_support(SKIP / CATCH_UP_ONCE / FIRE_NOW) → True，note 非空
  - assert_misfire_policy_supported(SKIP / CATCH_UP_ONCE / FIRE_NOW) → 不抛

§3 Store.cancel_run（顺带覆盖）
  - run_id 不存在 → TaskNotFoundError
  - run 已 completed → ValueError
  - happy path → 旧行 superseded，新行 CANCELLED + finished_at 非空
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scheduler.domain import (
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
from scheduler.policy import (
    ConcurrencyDecision,
    apply_concurrency_policy,
    assert_misfire_policy_supported,
    get_misfire_support,
)
from scheduler.store import Store, TaskNotFoundError
from scheduler.timing import to_iso

# ---------------------------------------------------------------------------
# fixtures（与 test_scheduler_store.py 风格一致）
# ---------------------------------------------------------------------------


def _t(seconds_offset: int = 0) -> str:
    base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    return to_iso(base + timedelta(seconds=seconds_offset))


def _policy(
    *,
    concurrency: ConcurrencyPolicy = ConcurrencyPolicy.FORBID,
    misfire: MisfirePolicy = MisfirePolicy.SKIP,
) -> TaskExecutionPolicy:
    return TaskExecutionPolicy(
        session_mode=SessionMode.FRESH_SESSION,
        concurrency_policy=concurrency,
        misfire_policy=misfire,
    )


def _make_task(
    *,
    task_id: str = "t-policy",
    concurrency: ConcurrencyPolicy = ConcurrencyPolicy.FORBID,
) -> ScheduledTask:
    return ScheduledTask(
        task_id=task_id,
        name=f"name-{task_id}",
        lifecycle=TaskLifecycleState.SCHEDULED,
        origin=TaskOrigin.CLI,
        trigger=ScheduleTrigger(
            trigger_type=TriggerType.INTERVAL,
            expr="10",
            timezone="UTC",
        ),
        policy=_policy(concurrency=concurrency),
        target=TaskTarget(agent_name="default", input_text="ping", metadata={}),
        next_run_at=_t(0),
        last_run_at=None,
        created_by="tester",
        created_at=_t(0),
        updated_at=_t(0),
    )


def _make_running_run(
    *,
    run_id: str = "r-running",
    task_id: str = "t-policy",
) -> ScheduledRun:
    return ScheduledRun(
        run_id=run_id,
        task_id=task_id,
        status=RunStatus.RUNNING,
        scheduled_for=_t(0),
        started_at=_t(0),
        finished_at=None,
        session_id=f"sess-{run_id}",
        result_status=None,
        final_message_excerpt=None,
        error_message=None,
        failure_reason=None,
        delivery_error=None,
        silent_suppressed=False,
    )


def _make_completed_run(
    *,
    run_id: str = "r-done",
    task_id: str = "t-policy",
) -> ScheduledRun:
    return ScheduledRun(
        run_id=run_id,
        task_id=task_id,
        status=RunStatus.COMPLETED,
        scheduled_for=_t(0),
        started_at=_t(0),
        finished_at=_t(60),
        session_id=f"sess-{run_id}",
        result_status="completed",
        final_message_excerpt="ok",
        error_message=None,
        failure_reason=None,
        delivery_error=None,
        silent_suppressed=False,
    )


# ---------------------------------------------------------------------------
# §1 apply_concurrency_policy
# ---------------------------------------------------------------------------


def test_allow_without_running_proceeds(tmp_path: Path) -> None:
    store = Store(tmp_path)
    task = _make_task(task_id="t-allow-empty", concurrency=ConcurrencyPolicy.ALLOW)
    store.create_task(task)

    decision = apply_concurrency_policy(task=task, store=store)
    assert decision.action == "proceed"
    assert decision.cancelled_run_id is None
    assert decision.reason is None


def test_allow_with_running_still_proceeds(tmp_path: Path) -> None:
    """ALLOW 即使有 RUNNING 也直接 proceed，不查 Store 状态。"""
    store = Store(tmp_path)
    task = _make_task(task_id="t-allow-running", concurrency=ConcurrencyPolicy.ALLOW)
    store.create_task(task)
    store.append_run(_make_running_run(run_id="r-existing", task_id=task.task_id))

    decision = apply_concurrency_policy(task=task, store=store)
    assert decision.action == "proceed"
    # 旧 RUNNING 行未被改动
    latest = store.get_latest_run(task.task_id)
    assert latest is not None
    assert latest.status is RunStatus.RUNNING


def test_forbid_without_running_proceeds(tmp_path: Path) -> None:
    store = Store(tmp_path)
    task = _make_task(task_id="t-forbid-empty", concurrency=ConcurrencyPolicy.FORBID)
    store.create_task(task)

    decision = apply_concurrency_policy(task=task, store=store)
    assert decision.action == "proceed"


def test_forbid_with_running_skips(tmp_path: Path) -> None:
    store = Store(tmp_path)
    task = _make_task(task_id="t-forbid-running", concurrency=ConcurrencyPolicy.FORBID)
    store.create_task(task)
    store.append_run(_make_running_run(run_id="r-existing", task_id=task.task_id))

    decision = apply_concurrency_policy(task=task, store=store)
    assert decision.action == "skip"
    assert decision.cancelled_run_id is None
    assert decision.reason is not None
    assert "r-existing" in decision.reason
    # 旧 RUNNING 行依然 RUNNING（forbid 不动 Store）
    latest = store.get_latest_run(task.task_id)
    assert latest is not None
    assert latest.status is RunStatus.RUNNING


def test_forbid_with_completed_run_proceeds(tmp_path: Path) -> None:
    """FORBID 但 latest 已是 COMPLETED → proceed。"""
    store = Store(tmp_path)
    task = _make_task(task_id="t-forbid-done", concurrency=ConcurrencyPolicy.FORBID)
    store.create_task(task)
    store.append_run(_make_completed_run(run_id="r-done", task_id=task.task_id))

    decision = apply_concurrency_policy(task=task, store=store)
    assert decision.action == "proceed"


def test_replace_without_running_proceeds(tmp_path: Path) -> None:
    store = Store(tmp_path)
    task = _make_task(task_id="t-replace-empty", concurrency=ConcurrencyPolicy.REPLACE)
    store.create_task(task)

    decision = apply_concurrency_policy(task=task, store=store)
    assert decision.action == "proceed"
    assert decision.cancelled_run_id is None


def test_replace_with_running_cancels_and_audits(tmp_path: Path) -> None:
    store = Store(tmp_path)
    task = _make_task(task_id="t-replace-running", concurrency=ConcurrencyPolicy.REPLACE)
    store.create_task(task)
    store.append_run(_make_running_run(run_id="r-old", task_id=task.task_id))

    decision = apply_concurrency_policy(task=task, store=store)

    assert isinstance(decision, ConcurrencyDecision)
    assert decision.action == "replace"
    assert decision.cancelled_run_id == "r-old"
    assert decision.reason is not None
    assert "r-old" in decision.reason

    # 旧 run 已被改为 CANCELLED
    latest = store.get_latest_run(task.task_id)
    assert latest is not None
    assert latest.run_id == "r-old"
    assert latest.status is RunStatus.CANCELLED
    assert latest.finished_at is not None

    # audit 写了 run_cancelled_by_replace
    audits = store.list_audits(task_id=task.task_id)
    actions = [a["action"] for a in audits]
    assert "run_cancelled_by_replace" in actions
    payload = next(a for a in audits if a["action"] == "run_cancelled_by_replace")["payload"]
    assert payload["cancelled_run_id"] == "r-old"


def test_unknown_policy_raises_value_error(tmp_path: Path) -> None:
    """非法 concurrency_policy 值（绕过 frozen dataclass 注入）→ ValueError。"""
    store = Store(tmp_path)
    task = _make_task(task_id="t-unknown")
    store.create_task(task)
    store.append_run(_make_running_run(run_id="r-x", task_id=task.task_id))

    # ConcurrencyPolicy 是 StrEnum；object.__setattr__ 绕过 frozen 注入非法对象
    bogus = "not-a-policy"
    object.__setattr__(task.policy, "concurrency_policy", bogus)
    try:
        with pytest.raises(ValueError):
            apply_concurrency_policy(task=task, store=store)
    finally:
        object.__setattr__(task.policy, "concurrency_policy", ConcurrencyPolicy.FORBID)


# ---------------------------------------------------------------------------
# §2 misfire 支持查询
# ---------------------------------------------------------------------------


def test_get_misfire_support_skip_supported() -> None:
    info = get_misfire_support(MisfirePolicy.SKIP)
    assert info.policy is MisfirePolicy.SKIP
    assert info.supported_in_v01 is True
    assert info.note  # 非空


def test_get_misfire_support_catch_up_once_supported() -> None:
    info = get_misfire_support(MisfirePolicy.CATCH_UP_ONCE)
    assert info.policy is MisfirePolicy.CATCH_UP_ONCE
    assert info.supported_in_v01 is True
    # note 描述补跑语义
    assert "补跑" in info.note or "scheduled_for" in info.note


def test_get_misfire_support_fire_now_supported() -> None:
    info = get_misfire_support(MisfirePolicy.FIRE_NOW)
    assert info.policy is MisfirePolicy.FIRE_NOW
    assert info.supported_in_v01 is True
    assert info.note  # 非空


def test_assert_misfire_policy_supported_skip_does_not_raise() -> None:
    assert_misfire_policy_supported(MisfirePolicy.SKIP)


def test_assert_misfire_policy_supported_catch_up_does_not_raise() -> None:
    """v0.1 已落地 CATCH_UP_ONCE，装配期不再抛 NotImplementedError。"""
    assert_misfire_policy_supported(MisfirePolicy.CATCH_UP_ONCE)


def test_assert_misfire_policy_supported_fire_now_does_not_raise() -> None:
    """v0.1 已落地 FIRE_NOW，装配期不再抛 NotImplementedError。"""
    assert_misfire_policy_supported(MisfirePolicy.FIRE_NOW)


# ---------------------------------------------------------------------------
# §3 Store.cancel_run
# ---------------------------------------------------------------------------


def test_cancel_run_unknown_run_id_raises_task_not_found(tmp_path: Path) -> None:
    store = Store(tmp_path)
    task = _make_task(task_id="t-cancel-missing")
    store.create_task(task)
    with pytest.raises(TaskNotFoundError):
        store.cancel_run(
            run_id="r-ghost",
            task_id=task.task_id,
            failure_reason=None,
            error_message=None,
        )


def test_cancel_run_already_completed_raises_value_error(tmp_path: Path) -> None:
    store = Store(tmp_path)
    task = _make_task(task_id="t-cancel-done")
    store.create_task(task)
    store.append_run(_make_completed_run(run_id="r-done", task_id=task.task_id))

    with pytest.raises(ValueError):
        store.cancel_run(
            run_id="r-done",
            task_id=task.task_id,
            failure_reason=None,
            error_message=None,
        )


def test_cancel_run_happy_path_supersedes_running(tmp_path: Path) -> None:
    store = Store(tmp_path)
    task = _make_task(task_id="t-cancel-ok")
    store.create_task(task)
    store.append_run(_make_running_run(run_id="r-live", task_id=task.task_id))

    store.cancel_run(
        run_id="r-live",
        task_id=task.task_id,
        failure_reason=RunFailureReason.RUNNER_EXCEPTION,
        error_message="forced cancel",
    )

    latest = store.get_latest_run(task.task_id)
    assert latest is not None
    assert latest.run_id == "r-live"
    assert latest.status is RunStatus.CANCELLED
    assert latest.finished_at is not None
    assert latest.error_message == "forced cancel"
    assert latest.failure_reason is RunFailureReason.RUNNER_EXCEPTION
    # list_runs 折叠后只剩一条逻辑 run（旧 RUNNING 行已 supersede）
    runs = store.list_runs(task.task_id)
    assert len(runs) == 1
    assert runs[0].status is RunStatus.CANCELLED
