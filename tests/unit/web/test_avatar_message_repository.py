"""AvatarMessageRepository 单元测试。

本脚本验证 Avatar 消息 SQLite 真源，作用是固定 register/list/ack、dedupe、
cursor 和 TTL 状态机。关键流程是用 pytest 临时目录创建真实 SQLite 数据库，
执行 Repository 公开方法并断言 DTO round-trip 和状态推进。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hosts.web.avatar import (
    AvatarAckRequest,
    AvatarAckStatus,
    AvatarMessageInput,
    AvatarMessageLevel,
    AvatarMessageListQuery,
    AvatarMessageStatus,
)
from hosts.web.avatar.errors import AvatarMessageError
from hosts.web.avatar.repository import AvatarMessageRepository


def _repo(tmp_path: Path) -> AvatarMessageRepository:
    """创建临时 AvatarMessageRepository。

    关键输入：pytest 临时目录。
    关键输出：使用真实 SQLite 文件的 Repository。
    """
    return AvatarMessageRepository(tmp_path / "avatar_messages.db")


def _message(
    *,
    title: str = "Approval required",
    source: str = "approval",
    dedupe_key: str | None = None,
    expires_at: datetime | None = None,
) -> AvatarMessageInput:
    """构造测试用 AvatarMessageInput。

    关键输入：标题、来源、dedupe key 和过期时间。
    关键输出：覆盖常用字段的消息输入 DTO。
    """
    return AvatarMessageInput(
        source=source,
        title=title,
        body="Tool call is waiting for approval",
        level=AvatarMessageLevel.WARNING,
        priority=80,
        thread_id="thread-avatar-test",
        run_id="run-1",
        request_id="req-1",
        dedupe_key=dedupe_key,
        expires_at=expires_at,
        metadata={"origin": "pytest"},
    )


def test_register_list_ack_message_round_trip(tmp_path: Path) -> None:
    """验证注册、默认拉取、cursor 和 consumed ack 主链路。"""
    repo = _repo(tmp_path)
    now = datetime(2026, 6, 15, 1, 0, tzinfo=UTC)
    first = repo.register_message(_message(title="First"), now=now)
    second = repo.register_message(_message(title="Second", source="thread"), now=now)

    listed = repo.list_messages(AvatarMessageListQuery(), now=now)
    assert [item.message_id for item in listed.items] == [first.message_id, second.message_id]
    assert listed.next_cursor is None
    assert listed.items[0].input.metadata == {"origin": "pytest"}

    after_first = repo.list_messages(
        AvatarMessageListQuery(cursor=str(first.sequence)),
        now=now,
    )
    assert [item.message_id for item in after_first.items] == [second.message_id]

    consumed = repo.ack_message(
        first.message_id,
        AvatarAckRequest(status=AvatarAckStatus.CONSUMED),
        now=now + timedelta(seconds=5),
    )
    assert consumed.status is AvatarMessageStatus.CONSUMED
    assert consumed.acked_at == now + timedelta(seconds=5)
    assert consumed.consumed_at == now + timedelta(seconds=5)

    default_after_ack = repo.list_messages(AvatarMessageListQuery(), now=now)
    assert [item.message_id for item in default_after_ack.items] == [second.message_id]

    consumed_again = repo.ack_message(
        first.message_id,
        AvatarAckRequest(status=AvatarAckStatus.CONSUMED),
        now=now + timedelta(seconds=10),
    )
    assert consumed_again.consumed_at == consumed.consumed_at


def test_dedupe_key_keeps_message_id_and_increments_revision(tmp_path: Path) -> None:
    """验证同 source/dedupeKey 更新保持 messageId 稳定并推进 sequence。"""
    repo = _repo(tmp_path)
    now = datetime(2026, 6, 15, 2, 0, tzinfo=UTC)
    first = repo.register_message(
        _message(title="Pending approval", dedupe_key="approval:req-1"),
        now=now,
    )
    updated = repo.register_message(
        _message(title="Still waiting", dedupe_key="approval:req-1"),
        now=now + timedelta(seconds=1),
    )

    assert updated.message_id == first.message_id
    assert updated.revision == 2
    assert updated.sequence > first.sequence
    assert updated.input.title == "Still waiting"

    listed = repo.list_messages(AvatarMessageListQuery(), now=now + timedelta(seconds=2))
    assert len(listed.items) == 1
    assert listed.items[0].message_id == first.message_id
    assert listed.items[0].revision == 2


def test_expired_messages_are_hidden_by_default_and_can_be_consumed(tmp_path: Path) -> None:
    """验证 TTL 过期消息默认隐藏，并允许客户端补交 consumed ack。"""
    repo = _repo(tmp_path)
    now = datetime(2026, 6, 15, 3, 0, tzinfo=UTC)
    expired = repo.register_message(
        _message(expires_at=now - timedelta(seconds=1)),
        now=now,
    )

    default_list = repo.list_messages(AvatarMessageListQuery(), now=now)
    assert default_list.items == []

    expired_list = repo.list_messages(
        AvatarMessageListQuery(status=[AvatarMessageStatus.EXPIRED]),
        now=now,
    )
    assert [item.message_id for item in expired_list.items] == [expired.message_id]

    consumed = repo.ack_message(
        expired.message_id,
        AvatarAckRequest(status=AvatarAckStatus.CONSUMED),
        now=now + timedelta(seconds=1),
    )
    assert consumed.status is AvatarMessageStatus.CONSUMED
    assert consumed.consumed_at == now + timedelta(seconds=1)


def test_invalid_cursor_and_missing_ack_return_stable_errors(tmp_path: Path) -> None:
    """验证非法 cursor 和缺失 messageId 抛稳定 AvatarMessageError。"""
    repo = _repo(tmp_path)

    with pytest.raises(AvatarMessageError) as cursor_error:
        repo.list_messages(AvatarMessageListQuery(cursor="-1"))
    assert cursor_error.value.code == "avatar_invalid_cursor"

    with pytest.raises(AvatarMessageError) as huge_cursor_error:
        repo.list_messages(AvatarMessageListQuery(cursor=str(2**63)))
    assert huge_cursor_error.value.code == "avatar_invalid_cursor"

    with pytest.raises(AvatarMessageError) as missing_error:
        repo.ack_message("missing", AvatarAckRequest())
    assert missing_error.value.code == "avatar_message_not_found"


def test_large_filter_and_corrupt_payload_return_stable_errors(tmp_path: Path) -> None:
    """验证超大过滤数组和损坏 payload_json 都返回稳定 Avatar 错误。"""
    repo = _repo(tmp_path)
    now = datetime(2026, 6, 15, 6, 0, tzinfo=UTC)
    registered = repo.register_message(_message(), now=now)

    with pytest.raises(AvatarMessageError) as filter_error:
        repo.list_messages(
            AvatarMessageListQuery(source=[f"source-{idx}" for idx in range(51)]),
            now=now,
        )
    assert filter_error.value.code == "avatar_invalid_filter"

    with sqlite3.connect(repo.db_path) as conn:
        conn.execute(
            "UPDATE avatar_messages SET payload_json = ? WHERE message_id = ?",
            ("{", registered.message_id),
        )

    with pytest.raises(AvatarMessageError) as payload_error:
        repo.list_messages(
            AvatarMessageListQuery(status=[AvatarMessageStatus.ACTIVE]),
            now=now,
        )
    assert payload_error.value.code == "avatar_invalid_request"
    assert registered.message_id in payload_error.value.message
    assert "payload_json" in payload_error.value.message


def test_corrupt_payload_ack_does_not_advance_status(tmp_path: Path) -> None:
    """验证损坏 payload 的 ack 失败时不会提交状态推进。"""
    repo = _repo(tmp_path)
    now = datetime(2026, 6, 15, 7, 0, tzinfo=UTC)
    registered = repo.register_message(_message(), now=now)

    with sqlite3.connect(repo.db_path) as conn:
        conn.execute(
            "UPDATE avatar_messages SET payload_json = ? WHERE message_id = ?",
            ("{", registered.message_id),
        )

    with pytest.raises(AvatarMessageError) as payload_error:
        repo.ack_message(registered.message_id, AvatarAckRequest(), now=now)
    assert payload_error.value.code == "avatar_invalid_request"

    with sqlite3.connect(repo.db_path) as conn:
        row = conn.execute(
            "SELECT status, acked_at, consumed_at FROM avatar_messages WHERE message_id = ?",
            (registered.message_id,),
        ).fetchone()
    assert row == ("active", None, None)
