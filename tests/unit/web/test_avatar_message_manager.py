"""AvatarManager 单元测试。

本脚本验证 AvatarManager 门户，作用是固定跨模块入口对 Repository 和
AssistantManager 的编排语义。关键流程是用真实临时 SQLite Repository 构造
Manager，覆盖注册、拉取、批量 ack 和 v1 chat disabled capability。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from hosts.web.avatar import (
    AvatarAckBatchRequest,
    AvatarAckRequest,
    AvatarChatRequest,
    AvatarManager,
    AvatarMessageInput,
    AvatarMessageListQuery,
    AvatarMessageStatus,
)
from hosts.web.avatar.errors import AvatarMessageError
from hosts.web.avatar.repository import AvatarMessageRepository


def _manager(tmp_path: Path) -> AvatarManager:
    """创建使用临时 SQLite 的 AvatarManager。

    关键输入：pytest 临时目录。
    关键输出：可执行真实 repository 路径的 Manager。
    """
    return AvatarManager(AvatarMessageRepository(tmp_path / "avatar.db"))


def _input(title: str = "Task update") -> AvatarMessageInput:
    """构造测试消息输入。

    关键输入：消息标题。
    关键输出：AvatarMessageInput。
    """
    return AvatarMessageInput(
        source="task",
        title=title,
        body="Task state changed",
        thread_id="thread-manager-test",
        metadata={"stage": "manager"},
    )


def test_manager_register_list_ack_round_trip(tmp_path: Path) -> None:
    """验证 Manager 主链路会通过门户完成注册、拉取和 ack。"""
    manager = _manager(tmp_path)
    now = datetime(2026, 6, 15, 4, 0, tzinfo=UTC)

    registered = manager.register_message(_input(), now=now)
    listed = manager.list_messages(AvatarMessageListQuery(), now=now)
    assert [item.message_id for item in listed.items] == [registered.message_id]

    acked = manager.ack_message(registered.message_id, AvatarAckRequest(), now=now)
    assert acked.status is AvatarMessageStatus.CONSUMED


def test_batch_ack_reports_missing_message_without_blocking(tmp_path: Path) -> None:
    """验证批量 ack 对单条缺失返回稳定错误并继续处理其它消息。"""
    manager = _manager(tmp_path)
    now = datetime(2026, 6, 15, 5, 0, tzinfo=UTC)
    registered = manager.register_message(_input(), now=now)

    result = manager.ack_messages(
        AvatarAckBatchRequest(message_ids=[registered.message_id, "missing"]),
        now=now,
    )

    assert [item.message_id for item in result.results] == [registered.message_id, "missing"]
    assert result.results[0].ok is True
    assert result.results[0].message is not None
    assert result.results[0].message.status is AvatarMessageStatus.CONSUMED
    assert result.results[1].ok is False
    assert result.results[1].error == "avatar_message_not_found"


def test_avatar_chat_capability_is_disabled_in_v1(tmp_path: Path) -> None:
    """验证 v1 capabilities 和 chat disabled 语义稳定。"""
    manager = _manager(tmp_path)

    capabilities = manager.capabilities()
    assert capabilities.message_registry is True
    assert capabilities.avatar_chat is False
    assert capabilities.required_scopes["chat"] == ["avatar.chat"]

    with pytest.raises(AvatarMessageError) as exc:
        manager.chat(AvatarChatRequest(text="hello from avatar"))
    assert exc.value.code == "avatar_capability_disabled"
