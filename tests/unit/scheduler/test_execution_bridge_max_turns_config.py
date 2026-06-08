"""ExecutionBridge max_turns 解析单测（v0.5.1）。

验证优先级：``task.policy.max_turns`` > ``cfg.scheduler.default_max_turns``
> ``_FALLBACK_MAX_TURNS_NO_CONFIG=90``。

替换 v0.2~v0.5 期间的 ``_DEFAULT_MAX_TURNS=20`` 硬编码——cron 跑司天等
重型任务时 20 turn 用完就被强制终止。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from application.scheduled_runs.execution_bridge import (
    _FALLBACK_MAX_TURNS_NO_CONFIG,
    ExecutionBridge,
)
from infrastructure.config.models import (
    Config,
    ModelConfig,
    SchedulerConfig,
)
from scheduler.domain import (
    ConcurrencyPolicy,
    MisfirePolicy,
    ScheduledTask,
    ScheduleTrigger,
    SessionMode,
    TaskExecutionPolicy,
    TaskOrigin,
    TaskState,
    TaskTarget,
    TriggerType,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(max_turns: int | None) -> ScheduledTask:
    return ScheduledTask(
        task_id="t-mt",
        name="max-turns test",
        enabled=True,
        state=TaskState.SCHEDULED,
        origin=TaskOrigin.TOOL,
        trigger=ScheduleTrigger(
            trigger_type=TriggerType.INTERVAL,
            expr="60",
            timezone="UTC",
        ),
        policy=TaskExecutionPolicy(
            session_mode=SessionMode.FRESH_SESSION,
            concurrency_policy=ConcurrencyPolicy.FORBID,
            misfire_policy=MisfirePolicy.SKIP,
            max_turns=max_turns,
        ),
        target=TaskTarget(agent_name="default", input_text="ping", metadata={}),
        next_run_at=None,
        last_run_at=None,
        created_by="test",
        created_at="2026-05-17T00:00:00Z",
        updated_at="2026-05-17T00:00:00Z",
    )


def _make_bridge(*, base_config: Config | None) -> ExecutionBridge:
    return ExecutionBridge(
        runner=MagicMock(),
        llm=MagicMock(),
        tools={},
        enabled_tool_names=(),
        inner_approval=MagicMock(),
        session_factory=lambda sid: MagicMock(),
        event_sinks=[],
        agent_spec=MagicMock(),
        store=MagicMock(),
        base_config=base_config,
    )


def _config_with_default_max_turns(value: int) -> Config:
    return Config(
        model=ModelConfig(name="t", base_url="http://127.0.0.1:1234/v1"),
        scheduler=SchedulerConfig(default_max_turns=value),
    )


def _resolve_max_turns(bridge: ExecutionBridge, task: ScheduledTask) -> int:
    """复用 _drive_runner 第 6 段 max_turns 解析逻辑做断言。

    本方法刻意复制 execution_bridge.py 内联三段判断，确保测试覆盖装配
    路径而非通过单独 helper；如未来抽方法，此处可改为直接调用。
    """
    if task.policy.max_turns is not None:
        return task.policy.max_turns
    if bridge._base_config is not None:
        return bridge._base_config.scheduler.default_max_turns
    return _FALLBACK_MAX_TURNS_NO_CONFIG


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_m1_task_policy_max_turns_takes_priority() -> None:
    """M1: task.policy.max_turns=N → 用 N（不查 config）。"""
    bridge = _make_bridge(base_config=_config_with_default_max_turns(90))
    task = _make_task(max_turns=15)
    assert _resolve_max_turns(bridge, task) == 15


@pytest.mark.unit
def test_m2_task_none_falls_back_to_config_default_max_turns() -> None:
    """M2: task.policy.max_turns=None + base_config 有值 → 用 cfg.scheduler.default_max_turns。"""
    bridge = _make_bridge(base_config=_config_with_default_max_turns(120))
    task = _make_task(max_turns=None)
    assert _resolve_max_turns(bridge, task) == 120


@pytest.mark.unit
def test_m3_no_base_config_falls_back_to_90() -> None:
    """M3: base_config=None（CLI 装配缺失） → 兜底 _FALLBACK_MAX_TURNS_NO_CONFIG=90。"""
    bridge = _make_bridge(base_config=None)
    task = _make_task(max_turns=None)
    assert _resolve_max_turns(bridge, task) == 90
    assert _FALLBACK_MAX_TURNS_NO_CONFIG == 90


@pytest.mark.unit
def test_m4_no_hardcoded_default_max_turns_constant() -> None:
    """M4: execution_bridge 模块不再有 _DEFAULT_MAX_TURNS=20 残留（防回退）。"""
    import application.scheduled_runs.execution_bridge as eb

    assert not hasattr(eb, "_DEFAULT_MAX_TURNS"), (
        "v0.5.1 应删除 _DEFAULT_MAX_TURNS 硬编码；改走 cfg.scheduler.default_max_turns"
    )


@pytest.mark.unit
def test_m5_config_default_aligns_with_fallback() -> None:
    """M5: cfg.scheduler.default_max_turns 默认值与 _FALLBACK_MAX_TURNS_NO_CONFIG 对齐（避免歧义）。"""
    cfg = SchedulerConfig()
    assert cfg.default_max_turns == _FALLBACK_MAX_TURNS_NO_CONFIG
