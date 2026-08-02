"""CLIApprovalEventSink 的人工审批投影与失败关闭测试。

关键流程：真实 ApprovalManager 创建 pending，CLI sink 投影 ApprovalRequest，
终端动作再经 manager.resolve 收口，覆盖允许、拒绝和通道隔离。
"""

from __future__ import annotations

from pathlib import Path

from core.contracts import ApprovalAction, ApprovalRequest
from hosts.cli.approval_manager_sink import CLIApprovalEventSink
from safety.approval.events import PendingApprovalView
from safety.approval.manager import ApprovalManager
from safety.approval.permissions_manager import PermissionsManager


def _manager(tmp_path: Path) -> ApprovalManager:
    """构造使用临时 thread permissions 本子的审批门户。"""
    return ApprovalManager(permissions_manager=PermissionsManager(tmp_path))


async def test_cli_sink_accept_once_resolves_allow_payload(tmp_path: Path) -> None:
    """终端单次允许会完成真实 pending，并保留 root thread 身份。"""
    manager = _manager(tmp_path)
    captured: list[ApprovalRequest] = []

    async def prompt(request: ApprovalRequest) -> ApprovalAction:
        """记录投影请求并返回单次允许。"""
        captured.append(request)
        return ApprovalAction.ACCEPT_ONCE

    manager.register_event_sink(CLIApprovalEventSink(manager, prompt))
    decision = await manager.request(
        channel="cli",
        thread_id="cli-session",
        cwd="/proj",
        tool_name="run_shell",
        tool_input={"command": "ls"},
        metadata={
            "run_id": "run-1",
            "session_id": "cli-session",
            "turn": 1,
            "call_id": "call-1",
        },
    )

    assert decision.outcome == "approved"
    assert captured[0].session_id == "cli-session"
    assert captured[0].metadata["approval_channel"] == "cli"
    assert captured[0].metadata["danger"] is False
    assert captured[0].metadata["remember_allowed"] is False


async def test_cli_sink_reject_action_rejects_pending(tmp_path: Path) -> None:
    """终端拒绝动作会按失败关闭语义完成 pending。"""
    manager = _manager(tmp_path)

    async def prompt(_request: ApprovalRequest) -> ApprovalAction:
        """返回显式拒绝。"""
        return ApprovalAction.REJECT

    manager.register_event_sink(CLIApprovalEventSink(manager, prompt))
    decision = await manager.request(
        channel="cli",
        thread_id="cli-session",
        cwd="/proj",
        tool_name="run_shell",
        tool_input={"command": "ls"},
    )
    assert decision.outcome == "rejected"


async def test_cli_sink_prompt_exception_rejects(tmp_path: Path) -> None:
    """终端输入异常时自动拒绝并清理 pending。"""
    manager = _manager(tmp_path)

    async def prompt(_request: ApprovalRequest) -> ApprovalAction:
        """模拟终端输入通道失败。"""
        raise RuntimeError("标准输入失败")

    manager.register_event_sink(CLIApprovalEventSink(manager, prompt))
    decision = await manager.request(
        channel="cli",
        thread_id="cli-session",
        cwd="/proj",
        tool_name="run_shell",
        tool_input={"command": "ls"},
    )
    assert decision.outcome == "rejected"
    assert manager.pending_count == 0


async def test_cli_sink_ignores_other_channels(tmp_path: Path) -> None:
    """CLI sink 对其他宿主的公开 pending 快照保持无副作用。"""
    manager = _manager(tmp_path)
    called = False

    async def prompt(_request: ApprovalRequest) -> ApprovalAction:
        """标记意外的 CLI prompt 调用。"""
        nonlocal called
        called = True
        return ApprovalAction.ACCEPT_ONCE

    sink = CLIApprovalEventSink(manager, prompt)
    await sink.emit_approval_required(
        pending=PendingApprovalView(
            request_id="req-1",
            channel="generic_chat",
            thread_id="thread-a",
        )
    )
    assert called is False
