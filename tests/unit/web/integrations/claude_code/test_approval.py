"""Claude ApprovalBridge 统一审批适配测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from core.contracts import ApprovalDecision, ApprovalRequest
from hosts.web.integrations.claude_code.approval import ApprovalBridge
from hosts.web.integrations.claude_code.normalizer import ClaudeNormalizer
from hosts.web.shared.session_manager import SessionManager


def _ctx(tool_use_id: str | None = "toolu_test") -> ToolPermissionContext:
    return ToolPermissionContext(
        signal=None,
        suggestions=[],
        tool_use_id=tool_use_id,
        agent_id=None,
    )


@dataclass
class _RecordingApproval:
    decision: ApprovalDecision
    requests: list[ApprovalRequest] = field(default_factory=list)

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return self.decision


def _bridge(
    provider: _RecordingApproval,
    *,
    normalizer: ClaudeNormalizer | None = None,
) -> ApprovalBridge:
    return ApprovalBridge(
        normalizer or ClaudeNormalizer(),
        SessionManager(),
        approval=provider,
        cwd="/workspace/a",
        thread_id="thread-a",
    )


async def test_can_use_tool_routes_canonical_request_to_shared_provider() -> None:
    provider = _RecordingApproval(ApprovalDecision(outcome="approved", reason="rule allow"))
    bridge = _bridge(provider)
    result = await bridge.can_use_tool("Bash", {"command": "ls -la"}, _ctx())
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == {"command": "ls -la"}
    request = provider.requests[0]
    assert request.tool_name == "run_shell"
    assert request.arguments == {"command": "ls -la"}
    assert request.metadata["channel"] == "claude_code"
    assert request.metadata["thread_id"] == "thread-a"
    assert request.metadata["cwd"] == "/workspace/a"


async def test_active_sdk_session_keeps_root_thread_for_permissions() -> None:
    """Claude SDK session 只标识执行实例，审批本子沿用 Web root thread。"""
    provider = _RecordingApproval(ApprovalDecision(outcome="approved"))
    sessions = SessionManager()
    writer = object()
    await sessions.register("claude-sdk-session", writer)
    bridge = ApprovalBridge(
        ClaudeNormalizer(),
        sessions,
        approval=provider,
        cwd="/workspace/a",
        thread_id="thread-a",
    )
    bridge.set_active_writer(writer)

    result = await bridge.can_use_tool("Read", {"file_path": "README.md"}, _ctx())

    assert isinstance(result, PermissionResultAllow)
    request = provider.requests[0]
    assert request.session_id == "claude-sdk-session"
    assert request.metadata["thread_id"] == "thread-a"


async def test_deny_translates_reason_and_marks_normalizer() -> None:
    normalizer = ClaudeNormalizer()
    provider = _RecordingApproval(ApprovalDecision(outcome="rejected", reason="hard block"))
    bridge = _bridge(provider, normalizer=normalizer)
    result = await bridge.can_use_tool("Write", {"file_path": "/tmp/a"}, _ctx("toolu-x"))
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "hard block"
    assert "toolu-x" in normalizer._pending_deny
    assert provider.requests[0].tool_name == "write_file"
    assert provider.requests[0].arguments == {"path": "/tmp/a"}


async def test_missing_tool_use_id_fails_closed_before_provider() -> None:
    provider = _RecordingApproval(ApprovalDecision(outcome="approved"))
    result = await _bridge(provider).can_use_tool("Read", {}, _ctx(None))
    assert isinstance(result, PermissionResultDeny)
    assert provider.requests == []


async def test_active_cwd_update_reaches_next_request() -> None:
    provider = _RecordingApproval(ApprovalDecision(outcome="approved"))
    bridge = _bridge(provider)
    bridge.set_active_cwd("/workspace/b")
    await bridge.can_use_tool("Read", {"file_path": "README.md"}, _ctx())
    assert provider.requests[0].metadata["cwd"] == "/workspace/b"


async def test_provider_cancellation_propagates() -> None:
    started = asyncio.Event()

    class _BlockingApproval:
        async def decide(self, _request: ApprovalRequest) -> ApprovalDecision:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    bridge = ApprovalBridge(
        ClaudeNormalizer(),
        SessionManager(),
        approval=_BlockingApproval(),
        cwd="/workspace/a",
        thread_id="thread-a",
    )
    task = asyncio.create_task(bridge.can_use_tool("Read", {}, _ctx()))
    await started.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("CancelledError must propagate")
