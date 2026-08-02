"""Claude 通道共享规则审计坐标回归测试。"""

from __future__ import annotations

from dataclasses import dataclass

from claude_agent_sdk.types import PermissionResultAllow, ToolPermissionContext

from core.contracts import ApprovalDecision, ApprovalRequest
from hosts.web.integrations.claude_code.approval import ApprovalBridge
from hosts.web.integrations.claude_code.normalizer import ClaudeNormalizer
from hosts.web.shared.session_manager import SessionManager


def _ctx() -> ToolPermissionContext:
    return ToolPermissionContext(
        signal=None,
        suggestions=[],
        tool_use_id="toolu-a",
        agent_id=None,
    )


@dataclass
class _AuditedApproval:
    request: ApprovalRequest | None = None

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        self.request = request
        return ApprovalDecision(
            outcome="approved",
            reason="config allow rule matched",
            metadata={
                "matched_rule": "config:ls",
                "source": "config",
                "matcher": "run_shell(ls:*)",
            },
        )


async def test_bridge_preserves_shared_provider_as_decision_owner() -> None:
    provider = _AuditedApproval()
    bridge = ApprovalBridge(
        ClaudeNormalizer(),
        SessionManager(),
        approval=provider,
        cwd="/project",
        thread_id="thread-a",
    )
    result = await bridge.can_use_tool("Bash", {"command": "ls"}, _ctx())
    assert isinstance(result, PermissionResultAllow)
    assert provider.request is not None
    assert provider.request.tool_name == "run_shell"
    assert provider.request.metadata == {
        "channel": "claude_code",
        "cwd": "/project",
        "thread_id": "thread-a",
        "sdk_tool_name": "Bash",
    }


async def test_writer_lifecycle_has_no_decision_effect() -> None:
    provider = _AuditedApproval()
    bridge = ApprovalBridge(
        ClaudeNormalizer(),
        SessionManager(),
        approval=provider,
    )
    bridge.set_active_writer(object())
    bridge.clear_active_writer()
    result = await bridge.can_use_tool("Bash", {"command": "ls"}, _ctx())
    assert isinstance(result, PermissionResultAllow)
