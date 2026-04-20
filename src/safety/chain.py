"""高层安全链：capability → permission → approval。

本模块只做一件事：把 :class:`safety.capability_policy.CapabilityPolicy`、
:class:`safety.permission_policy.PermissionPolicy` 和底层
:class:`core.contracts.ApprovalProvider` 三层串成一个**对外只暴露
ApprovalProvider Protocol** 的复合对象 :class:`SafetyGatedApproval`。

这样做的目的：

- ``core.runner`` / ``tools/`` / ``host/`` / ``cli/`` 只认
  :class:`core.contracts.ApprovalProvider` 单一协议面，不直接 touch safety 内部
  policy，符合 ``10-contracts.md`` 中"Capability 与 Permission"硬性约束。
- 装配层（``executors/agent_runtime/native_runtime.py``）调用
  :func:`build_safety_chain` 一次性串起来，形成最终 approval provider。
- 判定顺序固定：**capability → permission → approval**。任何一层"硬否决"都
  会短路后续层。

:class:`SafetyGatedApproval` **不**在装配时抛 :class:`core.errors.CapabilityDenied`
或 :class:`core.errors.PermissionDenied`——它把拒绝理由转成
``ApprovalDecision(outcome="rejected", ...)``，由 runner 统一处理 approval
拒绝路径（runner 会按 :class:`core.errors.ApprovalRejected` 规约收口）。
如果日后确有需求需要把安全链产生的"拒绝"区分自 runner 的 approval 语义，
再通过 ``metadata["stage"]`` 字段恢复（已写入）。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

# 延迟 import 以避免循环：Config 仅在类型注解里使用即可
from config_loader.models import Config
from core.contracts import (
    ApprovalDecision,
    ApprovalProvider,
    ApprovalRequest,
)
from core.errors import AgentError
from safety.capability_policy import CapabilityPolicy
from safety.permission_policy import PermissionPolicy, PermissionRule

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class SafetyChainError(AgentError):
    """安全链内部异常。

    仅用于"安全链自身出错"——例如底层 approval provider 抛了未预料的异常。
    capability / permission 层的"拒绝"不走这个异常，而是转成
    :class:`ApprovalDecision(outcome="rejected")`。
    """


# ---------------------------------------------------------------------------
# 内部 helper
# ---------------------------------------------------------------------------


def _approved(*, reason: str, metadata: dict[str, Any]) -> ApprovalDecision:
    return ApprovalDecision(
        outcome="approved",
        reason=reason,
        metadata=dict(metadata),
    )


def _rejected(*, reason: str, metadata: dict[str, Any]) -> ApprovalDecision:
    return ApprovalDecision(
        outcome="rejected",
        reason=reason,
        metadata=dict(metadata),
    )


def _clone_decision(
    decision: ApprovalDecision,
    *,
    metadata: dict[str, Any],
) -> ApprovalDecision:
    """复制 :class:`ApprovalDecision` 并替换 ``metadata``。

    ``ApprovalDecision`` 是 frozen dataclass，用 :func:`dataclasses.replace`
    产出新实例，保持不可变性。
    """
    return replace(decision, metadata=dict(metadata))


def _rule_meta(rule: PermissionRule | None) -> dict[str, Any] | None:
    if rule is None:
        return None
    return {
        "tool_name": rule.tool_name,
        "outcome": rule.outcome,
        "arg_key": rule.arg_key,
        "arg_contains": rule.arg_contains,
        "reason": rule.reason,
    }


# ---------------------------------------------------------------------------
# SafetyGatedApproval
# ---------------------------------------------------------------------------


class SafetyGatedApproval:
    """高层安全链。

    实现 :class:`core.contracts.ApprovalProvider` Protocol（duck-typed，
    不显式继承，保持和其他实现一致的轻量风格）。

    内部顺序：

    1. **capability check** —— 命中 ``deny`` → 直接 ``rejected``
    2. **permission check** —— 命中 ``allow`` → 直接 ``approved``；
       命中 ``deny`` → 直接 ``rejected``；``ask`` → 落到第 3 步
    3. **approval** —— 调用底层 :class:`ApprovalProvider` 拿最终决定

    所有返回的 :class:`ApprovalDecision` 都会带 ``metadata["stage"]`` 标识
    决定来源阶段，便于 observability 与调试。
    """

    def __init__(
        self,
        *,
        capability_policy: CapabilityPolicy,
        permission_policy: PermissionPolicy,
        interactive_approval: ApprovalProvider,
    ) -> None:
        self._cap = capability_policy
        self._perm = permission_policy
        self._approval = interactive_approval

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """按 capability → permission → approval 顺序给出最终 ApprovalDecision。"""
        # -- 1. capability check -----------------------------------------
        cap_result = await self._cap.check(request.tool_name, dict(request.arguments))
        if cap_result.outcome == "deny":
            return _rejected(
                reason=f"[capability] {cap_result.reason}",
                metadata={
                    "stage": "capability",
                    "capability": cap_result.capability,
                    "tool_name": request.tool_name,
                },
            )

        # -- 2. permission check -----------------------------------------
        perm_result = await self._perm.check(request.tool_name, dict(request.arguments))
        if perm_result.outcome == "allow":
            return _approved(
                reason=f"[permission] {perm_result.reason}",
                metadata={
                    "stage": "permission",
                    "matched_rule": _rule_meta(perm_result.matched_rule),
                    "tool_name": request.tool_name,
                },
            )
        if perm_result.outcome == "deny":
            return _rejected(
                reason=f"[permission] {perm_result.reason}",
                metadata={
                    "stage": "permission",
                    "matched_rule": _rule_meta(perm_result.matched_rule),
                    "tool_name": request.tool_name,
                },
            )

        # -- 3. ask → 下沉到底层 ApprovalProvider ------------------------
        try:
            decision = await self._approval.decide(request)
        except Exception as exc:  # 防御性：底层 provider 不应抛异常
            raise SafetyChainError(
                "underlying approval provider raised unexpectedly",
                details={
                    "tool_name": request.tool_name,
                    "call_id": request.call_id,
                    "error": repr(exc),
                },
            ) from exc

        merged: dict[str, Any] = dict(decision.metadata or {})
        merged.setdefault("stage", "approval")
        merged.setdefault("ask_source", "permission_policy")
        merged.setdefault("tool_name", request.tool_name)
        return _clone_decision(decision, metadata=merged)


# ---------------------------------------------------------------------------
# 装配糖
# ---------------------------------------------------------------------------


def build_safety_chain(
    config: Config,
    *,
    interactive_approval: ApprovalProvider,
    capability_policy: CapabilityPolicy | None = None,
    permission_policy: PermissionPolicy | None = None,
) -> SafetyGatedApproval:
    """按统一配置 + 底层 approval 装配一条完整的高层安全链。

    Args:
        config: 统一 :class:`Config`。``capability_policy`` / ``permission_policy``
            未显式传入时由 ``from_config`` 按此配置自动装配。
        interactive_approval: 底层 :class:`ApprovalProvider`（通常是
            :class:`tools.approval.InteractiveApproval` 或
            :class:`tools.approval.AutoAllowApproval` 等）。
        capability_policy: 显式注入的 :class:`CapabilityPolicy`；``None`` 走
            :meth:`CapabilityPolicy.from_config`。
        permission_policy: 显式注入的 :class:`PermissionPolicy`；``None`` 走
            :meth:`PermissionPolicy.from_config`。

    Returns:
        :class:`SafetyGatedApproval` 实例，满足
        :class:`core.contracts.ApprovalProvider` Protocol。
    """
    return SafetyGatedApproval(
        capability_policy=capability_policy or CapabilityPolicy.from_config(config),
        permission_policy=permission_policy or PermissionPolicy.from_config(config),
        interactive_approval=interactive_approval,
    )


__all__ = [
    "SafetyChainError",
    "SafetyGatedApproval",
    "build_safety_chain",
]
