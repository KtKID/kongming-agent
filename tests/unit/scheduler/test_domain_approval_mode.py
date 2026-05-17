"""Unit tests for scheduler.domain.ApprovalMode + resolve_effective_mode (v0.5)."""

from __future__ import annotations

import pytest

from scheduler.domain import (
    ApprovalMode,
    TaskExecutionPolicy,
    resolve_effective_mode,
)


def test_approval_mode_enum_values_are_strings() -> None:
    """D1: ApprovalMode 枚举值是字符串字面量，方便 yaml/json 持久化。"""
    assert ApprovalMode.FAIL_CLOSED.value == "fail_closed"
    assert ApprovalMode.TRUST.value == "trust"
    assert isinstance(ApprovalMode.FAIL_CLOSED, str)


def test_task_policy_accepts_none_approval_mode() -> None:
    """D2: TaskExecutionPolicy 默认 approval_mode=None（走全局兜底）。"""
    policy = TaskExecutionPolicy()
    assert policy.approval_mode is None


def test_task_policy_accepts_trust_approval_mode() -> None:
    """D3: 显式声明 ApprovalMode.TRUST 合法。"""
    policy = TaskExecutionPolicy(approval_mode=ApprovalMode.TRUST)
    assert policy.approval_mode is ApprovalMode.TRUST


def test_task_policy_rejects_non_enum_approval_mode() -> None:
    """D2b: approval_mode 必须是 ApprovalMode 或 None，传字符串抛 TypeError。"""
    with pytest.raises(TypeError):
        TaskExecutionPolicy(approval_mode="trust")  # type: ignore[arg-type]


def test_resolve_effective_mode_falls_back_to_global_when_task_is_none() -> None:
    """D4: task_mode is None → 走全局。"""
    assert resolve_effective_mode(None, ApprovalMode.FAIL_CLOSED) is ApprovalMode.FAIL_CLOSED
    assert resolve_effective_mode(None, ApprovalMode.TRUST) is ApprovalMode.TRUST


def test_resolve_effective_mode_task_overrides_global() -> None:
    """D5: task 显式声明优先于全局。"""
    assert (
        resolve_effective_mode(ApprovalMode.TRUST, ApprovalMode.FAIL_CLOSED) is ApprovalMode.TRUST
    )
    assert (
        resolve_effective_mode(ApprovalMode.FAIL_CLOSED, ApprovalMode.TRUST)
        is ApprovalMode.FAIL_CLOSED
    )
