"""compute_run_end_reason 单测：Result → RunEndReason bitmask 推导（错误分类器真源）。

覆盖四条收口路径：completed / cancelled / failed(max_turns) / failed(error)，
以及叠加语义（bitmask 可或运算）。
"""

from __future__ import annotations

from core.errors import AgentError, MaxTurnsExceededError, ProviderError
from core.result import Result, ResultStatus, RunEndReason, compute_run_end_reason


def _result(
    *,
    status: ResultStatus,
    error: AgentError | None = None,
    metadata: dict[str, object] | None = None,
) -> Result:
    """构造测试用 Result（绕过 frozen 默认值不便的写法）。"""
    return Result(
        run_id="run-test",
        session_id="sess-test",
        status=status,
        final_message=None,
        turn_count=0,
        error=error,
        metadata=metadata if metadata is not None else {},
    )


# ---------------------------------------------------------------------------
# 单一自然因映射（三选一互斥）
# ---------------------------------------------------------------------------


def test_completed_maps_to_complete() -> None:
    """status=completed → COMPLETE(1)。"""
    result = _result(status="completed")
    assert compute_run_end_reason(result) == RunEndReason.COMPLETE
    assert int(compute_run_end_reason(result)) == 1


def test_cancelled_maps_to_interrupt() -> None:
    """status=cancelled → INTERRUPT(8)。cancel 原因典型 = 用户点停止。"""
    result = _result(status="cancelled", metadata={"cancel_reason": "user_interrupt"})
    assert compute_run_end_reason(result) == RunEndReason.INTERRUPT
    assert int(compute_run_end_reason(result)) == 8


def test_failed_max_turns_maps_to_max_turns() -> None:
    """status=failed + MaxTurnsExceededError → MAX_TURNS(2)，不是 ERROR。

    这是 bug 的核心修复点：旧实现把 max_turns 和真 error 都打成 failed，
    下游无法区分「预算耗尽」和「东西坏了」。
    """
    error = MaxTurnsExceededError("exceeded", details={"max_turns": 10})
    result = _result(status="failed", error=error)
    assert compute_run_end_reason(result) == RunEndReason.MAX_TURNS
    assert int(compute_run_end_reason(result)) == 2


def test_failed_provider_error_maps_to_error() -> None:
    """status=failed + ProviderError（真错误）→ ERROR(4)。"""
    error = ProviderError("500 error", details={"status_code": 500})
    result = _result(status="failed", error=error)
    assert compute_run_end_reason(result) == RunEndReason.ERROR
    assert int(compute_run_end_reason(result)) == 4


def test_failed_generic_agent_error_maps_to_error() -> None:
    """status=failed + 普通 AgentError → ERROR(4)。"""
    error = AgentError("something broke")
    result = _result(status="failed", error=error)
    assert compute_run_end_reason(result) == RunEndReason.ERROR


def test_failed_no_error_maps_to_error() -> None:
    """status=failed 但 error=None（理论不该出现）→ ERROR(4) 兜底。"""
    result = _result(status="failed", error=None)
    assert compute_run_end_reason(result) == RunEndReason.ERROR


# ---------------------------------------------------------------------------
# bitmask 可或运算语义（叠加场景验证）
# ---------------------------------------------------------------------------


def test_bitmask_or_combination() -> None:
    """COMPLETE | INTERRUPT 可叠加（bitmask 核心特性）。

    场景：模型刚给出终态，用户同时点了停止。审计层诚实记录两个因。
    """
    combined = RunEndReason.COMPLETE | RunEndReason.INTERRUPT
    assert int(combined) == 9  # 1 + 8
    assert RunEndReason.INTERRUPT in combined
    assert RunEndReason.COMPLETE in combined
    assert RunEndReason.ERROR not in combined


def test_bitmask_natural_causes_mutually_exclusive() -> None:
    """自然因（COMPLETE / MAX_TURNS / ERROR）语义上互斥——compute 不会同时返回两个。

    turn 循环同一迭代的三条互斥出口：正常返回 / 预算耗尽 / 真失败。
    """
    # compute 对每个 status 只返回一个自然因位
    assert compute_run_end_reason(_result(status="completed")) == RunEndReason.COMPLETE
    assert compute_run_end_reason(_result(status="cancelled")) == RunEndReason.INTERRUPT
    max_turns_result = _result(status="failed", error=MaxTurnsExceededError("x", details={}))
    assert compute_run_end_reason(max_turns_result) == RunEndReason.MAX_TURNS
    error_result = _result(status="failed", error=ProviderError("x", details={}))
    assert compute_run_end_reason(error_result) == RunEndReason.ERROR


def test_none_is_zero() -> None:
    """NONE = 0（未结束 / 无因），用于「status > 0 才复位按钮」的判断。"""
    assert int(RunEndReason.NONE) == 0
