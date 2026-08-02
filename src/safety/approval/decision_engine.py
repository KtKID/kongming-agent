"""三模式审批的统一决策引擎。"""

from __future__ import annotations

import contextlib
from typing import Any, Protocol

from core.contracts import ApprovalDecision, ApprovalProvider, ApprovalRequest
from safety._request_context import SafetyRequestContext
from safety.approval.permissions_errors import PermissionsError
from safety.approval.permissions_manager import PermissionsManager
from safety.approval.rule_models import PermissionResolution, RememberRule, Verdict
from safety.approval.types import ApprovalMetadataKeys
from safety.auto_approval.disposition import (
    ApprovalDispositionMode,
    ApprovalDispositionResolver,
)
from safety.guards.consent import ConsentResolver
from safety.guards.danger import DangerAction, DangerGuard, DangerRule


class TraceEmitter(Protocol):
    """安全决策事件的同步 fan-out 回调。"""

    def __call__(
        self,
        event_kind: str,
        decision: ApprovalDecision,
        request: ApprovalRequest,
    ) -> None:
        """接收一条已结构化的安全决策事件。"""
        ...


class SafetyDecisionEngine:
    """驱动 HardBlock、处置模式和 thread permissions 的唯一入口。"""

    def __init__(
        self,
        *,
        danger_guard: DangerGuard,
        disposition_resolver: ApprovalDispositionResolver,
        permissions_manager: PermissionsManager,
        consent: ConsentResolver,
        trace_emitter: TraceEmitter | None = None,
    ) -> None:
        """注入硬防线、cwd 模式门户、本子门户和人工审批终点。"""
        self._danger_guard = danger_guard
        self._disposition_resolver = disposition_resolver
        self._permissions = permissions_manager
        self._consent = consent
        self._trace_emitter = trace_emitter

    def with_interactive_approval(
        self,
        interactive_approval: ApprovalProvider,
    ) -> SafetyDecisionEngine:
        """复用全部安全 owner，仅重建最终 ConsentResolver。"""
        return SafetyDecisionEngine(
            danger_guard=self._danger_guard,
            disposition_resolver=self._disposition_resolver,
            permissions_manager=self._permissions,
            consent=ConsentResolver(interactive_approval=interactive_approval),
            trace_emitter=self._trace_emitter,
        )

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """按 HardBlock → elevated → force ask → mode → permissions 决策。"""
        guard_rule = self._danger_guard.match(request)
        if guard_rule is not None and guard_rule.action is DangerAction.BLOCK:
            decision = _hard_block_decision(guard_rule)
            self._emit("tool.denied", decision, request)
            return decision
        if guard_rule is not None and guard_rule.action is DangerAction.ELEVATED:
            return await self._request_consent(
                request,
                matched_rule=f"elevated:{guard_rule.name}",
                reason=guard_rule.reason,
                danger=False,
                severity="elevated",
                remember_rule=None,
                remember_thread_id=None,
                remember_revision=None,
            )

        if guard_rule is not None:
            return await self._request_consent(
                request,
                matched_rule=f"danger:{guard_rule.name}",
                reason=guard_rule.reason,
                danger=True,
                severity="danger",
                remember_rule=None,
                remember_thread_id=None,
                remember_revision=None,
            )

        mode = self._mode_for(request)
        if mode is ApprovalDispositionMode.FULL_TRUST:
            decision = _full_trust_decision("mode:full_trust")
            self._emit("approval.full_trust.auto_allow", decision, request)
            self._emit("tool.silently_allowed", decision, request)
            return decision

        thread_id = resolve_thread_key(request)
        try:
            snapshot = await self._permissions.snapshot(thread_id)
            resolution = await self._permissions.resolve(thread_id, request)
        except PermissionsError as exc:
            return await self._request_consent(
                request,
                matched_rule="permissions:error",
                reason=f"thread permissions unavailable: {exc}",
                danger=False,
                severity="standard",
                remember_rule=None,
                remember_thread_id=None,
                remember_revision=None,
            )

        if resolution is not None:
            decision = _permissions_decision(resolution)
            event_kind = (
                "tool.silently_allowed" if resolution.verdict is Verdict.ALLOW else "tool.denied"
            )
            self._emit(event_kind, decision, request)
            return decision

        remember_rule = self._permissions.build_remember_expression(request)
        return await self._request_consent(
            request,
            matched_rule="default:ask",
            reason="no thread permission matched",
            danger=False,
            severity="standard",
            remember_rule=remember_rule,
            remember_thread_id=thread_id if remember_rule is not None else None,
            remember_revision=snapshot.revision if remember_rule is not None else None,
        )

    def _mode_for(self, request: ApprovalRequest) -> ApprovalDispositionMode:
        """按 cwd 查询模式；配置读取异常以 user 失败关闭。"""
        cwd = SafetyRequestContext.from_request(request).cwd or ""
        try:
            return self._disposition_resolver.mode_for(cwd)
        except Exception:
            return ApprovalDispositionMode.USER

    async def _request_consent(
        self,
        request: ApprovalRequest,
        *,
        matched_rule: str,
        reason: str,
        danger: bool,
        severity: str,
        remember_rule: RememberRule | None,
        remember_thread_id: str | None,
        remember_revision: int | None,
    ) -> ApprovalDecision:
        """构造展示预览、委托人工终点并投射最终事件。"""
        remember_allowed = (
            not danger
            and severity == "standard"
            and remember_rule is not None
            and remember_thread_id is not None
            and remember_revision is not None
        )
        preview_metadata: dict[str, Any] = {
            ApprovalMetadataKeys.DECISION_CLASS: "explicit_consent",
            ApprovalMetadataKeys.DECISION_SOURCE: "danger" if danger else severity,
            ApprovalMetadataKeys.MATCHED_RULE: matched_rule,
            ApprovalMetadataKeys.REASON: reason,
            ApprovalMetadataKeys.BOUNDARY_KIND: "host",
            ApprovalMetadataKeys.DANGER: danger,
            ApprovalMetadataKeys.REMEMBER_ALLOWED: remember_allowed,
            "severity": severity,
            "placeholder": True,
        }
        if remember_allowed:
            assert remember_rule is not None
            assert remember_thread_id is not None
            assert remember_revision is not None
            preview_metadata.update(
                {
                    ApprovalMetadataKeys.REMEMBER_RULE: {
                        "expression": remember_rule.expression,
                        "displayText": remember_rule.display_text,
                        "scopeCwd": remember_rule.scope_cwd,
                    },
                    ApprovalMetadataKeys.REMEMBER_THREAD_ID: remember_thread_id,
                    ApprovalMetadataKeys.REMEMBER_REVISION: remember_revision,
                }
            )
        self._emit(
            "tool.approval_required",
            ApprovalDecision(outcome="pending", reason=reason, metadata=preview_metadata),
            request,
        )
        decision = await self._consent.evaluate(
            request,
            danger=danger,
            severity=severity,
            matched_rule=matched_rule,
            reason=reason,
            remember_rule=remember_rule if remember_allowed else None,
            remember_thread_id=remember_thread_id if remember_allowed else None,
            remember_revision=remember_revision if remember_allowed else None,
        )
        self._emit(_final_event_kind(decision), decision, request)
        return decision

    def _emit(
        self,
        event_kind: str,
        decision: ApprovalDecision,
        request: ApprovalRequest,
    ) -> None:
        """隔离事件出口故障，保护主决策控制流。"""
        if self._trace_emitter is None:
            return
        with contextlib.suppress(Exception):
            self._trace_emitter(event_kind, decision, request)


def resolve_thread_key(request: ApprovalRequest) -> str:
    """优先返回 root thread_id，CLI 等宿主回落稳定 session_id。"""
    raw_thread_id = request.metadata.get("thread_id")
    if isinstance(raw_thread_id, str) and raw_thread_id.strip():
        return raw_thread_id
    if request.session_id.strip():
        return request.session_id
    raise ValueError("approval request requires a stable thread_id or session_id")


def _hard_block_decision(rule: DangerRule) -> ApprovalDecision:
    """构造任何模式均不可绕过的硬拒绝。"""
    return ApprovalDecision(
        outcome="rejected",
        reason=f"[hard_block:{rule.name}] {rule.reason}",
        metadata={
            ApprovalMetadataKeys.DECISION_CLASS: "hard_block",
            ApprovalMetadataKeys.DECISION_SOURCE: "intrinsic",
            ApprovalMetadataKeys.MATCHED_RULE: rule.name,
            ApprovalMetadataKeys.REASON: rule.reason,
            ApprovalMetadataKeys.BOUNDARY_KIND: "host",
            ApprovalMetadataKeys.SUGGESTED_ALTERNATIVES: list(rule.suggested_alternatives),
            "audit_priority": "critical",
        },
    )


def _full_trust_decision(matched_rule: str) -> ApprovalDecision:
    """构造 full_trust 的高优先级审计放行。"""
    return ApprovalDecision(
        outcome="approved",
        reason="full_trust mode allows request without DangerGuard match",
        metadata={
            ApprovalMetadataKeys.DECISION_CLASS: "silent_allow",
            ApprovalMetadataKeys.DECISION_SOURCE: "full_trust",
            ApprovalMetadataKeys.MATCHED_RULE: matched_rule,
            ApprovalMetadataKeys.REASON: "full_trust mode allows request without DangerGuard match",
            ApprovalMetadataKeys.BOUNDARY_KIND: "host",
            ApprovalMetadataKeys.DANGER: False,
            "audit_priority": "high",
        },
    )


def _permissions_decision(resolution: PermissionResolution) -> ApprovalDecision:
    """把 permissions allow/deny 命中转换为最终决定。"""
    if resolution.verdict is Verdict.ALLOW:
        return ApprovalDecision(
            outcome="approved",
            reason="thread permission allow matched",
            metadata={
                ApprovalMetadataKeys.DECISION_CLASS: "silent_allow",
                ApprovalMetadataKeys.DECISION_SOURCE: "permissions",
                ApprovalMetadataKeys.MATCHED_RULE: resolution.expression,
                ApprovalMetadataKeys.REASON: "thread permission allow matched",
                ApprovalMetadataKeys.BOUNDARY_KIND: "host",
                ApprovalMetadataKeys.DANGER: False,
                "matched_rule_scope_cwd": resolution.scope_cwd,
            },
        )
    return ApprovalDecision(
        outcome="rejected",
        reason="thread permission deny matched",
        metadata={
            ApprovalMetadataKeys.DECISION_CLASS: "explicit_consent",
            ApprovalMetadataKeys.DECISION_SOURCE: "permissions",
            ApprovalMetadataKeys.MATCHED_RULE: resolution.expression,
            ApprovalMetadataKeys.REASON: "thread permission deny matched",
            ApprovalMetadataKeys.BOUNDARY_KIND: "host",
            ApprovalMetadataKeys.DANGER: False,
            "matched_rule_scope_cwd": resolution.scope_cwd,
        },
    )


def _final_event_kind(decision: ApprovalDecision) -> str:
    """把用户最终 outcome 映射到安全事件类型。"""
    return "tool.silently_allowed" if decision.outcome == "approved" else "tool.denied"


__all__ = ["SafetyDecisionEngine", "TraceEmitter", "resolve_thread_key"]
