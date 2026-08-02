"""AvatarManager 单元测试。

本脚本验证 AvatarManager 门户，作用是固定跨模块入口对 Repository 和
AssistantManager 的编排语义。关键流程是用真实临时 SQLite Repository 构造
Manager，覆盖注册、拉取、批量 ack 和 Avatar chat accepted。
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
from hosts.web.avatar.assistant_manager import AvatarAssistantManager
from hosts.web.avatar.errors import AvatarMessageError
from hosts.web.avatar.repository import AvatarMessageRepository
from hosts.web.threads.metadata import ThreadMetadata


class _FakeBridge:
    """测试 bridge，记录 Avatar chat 是否进入 run_once。"""

    def __init__(self) -> None:
        """初始化调用记录。"""
        self.calls: list[dict[str, object]] = []

    async def run_once(
        self,
        text: str,
        *,
        reasoning_effort: str | None = None,
        attachments: list[dict[str, object]] | None = None,
        references: list[dict[str, object]] | None = None,
    ) -> None:
        """记录 run_once 入参。

        关键输入：文本、reasoning effort 和附件。
        关键输出：调用记录追加一条。
        """
        self.calls.append(
            {
                "text": text,
                "reasoning_effort": reasoning_effort,
                "attachments": attachments,
            }
        )


class _FakeCell:
    """测试 cell，提供 AvatarAssistantManager 需要的最小字段。"""

    def __init__(self, thread_id: str) -> None:
        """初始化 fake cell。

        关键输入：thread_id。
        关键输出：带 fake bridge 的 cell。
        """
        self.thread_id = thread_id
        self.bridge = _FakeBridge()
        self.current_run_task = None
        self.touch_count = 0

    def touch(self) -> None:
        """记录 cell 活跃刷新。"""
        self.touch_count += 1


class _FakeThreadManager:
    """测试 ThreadManager，提供 create/list/boot/refresh 入口。"""

    def __init__(self) -> None:
        """初始化 thread 和 cell 存储。"""
        self.threads: dict[str, ThreadMetadata] = {}
        self.cells: dict[str, _FakeCell] = {}
        self.refresh_calls: list[str] = []
        self.submit_calls: list[dict[str, object]] = []

    async def create_thread(
        self,
        name: str,
        preset_id: str = "",
        *,
        backend_kind: str = "generic_chat",
        cwd: str = "",
    ) -> ThreadMetadata:
        """创建 fake generic_chat thread metadata。

        关键输入：name、preset_id、backend_kind 和 cwd。
        关键输出：ThreadMetadata。
        """
        thread_id = f"thread-{'a' * 11}{len(self.threads)}"
        metadata = ThreadMetadata(
            id=thread_id,
            name=name,
            preset_id=preset_id,
            backend_kind=backend_kind,  # type: ignore[arg-type]
            cwd=cwd,
            created_at=1.0,
            updated_at=2.0,
            message_count=0,
        )
        self.threads[thread_id] = metadata
        return metadata

    def list_threads(self) -> list[ThreadMetadata]:
        """返回当前 fake thread 列表。"""
        return list(self.threads.values())

    async def boot_or_attach(self, thread_id: str) -> _FakeCell:
        """返回或创建 fake cell。

        关键输入：thread_id。
        关键输出：_FakeCell。
        """
        if thread_id not in self.threads:
            raise KeyError(thread_id)
        cell = self.cells.get(thread_id)
        if cell is None:
            cell = _FakeCell(thread_id)
            self.cells[thread_id] = cell
        return cell

    async def ensure_cell_runtime_preset_current(self, thread_id: str) -> bool:
        """记录 runtime refresh 调用并返回成功。"""
        self.refresh_calls.append(thread_id)
        return True

    async def submit_avatar_input(
        self,
        thread_id: str,
        text: str,
        *,
        request_id: str | None = None,
        reasoning_effort: str | None = None,
        attachments: list[dict[str, object]] | None = None,
        avatar_run_id: str | None = None,
    ) -> object:
        """记录 Avatar 输入提交到 ThreadManager 统一入口。"""
        await self.boot_or_attach(thread_id)
        self.submit_calls.append(
            {
                "thread_id": thread_id,
                "text": text,
                "request_id": request_id,
                "reasoning_effort": reasoning_effort,
                "attachments": attachments,
                "avatar_run_id": avatar_run_id,
            }
        )
        return object()


def _manager(tmp_path: Path, thread_manager: _FakeThreadManager | None = None) -> AvatarManager:
    """创建使用临时 SQLite 的 AvatarManager。

    关键输入：pytest 临时目录。
    关键输出：可执行真实 repository 路径的 Manager。
    """
    assistant = AvatarAssistantManager(thread_manager) if thread_manager is not None else None
    return AvatarManager(AvatarMessageRepository(tmp_path / "avatar.db"), assistant)


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


@pytest.mark.asyncio
async def test_avatar_chat_creates_generic_thread_and_starts_run(tmp_path: Path) -> None:
    """验证 Avatar chat 首发会创建 generic_chat thread 并启动 run_once。"""
    fake_tm = _FakeThreadManager()
    manager = _manager(tmp_path, fake_tm)

    capabilities = manager.capabilities()
    assert capabilities.message_registry is True
    assert capabilities.avatar_chat is True
    assert capabilities.avatar_realtime_chat is True
    assert capabilities.required_scopes["chat"] == ["avatar.chat"]

    accepted = await manager.chat(
        AvatarChatRequest(
            text="hello from avatar",
            preset_id="local-default",
            cwd="/tmp/avatar",
            reasoning_effort="medium",
        )
    )
    assert accepted.accepted is True
    assert accepted.thread_id in fake_tm.threads
    assert accepted.websocket_url == f"/ws/avatar/v1/threads/{accepted.thread_id}"
    assert accepted.transport == "websocket"

    assert fake_tm.refresh_calls == [accepted.thread_id]
    assert fake_tm.submit_calls == [
        {
            "thread_id": accepted.thread_id,
            "text": "hello from avatar",
            "request_id": None,
            "reasoning_effort": "medium",
            "attachments": None,
            "avatar_run_id": accepted.run_id,
        }
    ]


@pytest.mark.asyncio
async def test_avatar_chat_rejects_missing_preset(tmp_path: Path) -> None:
    """验证首发创建缺 preset 时返回稳定错误。"""
    manager = _manager(tmp_path, _FakeThreadManager())

    with pytest.raises(AvatarMessageError) as exc:
        await manager.chat(AvatarChatRequest(text="hello from avatar"))
    assert exc.value.code == "avatar_preset_required"
