"""unit：scheduler.domain 数据真源校验。

覆盖矩阵（参见 docs/agent-cron-module-v0.1/04-data-and-state.md §3 §6 §9）：
1. 枚举值正确性（含 v0.1 红线：不允许 ``waiting_approval``）
2. 常量值
3. ScheduleTrigger 构造与 frozen 不变性
4. TaskExecutionPolicy 默认值与边界
5. ScheduledTask 不变量（enabled / state 状态约束 + 必填字段）
6. ScheduledRun 字段
7. DueTaskReservation / TaskExecutionContext / ScheduledRunRequest
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from scheduler.domain import (
    DEFAULT_INACTIVITY_TIMEOUT,
    GRACE_MAX_SECONDS,
    GRACE_MIN_SECONDS,
    ONESHOT_GRACE_SECONDS,
    SCHEMA_VERSION,
    SILENT_MARKER,
    ConcurrencyPolicy,
    DueTaskReservation,
    MisfirePolicy,
    RunFailureReason,
    RunStatus,
    ScheduledRun,
    ScheduledRunRequest,
    ScheduledTask,
    ScheduleTrigger,
    SessionMode,
    TaskExecutionContext,
    TaskExecutionPolicy,
    TaskOrigin,
    TaskState,
    TaskTarget,
    TriggerType,
)

# ---------------------------------------------------------------------------
# 测试辅助：构造 fixture 用，不放 conftest（按 task 约束）
# ---------------------------------------------------------------------------


def _make_trigger(
    trigger_type: TriggerType = TriggerType.CRON,
    expr: str = "*/5 * * * *",
    timezone: str = "Asia/Shanghai",
) -> ScheduleTrigger:
    return ScheduleTrigger(trigger_type=trigger_type, expr=expr, timezone=timezone)


def _make_target(
    agent_name: str = "default",
    input_text: str = "ping",
) -> TaskTarget:
    return TaskTarget(agent_name=agent_name, input_text=input_text)


def _make_policy() -> TaskExecutionPolicy:
    return TaskExecutionPolicy()


def _make_task(
    *,
    enabled: bool = True,
    state: TaskState = TaskState.SCHEDULED,
    task_id: str = "t1",
    name: str = "task-1",
    created_by: str = "user-a",
    created_at: str = "2026-05-01T00:00:00Z",
    updated_at: str = "2026-05-01T00:00:00Z",
) -> ScheduledTask:
    return ScheduledTask(
        task_id=task_id,
        name=name,
        enabled=enabled,
        state=state,
        origin=TaskOrigin.CLI,
        trigger=_make_trigger(),
        policy=_make_policy(),
        target=_make_target(),
        next_run_at=None,
        last_run_at=None,
        created_by=created_by,
        created_at=created_at,
        updated_at=updated_at,
    )


def _make_exec_context(task_id: str = "t1") -> TaskExecutionContext:
    return TaskExecutionContext(
        task_id=task_id,
        scheduled_for="2026-05-01T00:00:00Z",
        trigger_type=TriggerType.CRON,
        origin=TaskOrigin.CLI,
        is_scheduled_run=True,
    )


# ---------------------------------------------------------------------------
# 1. 枚举值正确性
# ---------------------------------------------------------------------------


def test_run_status_silent_value():
    assert RunStatus.SILENT.value == "silent"


def test_run_status_inactivity_timeout_value():
    assert RunStatus.INACTIVITY_TIMEOUT.value == "inactivity_timeout"


def test_run_failure_reason_needs_approval_value():
    assert RunFailureReason.NEEDS_APPROVAL.value == "needs_approval"


def test_run_status_does_not_contain_waiting_approval():
    """v0.1 红线：cron run 不引入 waiting_approval 中间态。"""
    values = [s.value for s in RunStatus]
    assert "waiting_approval" not in values


def test_trigger_type_values():
    assert TriggerType.ONCE.value == "once"
    assert TriggerType.INTERVAL.value == "interval"
    assert TriggerType.CRON.value == "cron"
    assert TriggerType.SECONDS.value == "seconds"


def test_trigger_type_seconds_member_present():
    """v0.2 新增 SECONDS 枚举值，参与 1s ticker 秒级触发。"""
    assert "SECONDS" in TriggerType.__members__
    assert TriggerType("seconds") is TriggerType.SECONDS


def test_session_mode_v01_only_fresh_session():
    """v0.1 第一版固定 FRESH_SESSION。"""
    values = [m.value for m in SessionMode]
    assert values == ["fresh_session"]


# ---------------------------------------------------------------------------
# 2. 常量值
# ---------------------------------------------------------------------------


def test_oneshot_grace_seconds_constant():
    assert ONESHOT_GRACE_SECONDS == 120


def test_default_inactivity_timeout_constant():
    assert DEFAULT_INACTIVITY_TIMEOUT == 600


def test_grace_min_seconds_constant():
    assert GRACE_MIN_SECONDS == 120


def test_grace_max_seconds_constant():
    assert GRACE_MAX_SECONDS == 7200


def test_silent_marker_constant():
    assert SILENT_MARKER == "[SILENT]"


def test_schema_version_constant():
    # v0.5：scheduler-approval-task-level-v0.5 引入 TaskExecutionPolicy.approval_mode
    assert SCHEMA_VERSION == 5


# ---------------------------------------------------------------------------
# 3. ScheduleTrigger 构造与 frozen
# ---------------------------------------------------------------------------


def test_schedule_trigger_once_ok():
    trig = _make_trigger(TriggerType.ONCE, "2026-05-01T00:00:00Z", "UTC")
    assert trig.trigger_type is TriggerType.ONCE
    assert trig.expr == "2026-05-01T00:00:00Z"


def test_schedule_trigger_interval_ok():
    trig = _make_trigger(TriggerType.INTERVAL, "PT30M", "Asia/Shanghai")
    assert trig.trigger_type is TriggerType.INTERVAL
    assert trig.expr == "PT30M"


def test_schedule_trigger_cron_ok():
    trig = _make_trigger(TriggerType.CRON, "*/5 * * * *", "UTC")
    assert trig.trigger_type is TriggerType.CRON


def test_schedule_trigger_empty_expr_rejected():
    with pytest.raises(ValueError, match="expr"):
        ScheduleTrigger(trigger_type=TriggerType.CRON, expr="", timezone="UTC")


def test_schedule_trigger_empty_timezone_rejected():
    with pytest.raises(ValueError, match="timezone"):
        ScheduleTrigger(trigger_type=TriggerType.CRON, expr="* * * * *", timezone="")


def test_schedule_trigger_is_frozen():
    trig = _make_trigger()
    with pytest.raises(FrozenInstanceError):
        trig.expr = "x"  # type: ignore[misc]


def test_schedule_trigger_seconds_ok():
    """v0.2 SECONDS：expr 是正整数秒数文本。"""
    trig = ScheduleTrigger(trigger_type=TriggerType.SECONDS, expr="10", timezone="UTC")
    assert trig.trigger_type is TriggerType.SECONDS
    assert trig.expr == "10"


def test_schedule_trigger_seconds_zero_rejected():
    with pytest.raises(ValueError, match="SECONDS"):
        ScheduleTrigger(trigger_type=TriggerType.SECONDS, expr="0", timezone="UTC")


def test_schedule_trigger_seconds_negative_rejected():
    with pytest.raises(ValueError, match="SECONDS"):
        ScheduleTrigger(trigger_type=TriggerType.SECONDS, expr="-5", timezone="UTC")


def test_schedule_trigger_seconds_non_integer_rejected():
    with pytest.raises(ValueError, match="SECONDS"):
        ScheduleTrigger(trigger_type=TriggerType.SECONDS, expr="abc", timezone="UTC")


# ---------------------------------------------------------------------------
# 4. TaskExecutionPolicy 默认值与边界
# ---------------------------------------------------------------------------


def test_task_execution_policy_defaults():
    policy = TaskExecutionPolicy()
    assert policy.silent_marker_enabled is True
    assert policy.inactivity_timeout_seconds == 600
    assert policy.session_mode is SessionMode.FRESH_SESSION
    assert policy.concurrency_policy is ConcurrencyPolicy.FORBID
    assert policy.misfire_policy is MisfirePolicy.SKIP
    assert policy.max_turns is None
    assert policy.wall_timeout_seconds is None
    assert policy.retry_limit == 0


def test_task_execution_policy_inactivity_timeout_none_allowed():
    """None 表示禁用，应被接受。"""
    policy = TaskExecutionPolicy(inactivity_timeout_seconds=None)
    assert policy.inactivity_timeout_seconds is None


def test_task_execution_policy_inactivity_timeout_zero_allowed():
    """0 也表示禁用，应被接受。"""
    policy = TaskExecutionPolicy(inactivity_timeout_seconds=0)
    assert policy.inactivity_timeout_seconds == 0


def test_task_execution_policy_inactivity_timeout_negative_rejected():
    with pytest.raises(ValueError, match="inactivity_timeout_seconds"):
        TaskExecutionPolicy(inactivity_timeout_seconds=-1)


def test_task_execution_policy_wall_timeout_negative_rejected():
    with pytest.raises(ValueError, match="wall_timeout_seconds"):
        TaskExecutionPolicy(wall_timeout_seconds=-5)


def test_task_execution_policy_max_turns_negative_rejected():
    with pytest.raises(ValueError, match="max_turns"):
        TaskExecutionPolicy(max_turns=-1)


def test_task_execution_policy_max_turns_none_allowed():
    policy = TaskExecutionPolicy(max_turns=None)
    assert policy.max_turns is None


def test_task_execution_policy_retry_limit_negative_rejected():
    with pytest.raises(ValueError, match="retry_limit"):
        TaskExecutionPolicy(retry_limit=-1)


# ---------------------------------------------------------------------------
# 5. ScheduledTask 不变量
# ---------------------------------------------------------------------------


def test_scheduled_task_disabled_with_scheduled_state_rejected():
    with pytest.raises(ValueError, match="enabled=False"):
        _make_task(enabled=False, state=TaskState.SCHEDULED)


def test_scheduled_task_disabled_with_paused_state_ok():
    task = _make_task(enabled=False, state=TaskState.PAUSED)
    assert task.enabled is False
    assert task.state is TaskState.PAUSED


def test_scheduled_task_disabled_with_disabled_state_ok():
    task = _make_task(enabled=False, state=TaskState.DISABLED)
    assert task.state is TaskState.DISABLED


def test_scheduled_task_disabled_with_completed_state_ok():
    task = _make_task(enabled=False, state=TaskState.COMPLETED)
    assert task.state is TaskState.COMPLETED


def test_scheduled_task_enabled_with_running_state_ok():
    task = _make_task(enabled=True, state=TaskState.RUNNING)
    assert task.enabled is True
    assert task.state is TaskState.RUNNING


def test_scheduled_task_empty_task_id_rejected():
    with pytest.raises(ValueError, match="task_id"):
        _make_task(task_id="")


def test_scheduled_task_empty_name_rejected():
    with pytest.raises(ValueError, match="name"):
        _make_task(name="")


def test_scheduled_task_empty_created_by_rejected():
    with pytest.raises(ValueError, match="created_by"):
        _make_task(created_by="")


def test_scheduled_task_trigger_wrong_type_rejected():
    """trigger 传 dict 而非 ScheduleTrigger 实例 → TypeError。"""
    with pytest.raises(TypeError, match="trigger"):
        ScheduledTask(
            task_id="t1",
            name="task-1",
            enabled=True,
            state=TaskState.SCHEDULED,
            origin=TaskOrigin.CLI,
            trigger={"trigger_type": "cron"},  # type: ignore[arg-type]
            policy=_make_policy(),
            target=_make_target(),
            next_run_at=None,
            last_run_at=None,
            created_by="u",
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-01T00:00:00Z",
        )


# ---------------------------------------------------------------------------
# 6. ScheduledRun 字段
# ---------------------------------------------------------------------------


def _make_run(**overrides) -> ScheduledRun:
    base = dict(
        run_id="r1",
        task_id="t1",
        status=RunStatus.RUNNING,
        scheduled_for="2026-05-01T00:00:00Z",
        started_at=None,
        finished_at=None,
        session_id=None,
        result_status=None,
        final_message_excerpt=None,
        error_message=None,
        failure_reason=None,
        delivery_error=None,
        silent_suppressed=False,
    )
    base.update(overrides)
    return ScheduledRun(**base)  # type: ignore[arg-type]


def test_scheduled_run_defaults_silent_suppressed_false():
    run = _make_run()
    assert run.silent_suppressed is False
    assert run.failure_reason is None
    assert run.delivery_error is None


def test_scheduled_run_empty_run_id_rejected():
    with pytest.raises(ValueError, match="run_id"):
        _make_run(run_id="")


def test_scheduled_run_empty_scheduled_for_rejected():
    """scheduled_for=None 应被拒（必填 ISO8601 str）。"""
    with pytest.raises(ValueError, match="scheduled_for"):
        _make_run(scheduled_for=None)


def test_scheduled_run_status_wrong_type_rejected():
    with pytest.raises(TypeError, match="status"):
        _make_run(status="running")  # 字符串不是 RunStatus 枚举


# ---------------------------------------------------------------------------
# 7. DueTaskReservation / TaskExecutionContext / ScheduledRunRequest
# ---------------------------------------------------------------------------


def test_due_task_reservation_ok():
    res = DueTaskReservation(
        task=_make_task(),
        scheduled_for="2026-05-01T00:00:00Z",
        reserved_at="2026-05-01T00:00:01Z",
    )
    assert res.task.task_id == "t1"


def test_due_task_reservation_missing_scheduled_for_rejected():
    with pytest.raises(ValueError, match="scheduled_for"):
        DueTaskReservation(
            task=_make_task(),
            scheduled_for=None,  # type: ignore[arg-type]
            reserved_at="2026-05-01T00:00:01Z",
        )


def test_due_task_reservation_missing_reserved_at_rejected():
    with pytest.raises(ValueError, match="reserved_at"):
        DueTaskReservation(
            task=_make_task(),
            scheduled_for="2026-05-01T00:00:00Z",
            reserved_at=None,  # type: ignore[arg-type]
        )


def test_task_execution_context_is_scheduled_run_default_true_via_explicit():
    """文档约定 cron 上下文默认 is_scheduled_run=True；本字段没有默认值，
    但显式传 True 时应当 OK，且 frozen。"""
    ctx = _make_exec_context()
    assert ctx.is_scheduled_run is True


def test_task_execution_context_empty_task_id_rejected():
    with pytest.raises(ValueError, match="task_id"):
        TaskExecutionContext(
            task_id="",
            scheduled_for="2026-05-01T00:00:00Z",
            trigger_type=TriggerType.CRON,
            origin=TaskOrigin.CLI,
            is_scheduled_run=True,
        )


def test_task_execution_context_missing_scheduled_for_rejected():
    with pytest.raises(ValueError, match="scheduled_for"):
        TaskExecutionContext(
            task_id="t1",
            scheduled_for=None,  # type: ignore[arg-type]
            trigger_type=TriggerType.CRON,
            origin=TaskOrigin.CLI,
            is_scheduled_run=True,
        )


def test_scheduled_run_request_ok():
    req = ScheduledRunRequest(
        user_input="hi",
        agent_name="default",
        session_id="s1",
        run_id="r1",
        max_turns_override=None,
        execution_context=_make_exec_context(),
    )
    assert req.run_id == "r1"
    assert req.max_turns_override is None


def test_scheduled_run_request_empty_agent_name_rejected():
    with pytest.raises(ValueError, match="agent_name"):
        ScheduledRunRequest(
            user_input="hi",
            agent_name="",
            session_id="s1",
            run_id="r1",
            max_turns_override=None,
            execution_context=_make_exec_context(),
        )


def test_scheduled_run_request_empty_session_id_rejected():
    with pytest.raises(ValueError, match="session_id"):
        ScheduledRunRequest(
            user_input="hi",
            agent_name="a",
            session_id="",
            run_id="r1",
            max_turns_override=None,
            execution_context=_make_exec_context(),
        )


def test_scheduled_run_request_max_turns_override_negative_rejected():
    with pytest.raises(ValueError, match="max_turns_override"):
        ScheduledRunRequest(
            user_input="hi",
            agent_name="a",
            session_id="s1",
            run_id="r1",
            max_turns_override=-1,
            execution_context=_make_exec_context(),
        )


def test_scheduled_run_request_execution_context_wrong_type_rejected():
    with pytest.raises(TypeError, match="execution_context"):
        ScheduledRunRequest(
            user_input="hi",
            agent_name="a",
            session_id="s1",
            run_id="r1",
            max_turns_override=None,
            execution_context={"task_id": "t1"},  # type: ignore[arg-type]
        )
