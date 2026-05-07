"""safety — SafetyDecisionEngine（M7）。

主决策链装配：把五件套串成统一决策入口。

链路顺序（四级从严到松，不可跳级）：

1. **HardBlockGuard**：命中 secrets / destructive / self-escalation → ``rejected``
2. **BoundaryResolver**：把请求归类到 trusted / approval / blocked zone
3. **DestructiveForceAskGuard**：rm -r* / shred / srm → 无视 grant，强制 consent
4. **TrustResolver**：消费 intrinsic / session / config 证据 → silent_allow
5. **ConsentResolver**：standard / elevated 审批分流 → 委托 InteractiveApproval

设计要点：

- **trace 通过 callback 注入**：``trace_emitter`` 是 ``Callable[[event_kind, decision,
  request], None] | None``，由装配层（``chain.build_safety_chain``）注入，
  不直接 import ``observability`` / ``EventSink``，保持 ``safety`` 自包含。
- **运行时 boundary_kind 恒为 host**：``RuntimeBoundaryContext`` 由
  :class:`SafetyGatedApproval` 在调用入口构造，本类不读 ``boundary_kind`` 做
  sandbox 分支。
- **ConsentResolver 是终点 guard**：永远返回 ``ApprovalDecision``，本类不再做
  fallback。
- **DestructiveForceAskGuard 不产出 decision**：只做 bool 判断，命中后由
  SafetyDecisionEngine 直接跳到 ConsentResolver。
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Protocol

from core.contracts import ApprovalDecision
from safety.types import ApprovalMetadataKeys

if TYPE_CHECKING:
    from core.contracts import ApprovalRequest
    from safety.boundary_resolver import BoundaryResolver
    from safety.guards.consent import ConsentResolver
    from safety.guards.destructive import DestructiveForceAskGuard
    from safety.guards.hard_block import HardBlockGuard
    from safety.guards.trust import TrustResolver
    from safety.types import RuntimeBoundaryContext


class TraceEmitter(Protocol):
    """:class:`SafetyDecisionEngine` 的 trace 回调形态。

    ``event_kind`` ∈ ``{"tool.denied", "tool.silently_allowed",
    "tool.approval_required"}``（见 :class:`core.contracts.EventKind`）。

    ``trace_emitter`` 是同步回调；装配层（:func:`safety.chain.build_safety_chain`）
    内部把它包装成 EventSink fan-out 的同步代理，避免在主决策路径上引入异步 await。
    """

    def __call__(
        self,
        event_kind: str,
        decision: ApprovalDecision,
        request: ApprovalRequest,
    ) -> None: ...


class SafetyDecisionEngine:
    """主决策链装配中枢。

    构造时显式注入五个 guard，便于测试 stub；生产装配走
    :func:`safety.chain.build_safety_chain`。

    Attributes:
        _hard_block: 第一道防线，命中即拒绝。
        _boundary: 第二步，归类 zone。
        _destructive: 第三步，destructive 命令强制 consent。
        _trust: 第四步，消费已有证据 silent_allow。
        _consent: 第五步，发起人决策。
        _trace_emitter: 可选，每步决策后调用一次。
    """

    def __init__(
        self,
        *,
        hard_block: HardBlockGuard,
        boundary: BoundaryResolver,
        destructive: DestructiveForceAskGuard | None = None,
        trust: TrustResolver,
        consent: ConsentResolver,
        trace_emitter: TraceEmitter | None = None,
    ) -> None:
        self._hard_block = hard_block
        self._boundary = boundary
        self._destructive = destructive
        self._trust = trust
        self._consent = consent
        self._trace_emitter = trace_emitter

    async def decide(
        self,
        request: ApprovalRequest,
        runtime: RuntimeBoundaryContext,
    ) -> ApprovalDecision:
        """串行驱动主决策链（四级从严到松）。

        1. HardBlock 命中 → emit ``tool.denied``，返回。
        2. BoundaryResolver 计算 zone。
        3. DestructiveForceAsk 命中 → emit ``tool.destructive_force_ask``，
           跳过 Trust 直接进 Consent。
        4. TrustResolver 命中 → emit ``tool.silently_allowed``，返回。
        5. 否则委托 ConsentResolver。
        """
        # 1. HardBlock 优先
        hard_decision = self._hard_block.evaluate(request, runtime)
        if hard_decision is not None:
            self._emit("tool.denied", hard_decision, request)
            return hard_decision

        # 2. Boundary 归类（zone 给 TrustResolver / ConsentResolver 用）
        boundary_decision = self._boundary.resolve(request, runtime)

        # 3. DestructiveForceAsk：命中 → 跳过 Trust，强制进 Consent
        if self._destructive is not None and self._destructive.matches(request):
            matched = self._destructive.matched_rule(request)
            force_preview = ApprovalDecision(
                outcome="approved",  # 占位
                reason="destructive command: forced consent",
                metadata={
                    ApprovalMetadataKeys.DECISION_CLASS: "explicit_consent",
                    ApprovalMetadataKeys.BOUNDARY_KIND: "host",
                    ApprovalMetadataKeys.MATCHED_RULE: (
                        f"destructive:{matched.name}" if matched else "destructive:unknown"
                    ),
                },
            )
            self._emit("tool.destructive_force_ask", force_preview, request)

            consent_decision = await self._consent.evaluate(request, boundary_decision, runtime)
            if consent_decision.outcome == "approved":
                self._emit("tool.silently_allowed", consent_decision, request)
            else:
                self._emit("tool.denied", consent_decision, request)
            return consent_decision

        # 4. Trust 消费已有证据
        trust_decision = self._trust.evaluate(request, runtime)
        if trust_decision is not None:
            self._emit("tool.silently_allowed", trust_decision, request)
            return trust_decision

        # 5. Consent：先 emit approval_required，再按结果 emit
        approval_required_preview = ApprovalDecision(
            outcome="approved",  # 占位；emit 后立刻被真实 decision 替换
            reason="awaiting user consent",
            metadata={
                ApprovalMetadataKeys.DECISION_CLASS: "explicit_consent",
                ApprovalMetadataKeys.BOUNDARY_KIND: "host",
            },
        )
        self._emit("tool.approval_required", approval_required_preview, request)

        consent_decision = await self._consent.evaluate(request, boundary_decision, runtime)

        # outcome → 事件类型
        if consent_decision.outcome == "approved":
            self._emit("tool.silently_allowed", consent_decision, request)
        else:
            # rejected / cancelled 都视为 denied
            self._emit("tool.denied", consent_decision, request)

        return consent_decision

    def _emit(
        self,
        event_kind: str,
        decision: ApprovalDecision,
        request: ApprovalRequest,
    ) -> None:
        """安全地调用 trace emitter；emitter 异常被吞掉以保护主决策路径。"""
        if self._trace_emitter is None:
            return
        # trace 失败不影响主链路。装配层负责把 emitter 内部异常上报。
        with contextlib.suppress(Exception):
            self._trace_emitter(event_kind, decision, request)


__all__ = [
    "SafetyDecisionEngine",
    "TraceEmitter",
]
