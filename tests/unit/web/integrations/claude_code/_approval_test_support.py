"""Claude service 测试使用的统一审批适配器装配。"""

from __future__ import annotations

from core.contracts import ApprovalDecision, ApprovalRequest
from hosts.web.integrations.claude_code.approval import ApprovalBridge
from hosts.web.integrations.claude_code.normalizer import ClaudeNormalizer
from hosts.web.shared.session_manager import SessionManager


class _RejectingApproval:
    """service 测试不会触发工具审批；意外触发时保持失败关闭。"""

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(
            outcome="rejected",
            reason=f"unexpected approval in service test: {request.tool_name}",
        )


def build_test_approval_bridge(
    normalizer: ClaudeNormalizer,
    sessions: SessionManager,
) -> ApprovalBridge:
    """为非审批 service 测试显式注入共享审批 Provider。"""
    return ApprovalBridge(
        normalizer,
        sessions,
        approval=_RejectingApproval(),
    )


__all__ = ["build_test_approval_bridge"]
