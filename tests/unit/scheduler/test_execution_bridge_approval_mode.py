"""ExecutionBridge approval mode 解析装配单测（v0.5 阶段 C）。

只针对新抽出的 :meth:`ExecutionBridge._build_approval_wrapper` 做白盒装配测试：
不跑 runner、不写 store、不动 watchdog——直接断言 wrapper 的 ``mode`` 字段
按 ``task > global > FAIL_CLOSED`` 优先级解析正确，以及 ``event_sink`` 字段
按 ``len(event_sinks)`` 是否聚合。

5 用例覆盖优先级矩阵 P1～P5 + 2 个 event_sink 装配断言。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from application.scheduled_runs.execution_bridge import (
    ExecutionBridge,
    _AggregateEventSink,
    _CronAuditWriterSink,
)
from core.contracts import (
    ApprovalDecision,
    ApprovalProvider,
    ApprovalRequest,
    EventSink,
)
from infrastructure.config.models import (
    Config,
    ModelSelectionConfig,
    SchedulerApprovalConfig,
    SchedulerConfig,
)
from scheduler.domain import (
    ApprovalMode,
    ConcurrencyPolicy,
    MisfirePolicy,
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(approval_mode: ApprovalMode | None) -> ScheduledTask:
    """构造一个最小合法 :class:`ScheduledTask`，只在 policy.approval_mode 上变化。"""
    return ScheduledTask(
        task_id="t-1",
        name="test",
        lifecycle=TaskLifecycleState.SCHEDULED,
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
            approval_mode=approval_mode,
        ),
        target=TaskTarget(
            agent_name="default",
            input_text="ping",
            metadata={},
        ),
        next_run_at=None,
        last_run_at=None,
        created_by="test",
        created_at="2026-05-17T00:00:00Z",
        updated_at="2026-05-17T00:00:00Z",
    )


def _config_with_mode(mode: str) -> Config:
    """构造一个最小 :class:`Config`，其 ``scheduler.approval.mode = mode``。"""
    return Config(
        model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"),
        scheduler=SchedulerConfig(
            approval=SchedulerApprovalConfig(mode=mode),  # type: ignore[arg-type]
        ),
    )


def _make_bridge(
    *,
    base_config: Config | None,
    event_sinks: Sequence[EventSink] = (),
    tmp_path: Path | None = None,
    inner_approval: ApprovalProvider | None = None,
    interactive_approval_factory: (
        Callable[[ScheduledTask], ApprovalProvider | None] | None
    ) = None,
) -> ExecutionBridge:
    """构造最小可用 :class:`ExecutionBridge`，仅用于跑 ``_build_approval_wrapper``。

    其他构造参数（runner / llm / tools / session_factory / agent_spec）均用
    MagicMock 占位——本测试不触发 ``execute`` / ``_drive_runner``，不会用到。
    store 用真实的 :class:`Store`（构造便宜），避免 mock store API 漂移风险。
    """
    store = Store(home_dir=tmp_path / "cron") if tmp_path is not None else MagicMock(spec=Store)

    return ExecutionBridge(
        runtime=MagicMock(),
        llm=MagicMock(),
        tools={},  # ToolLookup 协议：dict 即满足
        enabled_tool_names=(),
        inner_approval=inner_approval or MagicMock(),
        session_factory=lambda sid: MagicMock(),
        event_sinks=event_sinks,
        agent_spec=MagicMock(),
        store=store,
        base_config=base_config,
        interactive_approval_factory=interactive_approval_factory,
    )


class _HardBlockSafetyApproval:
    """模拟运行时安全门户；人工终点可替换，HardBlock 决策保持自有。"""

    def __init__(self) -> None:
        self.interactive_approval: ApprovalProvider | None = None

    def with_interactive_approval(
        self,
        interactive_approval: ApprovalProvider,
    ) -> ApprovalProvider:
        self.interactive_approval = interactive_approval
        return self

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        del request
        return ApprovalDecision(
            outcome="rejected",
            reason="hard block",
            metadata={"decision_class": "hard_block"},
        )


class _AllowApproval:
    """若被直接当作 inner 使用会错误放行。"""

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        del request
        return ApprovalDecision(outcome="approved")


# ---------------------------------------------------------------------------
# P1～P5 优先级矩阵
# ---------------------------------------------------------------------------


def test_p1_task_none_global_fail_closed_resolves_fail_closed(tmp_path: Path) -> None:
    """P1: task.approval_mode=None + global=fail_closed → effective=FAIL_CLOSED。"""
    bridge = _make_bridge(
        base_config=_config_with_mode("fail_closed"),
        tmp_path=tmp_path,
    )
    task = _make_task(approval_mode=None)
    wrapper = bridge._build_approval_wrapper(task)
    assert wrapper.mode is ApprovalMode.FAIL_CLOSED


def test_p2_task_none_global_trust_resolves_trust(tmp_path: Path) -> None:
    """P2: task.approval_mode=None + global=trust → effective=TRUST。"""
    bridge = _make_bridge(
        base_config=_config_with_mode("trust"),
        tmp_path=tmp_path,
    )
    task = _make_task(approval_mode=None)
    wrapper = bridge._build_approval_wrapper(task)
    assert wrapper.mode is ApprovalMode.TRUST


def test_p3_task_trust_overrides_global_fail_closed(tmp_path: Path) -> None:
    """P3: task.approval_mode=TRUST 覆盖 global=fail_closed → effective=TRUST。"""
    bridge = _make_bridge(
        base_config=_config_with_mode("fail_closed"),
        tmp_path=tmp_path,
    )
    task = _make_task(approval_mode=ApprovalMode.TRUST)
    wrapper = bridge._build_approval_wrapper(task)
    assert wrapper.mode is ApprovalMode.TRUST


def test_p4_task_fail_closed_overrides_global_trust(tmp_path: Path) -> None:
    """P4: task.approval_mode=FAIL_CLOSED 覆盖 global=trust → effective=FAIL_CLOSED。"""
    bridge = _make_bridge(
        base_config=_config_with_mode("trust"),
        tmp_path=tmp_path,
    )
    task = _make_task(approval_mode=ApprovalMode.FAIL_CLOSED)
    wrapper = bridge._build_approval_wrapper(task)
    assert wrapper.mode is ApprovalMode.FAIL_CLOSED


def test_p5_no_base_config_falls_back_to_trust(tmp_path: Path) -> None:
    """P5: base_config=None（CLI 装配缺失）→ effective=TRUST 兜底（v0.5 调整后）。

    与 SchedulerApprovalConfig.mode 默认值（trust）保持一致——cron 即用户
    预批准任务。如果想严格审批必须显式构造 base_config 并设 fail_closed。
    """
    bridge = _make_bridge(base_config=None, tmp_path=tmp_path)
    task = _make_task(approval_mode=None)
    wrapper = bridge._build_approval_wrapper(task)
    assert wrapper.mode is ApprovalMode.TRUST


@pytest.mark.asyncio
async def test_interactive_factory_only_rebinds_runtime_safety_consent(
    tmp_path: Path,
) -> None:
    """Web 人工审批工厂只能替换 consent 终点，运行时 HardBlock 仍是 owner。"""
    safety = _HardBlockSafetyApproval()
    interactive = _AllowApproval()
    bridge = _make_bridge(
        base_config=_config_with_mode("trust"),
        tmp_path=tmp_path,
        inner_approval=safety,
        interactive_approval_factory=lambda task: interactive,
    )
    task = replace(
        _make_task(approval_mode=ApprovalMode.TRUST),
        thread_id="thread-aaaaaaaaaaaa",
    )

    wrapper = bridge._build_approval_wrapper(
        task,
        runtime_approval=safety,
    )
    decision = await wrapper.decide(
        ApprovalRequest(
            run_id="run-safety",
            session_id="session-safety",
            turn=1,
            call_id="call-safety",
            tool_name="run_shell",
            arguments={"command": "danger"},
        )
    )

    assert safety.interactive_approval is interactive
    assert wrapper.inner is safety
    assert decision.outcome == "rejected"
    assert decision.metadata["decision_class"] == "hard_block"


# ---------------------------------------------------------------------------
# event_sink 装配断言
# ---------------------------------------------------------------------------


def test_event_sink_aggregated_from_bridge_event_sinks(tmp_path: Path) -> None:
    """bridge.event_sinks 非空 → wrapper.event_sink 是 _AggregateEventSink，
    内含 audit_writer + 两个 bridge 下游 sink，共 3 个。
    """
    fake_sink_1 = MagicMock(spec=EventSink)
    fake_sink_2 = MagicMock(spec=EventSink)
    bridge = _make_bridge(
        base_config=None,
        event_sinks=[fake_sink_1, fake_sink_2],
        tmp_path=tmp_path,
    )
    task = _make_task(approval_mode=None)
    wrapper = bridge._build_approval_wrapper(task)
    assert wrapper.event_sink is not None
    assert isinstance(wrapper.event_sink, _AggregateEventSink)
    # v0.5 阶段 D：sink 链 = [audit_writer, fake_sink_1, fake_sink_2]
    assert len(wrapper.event_sink.sinks) == 3
    assert isinstance(wrapper.event_sink.sinks[0], _CronAuditWriterSink)
    # 后两个是 bridge 注入的原 sink，顺序保留
    assert wrapper.event_sink.sinks[1] is fake_sink_1
    assert wrapper.event_sink.sinks[2] is fake_sink_2


def test_event_sink_always_aggregated_with_audit_writer(tmp_path: Path) -> None:
    """v0.5 阶段 D：bridge.event_sinks 空时 wrapper.event_sink 仍非 None，
    至少包含 audit_writer 一个 sink，保证 trust 自动放行能写 audit。
    """
    bridge = _make_bridge(base_config=None, event_sinks=(), tmp_path=tmp_path)
    task = _make_task(approval_mode=None)
    wrapper = bridge._build_approval_wrapper(task)
    # v0.5 阶段 D 起 wrapper.event_sink 永远非 None；空 bridge sink 时只含 audit_writer
    assert wrapper.event_sink is not None
    assert isinstance(wrapper.event_sink, _AggregateEventSink)
    assert len(wrapper.event_sink.sinks) == 1
    assert isinstance(wrapper.event_sink.sinks[0], _CronAuditWriterSink)
