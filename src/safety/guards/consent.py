"""v0.6 用户审批终点。

本模块只负责把 DangerGuard 或 permissions 未命中的请求提交给底层
ApprovalProvider，并冻结 danger、remember candidate、thread_id 与 revision 元数据。
风险分类、目录边界、grant 与自动倒计时均不属于本层。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from core.contracts import ApprovalDecision, ApprovalProvider, ApprovalRequest
from safety.approval.rule_models import RememberRule
from safety.approval.types import ApprovalMetadataKeys


class ConsentResolver:
    """把需要人决策的请求交给宿主审批实现。"""

    def __init__(self, *, interactive_approval: ApprovalProvider) -> None:
        """绑定真正承担 UI 或 CLI 交互的审批 Provider。"""
        self._interactive_approval = interactive_approval

    async def evaluate(
        self,
        request: ApprovalRequest,
        *,
        danger: bool,
        severity: str = "standard",
        matched_rule: str,
        reason: str,
        remember_rule: RememberRule | None = None,
        remember_thread_id: str | None = None,
        remember_revision: int | None = None,
    ) -> ApprovalDecision:
        """冻结审批上下文，等待一次显式用户决策并装饰标准元数据。"""
        if danger and (
            remember_rule is not None
            or remember_thread_id is not None
            or remember_revision is not None
        ):
            raise ValueError("danger approval must not carry remember context")

        remember_allowed = (
            severity == "standard"
            and not danger
            and remember_rule is not None
            and remember_thread_id is not None
            and remember_revision is not None
        )
        enriched_metadata: dict[str, Any] = dict(request.metadata)
        enriched_metadata.update(
            {
                ApprovalMetadataKeys.DECISION_CLASS: "explicit_consent",
                ApprovalMetadataKeys.DECISION_SOURCE: (
                    "danger"
                    if danger
                    else "elevated"
                    if severity == "elevated"
                    else "user_approval"
                ),
                ApprovalMetadataKeys.MATCHED_RULE: matched_rule,
                ApprovalMetadataKeys.REASON: reason,
                ApprovalMetadataKeys.BOUNDARY_KIND: "host",
                ApprovalMetadataKeys.DANGER: danger,
                ApprovalMetadataKeys.REMEMBER_ALLOWED: remember_allowed,
                "policy_hint": severity,
                "severity": severity,
            }
        )
        if remember_allowed:
            assert remember_rule is not None
            assert remember_thread_id is not None
            assert remember_revision is not None
            enriched_metadata.update(
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

        downstream = await self._interactive_approval.decide(
            replace(request, metadata=enriched_metadata)
        )
        metadata = dict(downstream.metadata)
        metadata.update(enriched_metadata)
        return replace(
            downstream,
            reason=downstream.reason or reason,
            metadata=metadata,
        )


__all__ = ["ConsentResolver"]
