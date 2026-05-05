"""unit：scheduler.safety_wrapper.ScheduleApprovalProvider（Wave B3）。

覆盖矩阵：

1. fail-closed：inner 返回 ``decision_class=explicit_consent`` → 包装层改判
   ``rejected``，metadata 注入 ``cron_fail_closed`` / ``original_decision_class``
   / ``original_outcome`` / ``cron_task_id``，``reason`` 含 fail-closed + tool_name
2. silent_allow 透传：``decision_class=silent_allow`` → 原样返回
3. hard_block 透传：``decision_class=hard_block`` → 原样返回
4. 无 metadata / 无 ``decision_class`` → 不命中 consent，原样透传
5. inner 抛异常 → 包装层不吞，原样向上抛
6. inner ``cancelled`` + consent → 仍走 fail-closed（按 decision_class 命中）
7. ``ScheduleApprovalProvider`` frozen=True → 写字段抛 FrozenInstanceError
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, field
from typing import Any

import pytest

from core.contracts import ApprovalDecision, ApprovalRequest
from scheduler.safety_wrapper import ScheduleApprovalProvider

# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


@dataclass
class FakeInner:
    """可配置的 inner ApprovalProvider stub。"""

    decision: ApprovalDecision | None = None
    raise_exc: BaseException | None = None
    captured: list[ApprovalRequest] = field(default_factory=list)

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        self.captured.append(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        assert self.decision is not None
        return self.decision


def _make_request(tool_name: str = "shell") -> ApprovalRequest:
    return ApprovalRequest(
        run_id="run-1",
        session_id="sess-1",
        turn=1,
        call_id="call-1",
        tool_name=tool_name,
        arguments={"command": "rm -rf /tmp/x"},
        reason="test",
        metadata={},
    )


# ---------------------------------------------------------------------------
# §1 fail-closed 路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_closed_when_consent_required() -> None:
    """consent → 包装后 outcome=rejected，metadata 注入完整 fail-closed 标记。"""
    inner = FakeInner(
        decision=ApprovalDecision(
            outcome="approved",
            reason="user said yes",
            metadata={
                "decision_class": "explicit_consent",
                "decision_source": "standard",
                "matched_rule": "approval:shell",
            },
        )
    )
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-42")
    request = _make_request(tool_name="shell")

    decision = await wrapped.decide(request)

    # outcome 被翻转
    assert decision.outcome == "rejected"
    # metadata 全套 fail-closed 标记
    assert decision.metadata["cron_fail_closed"] is True
    assert decision.metadata["original_decision_class"] == "explicit_consent"
    assert decision.metadata["original_outcome"] == "approved"
    assert decision.metadata["cron_task_id"] == "cron-task-42"
    # 保留 inner 原始 metadata
    assert decision.metadata["decision_class"] == "explicit_consent"
    assert decision.metadata["matched_rule"] == "approval:shell"
    # reason 含 fail-closed 与 tool_name
    assert decision.reason is not None
    assert "fail-closed" in decision.reason
    assert "shell" in decision.reason


# ---------------------------------------------------------------------------
# §2 silent_allow 透传
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_silent_allow_passthrough() -> None:
    """silent_allow → 原样透传（同对象 / 字段相等）。"""
    inner_decision = ApprovalDecision(
        outcome="approved",
        reason="trusted path",
        metadata={
            "decision_class": "silent_allow",
            "decision_source": "intrinsic",
        },
    )
    inner = FakeInner(decision=inner_decision)
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-1")

    decision = await wrapped.decide(_make_request())

    # 实现是直接 return decision，应是同一对象
    assert decision is inner_decision
    assert decision.outcome == "approved"
    assert decision.metadata == {
        "decision_class": "silent_allow",
        "decision_source": "intrinsic",
    }
    # 不应注入 fail-closed 标记
    assert "cron_fail_closed" not in decision.metadata
    assert "cron_task_id" not in decision.metadata


# ---------------------------------------------------------------------------
# §3 hard_block 透传
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hard_block_passthrough() -> None:
    """hard_block → outcome 仍 rejected，metadata 不被增改。"""
    inner_decision = ApprovalDecision(
        outcome="rejected",
        reason="blocked by policy",
        metadata={
            "decision_class": "hard_block",
            "matched_rule": "deny:rm -rf /",
        },
    )
    inner = FakeInner(decision=inner_decision)
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-7")

    decision = await wrapped.decide(_make_request())

    assert decision is inner_decision
    assert decision.outcome == "rejected"
    assert decision.metadata == {
        "decision_class": "hard_block",
        "matched_rule": "deny:rm -rf /",
    }
    assert "cron_fail_closed" not in decision.metadata


# ---------------------------------------------------------------------------
# §4 无 metadata / 无 decision_class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_decision_class_passthrough() -> None:
    """metadata 为空 → 不命中 consent 分支，原样透传。"""
    inner_decision = ApprovalDecision(
        outcome="approved",
        reason="legacy approval path",
        metadata={},
    )
    inner = FakeInner(decision=inner_decision)
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-x")

    decision = await wrapped.decide(_make_request())

    assert decision is inner_decision
    assert decision.outcome == "approved"
    assert decision.metadata == {}


@pytest.mark.asyncio
async def test_metadata_without_decision_class_key_passthrough() -> None:
    """metadata 有别的键但没 decision_class → 不命中 consent 分支。"""
    inner_decision = ApprovalDecision(
        outcome="approved",
        reason="legacy",
        metadata={"some_other_field": "x", "matched_rule": "foo"},
    )
    inner = FakeInner(decision=inner_decision)
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-y")

    decision = await wrapped.decide(_make_request())

    assert decision is inner_decision
    assert "cron_fail_closed" not in decision.metadata


# ---------------------------------------------------------------------------
# §5 inner 抛异常
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inner_exception_propagates() -> None:
    """inner.decide 抛 RuntimeError → 包装层不吞异常。"""
    sentinel = RuntimeError("inner blew up")
    inner = FakeInner(raise_exc=sentinel)
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-err")

    with pytest.raises(RuntimeError) as excinfo:
        await wrapped.decide(_make_request())

    assert excinfo.value is sentinel
    # inner 仍被调用一次
    assert len(inner.captured) == 1


# ---------------------------------------------------------------------------
# §6 inner outcome=cancelled + consent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_with_consent_class_still_fail_closed() -> None:
    """实现按 decision_class 命中即 fail-closed，与 outcome 无关；
    cancelled + explicit_consent 也走 fail-closed 分支。
    original_outcome 应保留为 cancelled。"""
    inner_decision = ApprovalDecision(
        outcome="cancelled",
        reason="user closed dialog",
        metadata={"decision_class": "explicit_consent"},
    )
    inner = FakeInner(decision=inner_decision)
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-cancel")

    decision = await wrapped.decide(_make_request(tool_name="write_file"))

    assert decision.outcome == "rejected"
    assert decision.metadata["cron_fail_closed"] is True
    assert decision.metadata["original_outcome"] == "cancelled"
    assert decision.metadata["original_decision_class"] == "explicit_consent"
    assert decision.metadata["cron_task_id"] == "cron-task-cancel"
    assert decision.reason is not None
    assert "fail-closed" in decision.reason
    assert "write_file" in decision.reason


# ---------------------------------------------------------------------------
# §7 frozen dataclass
# ---------------------------------------------------------------------------


def test_frozen_dataclass_rejects_field_assignment() -> None:
    """ScheduleApprovalProvider 是 frozen=True，赋值字段必须抛 FrozenInstanceError。"""
    inner = FakeInner(
        decision=ApprovalDecision(outcome="approved", metadata={}),
    )
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-z")

    with pytest.raises(FrozenInstanceError):
        wrapped.task_id = "new-id"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# §8 inner 收到 request 是同一对象（不被包装层篡改）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_is_passed_through_untouched() -> None:
    """包装层只改 decision，不改 request。"""
    inner = FakeInner(
        decision=ApprovalDecision(
            outcome="approved",
            metadata={"decision_class": "silent_allow"},
        )
    )
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-passthrough")
    request = _make_request(tool_name="read_file")

    await wrapped.decide(request)

    assert len(inner.captured) == 1
    assert inner.captured[0] is request


# ---------------------------------------------------------------------------
# §9 metadata 是新 dict（不写穿 inner 原对象）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_closed_does_not_mutate_inner_metadata() -> None:
    """fail-closed 路径必须基于 dict() 拷贝，不能写穿 inner decision 的 metadata。"""
    inner_meta: dict[str, Any] = {"decision_class": "explicit_consent"}
    inner_decision = ApprovalDecision(
        outcome="approved",
        metadata=inner_meta,
    )
    inner = FakeInner(decision=inner_decision)
    wrapped = ScheduleApprovalProvider(inner=inner, task_id="cron-task-iso")

    decision = await wrapped.decide(_make_request())

    # inner 原 metadata 不被污染
    assert "cron_fail_closed" not in inner_meta
    assert "cron_task_id" not in inner_meta
    assert inner_meta == {"decision_class": "explicit_consent"}
    # 包装后是新 dict，含 fail-closed 标记
    assert decision.metadata is not inner_meta
    assert decision.metadata["cron_fail_closed"] is True
