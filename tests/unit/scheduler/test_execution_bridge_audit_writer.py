"""ExecutionBridge cron audit writer sink 单测（v0.5 阶段 D）。

验证 :class:`application.scheduled_runs.execution_bridge._CronAuditWriterSink` 在 trust 自动放行
路径下把 ``approval.cron.auto_allow`` event 落成
``store.append_audit(action="run_approval_auto_allow", ...)`` audit 行；
非 trust 路径（fail_closed / hard_block / silent_allow）不写 audit；
store 异常不破坏主决策（防御性 :func:`contextlib.suppress` 生效）。

5 用例 A1～A5 覆盖 trust 写入正路径 + 3 条不写 audit 路径 + 1 条 store 失败兜底。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from application.scheduled_runs.execution_bridge import ExecutionBridge
from core.contracts import ApprovalDecision, ApprovalRequest
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeInner:
    """模拟内层 :class:`ApprovalProvider`。

    ``decision_class`` 决定 wrapper 走 hard_block / explicit_consent / silent_allow 分支；
    ``outcome`` 决定 inner 给的原始结果（trust 模式下被 wrapper 改写为 approved）。
    """

    decision_class: str
    decision_source: str = ""
    outcome: str = "approved"
    matched_rule: str = "default:ask"
    reason: str = "test reason"

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        metadata: dict[str, Any] = {"decision_class": self.decision_class}
        if self.decision_source:
            metadata["decision_source"] = self.decision_source
        if self.matched_rule:
            metadata["matched_rule"] = self.matched_rule
        return ApprovalDecision(
            outcome=self.outcome,
            reason=self.reason,
            metadata=metadata,
        )


def _make_task(approval_mode: ApprovalMode | None) -> ScheduledTask:
    """构造最小合法 :class:`ScheduledTask`，只变化 ``policy.approval_mode``。"""
    return ScheduledTask(
        task_id="t-audit",
        name="audit test",
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


def _make_bridge_with_store_spy(
    inner: FakeInner,
) -> tuple[ExecutionBridge, MagicMock]:
    """构造 bridge + store 探针。

    不实例化真 :class:`scheduler.store.Store`：本测试只读 ``append_audit`` 调用列表，
    用 MagicMock 比真实 Store 简洁也避免文件系统副作用。
    """
    store_spy = MagicMock()
    bridge = ExecutionBridge(
        runtime=MagicMock(),
        llm=MagicMock(),
        tools={},
        enabled_tool_names=(),
        inner_approval=inner,
        session_factory=lambda sid: MagicMock(),
        event_sinks=[],
        agent_spec=MagicMock(),
        store=store_spy,
        base_config=None,
    )
    return bridge, store_spy


def _make_request(tool_name: str = "run_shell") -> ApprovalRequest:
    return ApprovalRequest(
        run_id="r-1",
        session_id="s-1",
        turn=1,
        call_id="c-1",
        tool_name=tool_name,
        arguments={"cmd": "python scripts/x.py"},
    )


# ---------------------------------------------------------------------------
# A1～A5 用例
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a1_trust_consent_writes_audit() -> None:
    """A1: trust + consent 自动放行 → store.append_audit('run_approval_auto_allow')。"""
    inner = FakeInner(
        decision_class="explicit_consent",
        decision_source="standard",
        outcome="rejected",
        matched_rule="default:ask",
    )
    bridge, store_spy = _make_bridge_with_store_spy(inner)
    task = _make_task(approval_mode=ApprovalMode.TRUST)
    wrapper = bridge._build_approval_wrapper(task)

    decision = await wrapper.decide(_make_request())

    # trust 模式：rejected 被 wrapper 覆盖为 approved
    assert decision.outcome == "approved"
    assert decision.metadata.get("cron_trust_mode") is True

    # 找到 append_audit(action="run_approval_auto_allow", ...) 调用
    audit_calls = [
        c
        for c in store_spy.append_audit.call_args_list
        if c.kwargs.get("action") == "run_approval_auto_allow"
    ]
    assert len(audit_calls) == 1
    call = audit_calls[0]
    assert call.kwargs["task_id"] == "t-audit"
    assert call.kwargs["actor"] == "scheduler"
    payload = call.kwargs["payload"]
    assert payload["tool_name"] == "run_shell"
    assert payload["original_decision_class"] == "explicit_consent"
    assert payload["original_decision_source"] == "standard"
    assert payload["matched_rule"] == "default:ask"
    assert isinstance(payload["arguments_digest"], str)
    assert payload["arguments_digest"].startswith("sha256:")


@pytest.mark.asyncio
async def test_a2_fail_closed_consent_does_not_write_audit() -> None:
    """A2: fail_closed + consent rejected → wrapper 不 emit，不写 audit。"""
    inner = FakeInner(
        decision_class="explicit_consent",
        decision_source="standard",
        outcome="rejected",
    )
    bridge, store_spy = _make_bridge_with_store_spy(inner)
    task = _make_task(approval_mode=ApprovalMode.FAIL_CLOSED)
    wrapper = bridge._build_approval_wrapper(task)

    decision = await wrapper.decide(_make_request())

    # fail_closed 路径：consent 被改写为 rejected（带 cron_fail_closed metadata）
    assert decision.outcome == "rejected"
    assert decision.metadata.get("cron_fail_closed") is True

    audit_calls = [
        c
        for c in store_spy.append_audit.call_args_list
        if c.kwargs.get("action") == "run_approval_auto_allow"
    ]
    assert audit_calls == []


@pytest.mark.asyncio
async def test_a3_trust_hard_block_does_not_write_audit() -> None:
    """A3: trust + hard_block → 透传 rejected，wrapper 不 emit，不写 audit。"""
    inner = FakeInner(decision_class="hard_block", outcome="rejected")
    bridge, store_spy = _make_bridge_with_store_spy(inner)
    task = _make_task(approval_mode=ApprovalMode.TRUST)
    wrapper = bridge._build_approval_wrapper(task)

    decision = await wrapper.decide(_make_request())

    # hard_block 永远透传 rejected（即使在 trust 模式）——安全不变量
    assert decision.outcome == "rejected"

    audit_calls = [
        c
        for c in store_spy.append_audit.call_args_list
        if c.kwargs.get("action") == "run_approval_auto_allow"
    ]
    assert audit_calls == []


@pytest.mark.asyncio
async def test_a4_trust_silent_allow_does_not_write_audit() -> None:
    """A4: trust + silent_allow → 透传 approved，wrapper 不 emit，不写 audit。"""
    inner = FakeInner(decision_class="silent_allow", outcome="approved")
    bridge, store_spy = _make_bridge_with_store_spy(inner)
    task = _make_task(approval_mode=ApprovalMode.TRUST)
    wrapper = bridge._build_approval_wrapper(task)

    decision = await wrapper.decide(_make_request())

    # silent_allow 透传 approved，不进 trust 分支（wrapper 只在 consent 上 emit）
    assert decision.outcome == "approved"

    audit_calls = [
        c
        for c in store_spy.append_audit.call_args_list
        if c.kwargs.get("action") == "run_approval_auto_allow"
    ]
    assert audit_calls == []


@pytest.mark.asyncio
async def test_a5_audit_store_failure_does_not_break_decision() -> None:
    """A5: store.append_audit 抛异常 → 主决策仍 approved（contextlib.suppress 生效）。"""
    inner = FakeInner(
        decision_class="explicit_consent",
        decision_source="standard",
        outcome="rejected",
    )
    bridge, store_spy = _make_bridge_with_store_spy(inner)
    store_spy.append_audit.side_effect = RuntimeError("disk full")
    task = _make_task(approval_mode=ApprovalMode.TRUST)
    wrapper = bridge._build_approval_wrapper(task)

    decision = await wrapper.decide(_make_request())

    # 决策没受影响：trust 自动放行仍生效，元数据完整
    assert decision.outcome == "approved"
    assert decision.metadata.get("cron_trust_mode") is True


# ---------------------------------------------------------------------------
# A6/A7: trace sink gap fix —— per-run trace 与 audit jsonl 双份证据对齐
# ---------------------------------------------------------------------------


@dataclass
class RecordingSink:
    """记录 emit 的 event，供 trace sink gap 测试断言。"""

    events: list[Any]

    def __init__(self) -> None:
        self.events = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_a6_per_run_trace_sink_receives_trust_auto_allow_event() -> None:
    """A6: 装配阶段把 per-run trace sink augment 进 wrapper.event_sink 后，
    trust 自动放行 emit 的 approval.cron.auto_allow event 应到达 trace sink。

    覆盖 v0.5 修复的 trace gap：之前 wrapper 只收 bridge 构造期的 sinks，
    不知道 _drive_runner 动态加的 per-run trace；导致 trace 文件看不到
    cron_trust event，跟实际行为割裂。
    """
    inner = FakeInner(
        decision_class="explicit_consent",
        decision_source="standard",
        outcome="rejected",
    )
    bridge, _store_spy = _make_bridge_with_store_spy(inner)
    task = _make_task(approval_mode=ApprovalMode.TRUST)
    wrapper = bridge._build_approval_wrapper(task)

    # 模拟 _drive_runner 创建 per-run trace sink 后 augment 到 wrapper
    trace_sink = RecordingSink()
    wrapper = bridge._augment_wrapper_with_run_trace(wrapper, trace_sink)

    await wrapper.decide(_make_request())

    # trace sink 应收到 1 个 approval.cron.auto_allow event（与 audit 行对齐）
    auto_allow_events = [e for e in trace_sink.events if e.kind == "approval.cron.auto_allow"]
    assert len(auto_allow_events) == 1
    assert auto_allow_events[0].payload["tool_name"] == "run_shell"
    assert auto_allow_events[0].payload["original_decision_class"] == "explicit_consent"


@pytest.mark.asyncio
async def test_a7_augment_with_none_trace_sink_is_noop() -> None:
    """A7: trace_sink=None（trace_dir 未配置）时 augment 不改 wrapper。"""
    inner = FakeInner(decision_class="silent_allow", outcome="approved")
    bridge, _store_spy = _make_bridge_with_store_spy(inner)
    task = _make_task(approval_mode=ApprovalMode.TRUST)
    original_wrapper = bridge._build_approval_wrapper(task)

    augmented = bridge._augment_wrapper_with_run_trace(original_wrapper, None)

    # 同一引用即可——None 不创建新 wrapper
    assert augmented is original_wrapper


@pytest.mark.asyncio
async def test_a8_augment_preserves_existing_aggregate_sinks_order() -> None:
    """A8: augment 后 _AggregateEventSink.sinks 保留原有顺序 + trace_sink 追加在末尾。

    保证：audit_writer 仍在首位（不破坏 D 阶段约定的优先级），原 bridge sinks
    保持中间，新 trace_sink 在末尾。
    """
    from application.scheduled_runs.execution_bridge import (
        _AggregateEventSink,
        _CronAuditWriterSink,
    )

    inner = FakeInner(decision_class="silent_allow", outcome="approved")
    bridge, _store_spy = _make_bridge_with_store_spy(inner)
    task = _make_task(approval_mode=ApprovalMode.TRUST)
    wrapper = bridge._build_approval_wrapper(task)

    trace_sink = RecordingSink()
    augmented = bridge._augment_wrapper_with_run_trace(wrapper, trace_sink)

    assert isinstance(augmented.event_sink, _AggregateEventSink)
    sinks = augmented.event_sink.sinks
    # 首位是 audit_writer（D 阶段约定）
    assert isinstance(sinks[0], _CronAuditWriterSink)
    # 末位是新追加的 trace sink
    assert sinks[-1] is trace_sink
