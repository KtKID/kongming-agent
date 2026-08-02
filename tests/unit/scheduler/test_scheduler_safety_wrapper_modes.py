"""Unit tests for ScheduleApprovalProvider mode-based decision matrix (v0.5).

覆盖矩阵：

- U1～U5: ``mode=FAIL_CLOSED`` × {hard_block, silent_allow, standard_allow,
  explicit_consent (rejected), explicit_consent + write_file 白名单}
- U6～U10: ``mode=TRUST`` × {hard_block, silent_allow, standard_allow,
  explicit_consent(standard), explicit_consent(elevated)}
- U11: trust 自动放行时 event_sink 收到 ``approval.cron.auto_allow`` 审计事件
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from core.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    Event,
)
from scheduler.domain import ApprovalMode
from scheduler.safety_wrapper import ScheduleApprovalProvider


@dataclass
class FakeInner:
    """可控的 inner provider：按预设 decision_class 返回 decision。"""

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


@dataclass
class RecordingSink:
    """记录 emit 的 event，便于断言。"""

    events: list[Event] = field(default_factory=list)

    async def emit(self, event: Event) -> None:
        self.events.append(event)


def _make_request(
    tool_name: str = "run_shell",
    arguments: dict[str, Any] | None = None,
) -> ApprovalRequest:
    return ApprovalRequest(
        run_id="run-1",
        session_id="sess-1",
        turn=1,
        call_id="call-1",
        tool_name=tool_name,
        arguments=arguments or {"cmd": "echo hi"},
        reason="test",
        metadata={},
    )


# === U1～U5: fail_closed 模式 ===


@pytest.mark.asyncio
async def test_u1_fail_closed_hard_block_passthrough() -> None:
    """U1: fail_closed + hard_block → 透传 rejected（不带 cron_fail_closed）。"""
    inner = FakeInner(decision_class="hard_block", outcome="rejected")
    wrapper = ScheduleApprovalProvider(
        inner=inner,
        task_id="t1",
        mode=ApprovalMode.FAIL_CLOSED,
    )
    decision = await wrapper.decide(_make_request())
    assert decision.outcome == "rejected"
    assert decision.metadata.get("cron_fail_closed") is not True
    assert decision.metadata.get("cron_trust_mode") is not True
    # 透传：metadata 不应包含 cron_task_id（说明走的不是 fail_closed / trust 分支）
    assert "cron_task_id" not in decision.metadata


@pytest.mark.asyncio
async def test_u2_fail_closed_silent_allow_passthrough() -> None:
    """U2: fail_closed + silent_allow → 透传 approved。"""
    inner = FakeInner(decision_class="silent_allow", outcome="approved")
    wrapper = ScheduleApprovalProvider(
        inner=inner,
        task_id="t2",
        mode=ApprovalMode.FAIL_CLOSED,
    )
    decision = await wrapper.decide(_make_request())
    assert decision.outcome == "approved"
    assert decision.metadata.get("decision_class") == "silent_allow"
    assert "cron_task_id" not in decision.metadata


@pytest.mark.asyncio
async def test_u3_fail_closed_standard_allow_passthrough() -> None:
    """U3: fail_closed + standard_allow → 透传 approved（standard_allow 不属于 consent）。"""
    inner = FakeInner(decision_class="standard_allow", outcome="approved")
    wrapper = ScheduleApprovalProvider(
        inner=inner,
        task_id="t3",
        mode=ApprovalMode.FAIL_CLOSED,
    )
    decision = await wrapper.decide(_make_request())
    assert decision.outcome == "approved"
    assert decision.metadata.get("decision_class") == "standard_allow"
    assert "cron_task_id" not in decision.metadata


@pytest.mark.asyncio
async def test_u4_fail_closed_consent_rejected() -> None:
    """U4: fail_closed + explicit_consent → rejected + cron_fail_closed metadata。"""
    inner = FakeInner(
        decision_class="explicit_consent",
        decision_source="standard",
        outcome="approved",
    )
    wrapper = ScheduleApprovalProvider(
        inner=inner,
        task_id="t4",
        mode=ApprovalMode.FAIL_CLOSED,
    )
    decision = await wrapper.decide(_make_request())
    assert decision.outcome == "rejected"
    assert decision.metadata["cron_fail_closed"] is True
    assert decision.metadata["cron_task_id"] == "t4"
    assert decision.metadata["original_decision_class"] == "explicit_consent"
    assert decision.metadata["original_outcome"] == "approved"


@pytest.mark.asyncio
async def test_fail_closed_consent_passthrough_keeps_user_decision() -> None:
    """绑定通用审批的 cron 任务透传用户审批结果。"""
    inner = FakeInner(
        decision_class="explicit_consent",
        decision_source="standard",
        outcome="approved",
    )
    wrapper = ScheduleApprovalProvider(
        inner=inner,
        task_id="t4-thread",
        mode=ApprovalMode.FAIL_CLOSED,
        consent_passthrough=True,
    )

    decision = await wrapper.decide(_make_request())

    assert decision.outcome == "approved"
    assert decision.metadata["decision_class"] == "explicit_consent"
    assert "cron_fail_closed" not in decision.metadata


@pytest.mark.asyncio
async def test_u5_fail_closed_write_file_whitelist_approved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U5: fail_closed + explicit_consent + write_file 命中白名单 → approved。"""
    monkeypatch.chdir(tmp_path)
    inner = FakeInner(
        decision_class="explicit_consent",
        decision_source="standard",
        outcome="approved",
    )
    wrapper = ScheduleApprovalProvider(
        inner=inner,
        task_id="t5",
        mode=ApprovalMode.FAIL_CLOSED,
    )
    req = _make_request(
        tool_name="write_file",
        arguments={"path": "out.txt", "content": "hi", "append": False},
    )
    decision = await wrapper.decide(req)
    assert decision.outcome == "approved"
    assert decision.metadata["cron_auto_allow"] == "write_file_create"
    assert decision.metadata["decision_source"] == "cron_whitelist"
    assert decision.metadata["cron_task_id"] == "t5"


# === U6～U10: trust 模式 ===


@pytest.mark.asyncio
async def test_u6_trust_hard_block_passthrough() -> None:
    """U6: trust + hard_block → rejected（trust 绝不能绕过 hard_block）。"""
    inner = FakeInner(decision_class="hard_block", outcome="rejected")
    wrapper = ScheduleApprovalProvider(
        inner=inner,
        task_id="t6",
        mode=ApprovalMode.TRUST,
    )
    decision = await wrapper.decide(_make_request())
    assert decision.outcome == "rejected"
    assert decision.metadata.get("cron_trust_mode") is not True
    assert "cron_task_id" not in decision.metadata


@pytest.mark.asyncio
async def test_u7_trust_silent_allow_passthrough() -> None:
    """U7: trust + silent_allow → 透传 approved。"""
    inner = FakeInner(decision_class="silent_allow", outcome="approved")
    wrapper = ScheduleApprovalProvider(
        inner=inner,
        task_id="t7",
        mode=ApprovalMode.TRUST,
    )
    decision = await wrapper.decide(_make_request())
    assert decision.outcome == "approved"
    assert decision.metadata.get("cron_trust_mode") is not True


@pytest.mark.asyncio
async def test_u8_trust_standard_allow_passthrough() -> None:
    """U8: trust + standard_allow → 透传 approved。"""
    inner = FakeInner(decision_class="standard_allow", outcome="approved")
    wrapper = ScheduleApprovalProvider(
        inner=inner,
        task_id="t8",
        mode=ApprovalMode.TRUST,
    )
    decision = await wrapper.decide(_make_request())
    assert decision.outcome == "approved"
    assert decision.metadata.get("cron_trust_mode") is not True


@pytest.mark.asyncio
async def test_u9_trust_consent_standard_auto_allowed() -> None:
    """U9: trust + explicit_consent(standard) → 自动 approved + cron_trust_mode 标记。"""
    inner = FakeInner(
        decision_class="explicit_consent",
        decision_source="standard",
        outcome="approved",
    )
    wrapper = ScheduleApprovalProvider(
        inner=inner,
        task_id="t9",
        mode=ApprovalMode.TRUST,
    )
    decision = await wrapper.decide(_make_request())
    assert decision.outcome == "approved"
    assert decision.metadata["cron_trust_mode"] is True
    assert decision.metadata["decision_class"] == "silent_allow"
    assert decision.metadata["decision_source"] == "cron_trust"
    assert decision.metadata["original_decision_class"] == "explicit_consent"
    assert decision.metadata["original_decision_source"] == "standard"
    assert decision.metadata["cron_task_id"] == "t9"


@pytest.mark.asyncio
async def test_u10_trust_consent_elevated_auto_allowed() -> None:
    """U10: trust + explicit_consent(elevated) → 自动 approved（elevated 也放）。"""
    inner = FakeInner(
        decision_class="explicit_consent",
        decision_source="elevated",
        outcome="approved",
    )
    wrapper = ScheduleApprovalProvider(
        inner=inner,
        task_id="t10",
        mode=ApprovalMode.TRUST,
    )
    decision = await wrapper.decide(_make_request())
    assert decision.outcome == "approved"
    assert decision.metadata["cron_trust_mode"] is True
    assert decision.metadata["original_decision_source"] == "elevated"


# === U11: event_sink emit 断言 ===


@pytest.mark.asyncio
async def test_u11_trust_consent_emits_audit_event() -> None:
    """U11: trust + consent 自动放行时 event_sink 收到 1 个 approval.cron.auto_allow event。"""
    inner = FakeInner(
        decision_class="explicit_consent",
        decision_source="standard",
        outcome="approved",
        matched_rule="default:ask",
        reason="no approval rule matched",
    )
    sink = RecordingSink()
    wrapper = ScheduleApprovalProvider(
        inner=inner,
        task_id="t11",
        mode=ApprovalMode.TRUST,
        event_sink=sink,
    )
    await wrapper.decide(
        _make_request(tool_name="run_shell", arguments={"cmd": "python scripts/x.py"})
    )

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.kind == "approval.cron.auto_allow"
    payload = event.payload
    assert payload["task_id"] == "t11"
    assert payload["tool_name"] == "run_shell"
    assert payload["original_decision_class"] == "explicit_consent"
    assert payload["original_decision_source"] == "standard"
    assert payload["matched_rule"] == "default:ask"
    assert payload["reason"] == "no approval rule matched"
    assert isinstance(payload["arguments_digest"], str)
    assert payload["arguments_digest"].startswith("sha256:")
