"""Cron audit model context resolution + 注入测试（v0.5.2）。

验证 v0.5.2 加的 ``_resolve_run_audit_context`` helper 在 5 种装配条件下
返回 ``{preset_id, model_name}`` 正确，并被所有 run 相关 audit 调用点
（run_started / _emit_finishing_audit / _record_skipped_by_concurrency /
_CronAuditWriterSink）写入 payload。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from infrastructure.config.models import (
    Config,
    LLMPresetConfig,
    ModelConfig,
)
from scheduler.domain import (
    ConcurrencyPolicy,
    MisfirePolicy,
    RunStatus,
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
from scheduler.execution_bridge import ExecutionBridge, _CronAuditWriterSink

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(preset_id: str = "") -> ScheduledTask:
    return ScheduledTask(
        task_id="t-ctx",
        name="ctx",
        enabled=True,
        state=TaskState.SCHEDULED,
        origin=TaskOrigin.TOOL,
        trigger=ScheduleTrigger(trigger_type=TriggerType.INTERVAL, expr="60", timezone="UTC"),
        policy=TaskExecutionPolicy(
            session_mode=SessionMode.FRESH_SESSION,
            concurrency_policy=ConcurrencyPolicy.FORBID,
            misfire_policy=MisfirePolicy.SKIP,
        ),
        target=TaskTarget(agent_name="default", input_text="ping", metadata={}),
        next_run_at=None,
        last_run_at=None,
        created_by="test",
        created_at="2026-05-17T00:00:00Z",
        updated_at="2026-05-17T00:00:00Z",
        preset_id=preset_id,
    )


def _make_run(status: RunStatus = RunStatus.COMPLETED) -> ScheduledRun:
    return ScheduledRun(
        run_id="r-ctx",
        task_id="t-ctx",
        status=status,
        scheduled_for="2026-05-17T00:00:00Z",
        started_at="2026-05-17T00:00:01Z",
        finished_at="2026-05-17T00:00:02Z",
        session_id="s-ctx",
        result_status=status.value,
        final_message_excerpt=None,
        error_message=None,
        failure_reason=None,
        delivery_error=None,
        silent_suppressed=False,
    )


def _make_config(*, model_name: str = "default-model") -> Config:
    return Config(model=ModelConfig(name=model_name, base_url="http://127.0.0.1:1234/v1"))


def _make_preset(preset_id: str, model_name: str) -> LLMPresetConfig:
    return LLMPresetConfig(
        id=preset_id,
        display_name=preset_id,
        base_url="https://api.example.com/v1",
        model=model_name,
    )


def _make_bridge(
    *,
    base_config: Config | None = None,
    preset_map: dict[str, LLMPresetConfig] | None = None,
    store: Any = None,
) -> ExecutionBridge:
    return ExecutionBridge(
        runner=MagicMock(),
        llm=MagicMock(),
        tools={},
        enabled_tool_names=(),
        inner_approval=MagicMock(),
        session_factory=lambda sid: MagicMock(),
        event_sinks=[],
        agent_spec=MagicMock(),
        store=store or MagicMock(),
        base_config=base_config,
        preset_map=preset_map,
    )


# ---------------------------------------------------------------------------
# _resolve_run_audit_context 5 种情景
# ---------------------------------------------------------------------------


def test_x1_preset_hit_uses_preset_model() -> None:
    """X1: task.preset_id 命中 preset_map → model_name = preset.model（不查 cfg.model）。"""
    bridge = _make_bridge(
        base_config=_make_config(model_name="local-gemma"),
        preset_map={"minimax-m2-7": _make_preset("minimax-m2-7", "MiniMax-M2.7")},
    )
    ctx = bridge._resolve_run_audit_context(_make_task(preset_id="minimax-m2-7"))
    assert ctx["preset_id"] == "minimax-m2-7"
    assert ctx["model_name"] == "MiniMax-M2.7"


def test_x2_empty_preset_falls_back_to_cfg_model() -> None:
    """X2: task.preset_id="" → fallback cfg.model.name。"""
    bridge = _make_bridge(base_config=_make_config(model_name="gemma-4-e4b-it"))
    ctx = bridge._resolve_run_audit_context(_make_task(preset_id=""))
    assert ctx["preset_id"] == ""
    assert ctx["model_name"] == "gemma-4-e4b-it"


def test_x3_unknown_preset_falls_back_to_cfg_model() -> None:
    """X3: task.preset_id 不在 preset_map 中 → fallback cfg.model.name。"""
    bridge = _make_bridge(
        base_config=_make_config(model_name="default-fb"),
        preset_map={"existing-preset": _make_preset("existing-preset", "Other-Model")},
    )
    ctx = bridge._resolve_run_audit_context(_make_task(preset_id="nonexistent"))
    # preset_id 字段是 task 显式声明的，即使未命中 preset_map 也保留原值（审计真实意图）
    assert ctx["preset_id"] == "nonexistent"
    assert ctx["model_name"] == "default-fb"


def test_x4_no_base_config_returns_empty_model_name() -> None:
    """X4: base_config=None 且无 preset → model_name=""。"""
    bridge = _make_bridge(base_config=None)
    ctx = bridge._resolve_run_audit_context(_make_task(preset_id=""))
    assert ctx == {"preset_id": "", "model_name": ""}


def test_x5_preset_hit_with_none_base_config() -> None:
    """X5: 即使 base_config=None，preset 命中仍能拿到 model_name（不需要 cfg）。"""
    bridge = _make_bridge(
        base_config=None,
        preset_map={"glm5": _make_preset("glm5", "glm-5.1")},
    )
    ctx = bridge._resolve_run_audit_context(_make_task(preset_id="glm5"))
    assert ctx == {"preset_id": "glm5", "model_name": "glm-5.1"}


# ---------------------------------------------------------------------------
# _CronAuditWriterSink emit 写入 audit 含 preset_id + model_name
# ---------------------------------------------------------------------------


@dataclass
class _FakeEvent:
    kind: str
    payload: dict[str, Any]


@pytest.mark.asyncio
async def test_x6_sink_writes_preset_and_model_to_audit_payload() -> None:
    """X6: _CronAuditWriterSink emit 时把 preset_id + model_name 写入 audit payload。"""
    store_spy = MagicMock()
    sink = _CronAuditWriterSink(
        store=store_spy,
        task_id="t-audit-1",
        preset_id="minimax-m2-7",
        model_name="MiniMax-M2.7",
    )
    event = _FakeEvent(
        kind="approval.cron.auto_allow",
        payload={
            "tool_name": "run_shell",
            "original_decision_class": "explicit_consent",
            "original_decision_source": "standard",
            "matched_rule": "shell.exec",
            "arguments_digest": "sha256:abc",
        },
    )
    await sink.emit(event)

    store_spy.append_audit.assert_called_once()
    call = store_spy.append_audit.call_args
    payload = call.kwargs["payload"]
    assert payload["preset_id"] == "minimax-m2-7"
    assert payload["model_name"] == "MiniMax-M2.7"
    # 老字段仍在
    assert payload["tool_name"] == "run_shell"
    assert payload["arguments_digest"] == "sha256:abc"


@pytest.mark.asyncio
async def test_x7_sink_default_empty_preset_and_model() -> None:
    """X7: sink 不传 preset_id/model_name 时默认空串（向后兼容）。"""
    store_spy = MagicMock()
    sink = _CronAuditWriterSink(store=store_spy, task_id="t-audit-2")
    event = _FakeEvent(
        kind="approval.cron.auto_allow",
        payload={
            "tool_name": "skill",
            "original_decision_class": "explicit_consent",
            "original_decision_source": "standard",
            "matched_rule": "skill-call-default",
            "arguments_digest": "sha256:xyz",
        },
    )
    await sink.emit(event)

    payload = store_spy.append_audit.call_args.kwargs["payload"]
    assert payload["preset_id"] == ""
    assert payload["model_name"] == ""


# ---------------------------------------------------------------------------
# _emit_finishing_audit 各 status 都注入 ctx
# ---------------------------------------------------------------------------


def _assert_audit_call_has_ctx(call, *, expected_preset: str, expected_model: str) -> None:
    payload = call.kwargs["payload"]
    assert payload["preset_id"] == expected_preset
    assert payload["model_name"] == expected_model


def test_x8_emit_finishing_audit_completed_includes_ctx() -> None:
    """X8: COMPLETED status → run_finished audit 含 ctx。"""
    store_spy = MagicMock()
    bridge = _make_bridge(
        base_config=_make_config(model_name="m1"),
        preset_map={"p1": _make_preset("p1", "ModelA")},
        store=store_spy,
    )
    task = _make_task(preset_id="p1")
    run = _make_run(status=RunStatus.COMPLETED)
    bridge._emit_finishing_audit(task=task, run=run)

    assert store_spy.append_audit.call_count == 1
    _assert_audit_call_has_ctx(
        store_spy.append_audit.call_args, expected_preset="p1", expected_model="ModelA"
    )
    assert store_spy.append_audit.call_args.kwargs["action"] == "run_finished"


def test_x9_emit_finishing_audit_failed_includes_ctx() -> None:
    """X9: FAILED status → run_failed audit 含 ctx。"""
    store_spy = MagicMock()
    bridge = _make_bridge(
        base_config=_make_config(model_name="fb-model"),
        store=store_spy,
    )
    task = _make_task(preset_id="")
    run = _make_run(status=RunStatus.FAILED)
    bridge._emit_finishing_audit(task=task, run=run)

    assert store_spy.append_audit.call_count == 1
    payload = store_spy.append_audit.call_args.kwargs["payload"]
    assert payload["preset_id"] == ""
    assert payload["model_name"] == "fb-model"
    assert store_spy.append_audit.call_args.kwargs["action"] == "run_failed"


def test_x10_emit_finishing_audit_silent_writes_two_with_ctx() -> None:
    """X10: SILENT status 写 2 行 audit（run_silent_suppressed + run_finished），都含 ctx。"""
    store_spy = MagicMock()
    bridge = _make_bridge(
        base_config=_make_config(model_name="silent-model"),
        store=store_spy,
    )
    task = _make_task(preset_id="")
    run = _make_run(status=RunStatus.SILENT)
    bridge._emit_finishing_audit(task=task, run=run)

    assert store_spy.append_audit.call_count == 2
    actions = [c.kwargs["action"] for c in store_spy.append_audit.call_args_list]
    assert actions == ["run_silent_suppressed", "run_finished"]
    for call in store_spy.append_audit.call_args_list:
        payload = call.kwargs["payload"]
        assert payload["preset_id"] == ""
        assert payload["model_name"] == "silent-model"


def test_x11_record_skipped_by_concurrency_includes_ctx() -> None:
    """X11: _record_skipped_by_concurrency 的 audit 含 ctx。"""
    store_spy = MagicMock()
    bridge = _make_bridge(
        base_config=_make_config(model_name="skip-model"),
        preset_map={"abc": _make_preset("abc", "AbcModel")},
        store=store_spy,
    )
    task = _make_task(preset_id="abc")
    bridge._record_skipped_by_concurrency(
        task=task, scheduled_for="2026-05-17T00:00:00Z", reason="locked"
    )

    # 1 次 append_run + 1 次 append_audit
    audit_calls = [c for c in store_spy.append_audit.call_args_list]
    assert len(audit_calls) == 1
    payload = audit_calls[0].kwargs["payload"]
    assert payload["preset_id"] == "abc"
    assert payload["model_name"] == "AbcModel"
    assert audit_calls[0].kwargs["action"] == "run_skipped_by_concurrency"
