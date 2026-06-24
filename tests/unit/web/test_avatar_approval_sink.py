"""AvatarApprovalSink 单元测试。

本脚本验证审批 pending view 能注册到 Avatar message registry。关键流程是构造
PendingApprovalView fixture，调用 AvatarApprovalSink.emit_approval_required，再用
AvatarManager.list_messages 查询真实 SQLite repository。关键函数职责：固定
source/action/metadata 映射和 Web 装配幂等注册。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from hosts.web.avatar import AvatarManager, AvatarMessageListQuery, AvatarMessageStatus
from hosts.web.avatar.approval_sink import AvatarApprovalSink
from hosts.web.avatar.repository import AvatarMessageRepository
from hosts.web.run import _build_manager_and_inbox_sink
from safety.approval import PendingApprovalView
from safety.approval.manager import reset_for_testing
from safety.inbox.event_sink import InboxEventSink


def _make_avatar_manager(tmp_path: Path) -> AvatarManager:
    """创建真实 SQLite backed AvatarManager。"""
    return AvatarManager(AvatarMessageRepository(tmp_path / "avatar.db"))


def _make_pending(
    *,
    request_id: str = "req-avatar-1",
    severity: str = "standard",
) -> PendingApprovalView:
    """构造 AvatarApprovalSink 测试用 pending 审批。"""
    return PendingApprovalView(
        request_id=request_id,
        channel="generic_chat",
        thread_id="thread-avatar",
        cwd="/workspace",
        tool_name="Bash",
        tool_input={"command": "rm -rf /tmp/demo"},
        metadata={"run_id": "run-1"},
        severity=severity,
        matched_rule="dangerous-command" if severity == "elevated" else None,
        auto_approve_at_ms=None,
        auto_reject_at_ms=12345 if severity == "elevated" else None,
        arrived_at_ms=1000,
        timeout_ms=60_000,
    )


async def test_emit_approval_required_registers_avatar_message(tmp_path: Path) -> None:
    """验证 pending 审批会写入 source=approval 的 Avatar 消息。"""
    avatar_manager = _make_avatar_manager(tmp_path)
    sink = AvatarApprovalSink(avatar_manager)

    await sink.emit_approval_required(pending=_make_pending())

    result = avatar_manager.list_messages(
        AvatarMessageListQuery(source=["approval"], status=[AvatarMessageStatus.ACTIVE])
    )
    assert len(result.items) == 1
    message = result.items[0]
    assert message.input.source == "approval"
    assert message.input.title == "Bash 等待审批"
    assert message.input.level.value == "warning"
    assert message.input.priority == 90
    assert message.input.thread_id == "thread-avatar"
    assert message.input.request_id == "req-avatar-1"
    assert message.input.dedupe_key == "approval:req-avatar-1"
    assert message.input.action is not None
    assert message.input.action.type == "open_approval"
    assert message.input.action.target == "req-avatar-1"
    assert message.input.action.payload == {
        "threadId": "thread-avatar",
        "requestId": "req-avatar-1",
        "channel": "generic_chat",
    }
    assert message.input.metadata["toolName"] == "Bash"
    assert message.input.metadata["isElevated"] is False
    assert "rm -rf" in message.input.metadata["toolInputPreview"]


async def test_elevated_approval_registers_error_priority_message(tmp_path: Path) -> None:
    """验证 elevated 审批映射为更高等级 Avatar 消息。"""
    avatar_manager = _make_avatar_manager(tmp_path)
    sink = AvatarApprovalSink(avatar_manager)

    await sink.emit_approval_required(pending=_make_pending(severity="elevated"))

    message = avatar_manager.list_messages(AvatarMessageListQuery(source=["approval"])).items[0]
    assert message.input.level.value == "error"
    assert message.input.priority == 95
    assert message.input.metadata["isElevated"] is True
    assert message.input.metadata["matchedRule"] == "dangerous-command"
    assert message.input.metadata["autoRejectAtMs"] == 12345


async def test_register_sink_is_idempotent_in_web_approval_manager(tmp_path: Path) -> None:
    """验证 Web 装配会幂等注册 inbox sink 和 Avatar sink。"""
    reset_for_testing()
    app = SimpleNamespace(
        state=SimpleNamespace(
            auto_approval_policy=None,
            avatar_manager=_make_avatar_manager(tmp_path),
        )
    )
    try:
        manager = _build_manager_and_inbox_sink(app=app)
        same_manager = _build_manager_and_inbox_sink(app=app)
        assert same_manager is manager
        assert sum(isinstance(s, InboxEventSink) for s in manager._event_sinks) == 1
        assert sum(isinstance(s, AvatarApprovalSink) for s in manager._event_sinks) == 1
    finally:
        reset_for_testing()
