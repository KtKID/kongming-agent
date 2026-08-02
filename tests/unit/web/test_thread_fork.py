"""完整对话 fork 的 ThreadManager 与资产持久化测试。"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, Literal
from unittest.mock import AsyncMock

import pytest

from core.agent_spec import AgentSpec
from core.message import Message, ToolCall
from core.result import Result
from hosts.shared.host_dispatcher import HostDispatcher
from hosts.web.protocol import PendingInputDTO
from hosts.web.threads import manager as thread_manager_module
from hosts.web.threads.errors import ThreadForkConflictError
from hosts.web.threads.manager import ThreadManager
from hosts.web.threads.metadata import (
    ThreadMetadata,
    list_thread_metadata,
    read_thread_metadata,
)
from hosts.web.uploads.storage import AssetStorage
from infrastructure.config.models import Config
from safety.approval.permissions_manager import PermissionsManager
from safety.approval.rule_models import PermissionRuleRecord


class _Session:
    """记录完整 Message 历史，并可注入 append 失败验证回滚。"""

    def __init__(
        self,
        session_id: str,
        *,
        fail_after: int | None = None,
        append_gate: tuple[threading.Event, threading.Event] | None = None,
    ) -> None:
        self.session_id = session_id
        self.messages: list[Message] = []
        self.run_count = 0
        self.fail_after = fail_after
        self.append_gate = append_gate

    async def append(self, message: Message, *, usage: dict[str, Any] | None = None) -> None:
        """追加消息；达到失败阈值时抛错。"""
        del usage
        if self.fail_after is not None and len(self.messages) >= self.fail_after:
            raise RuntimeError("fork append failed")
        if self.append_gate is not None:
            started, release = self.append_gate
            started.set()
            if not await asyncio.to_thread(release.wait, 5):
                raise TimeoutError("session append release timed out")
        self.messages.append(message)

    async def history(self) -> list[Message]:
        """返回消息副本。"""
        return list(self.messages)

    async def clear(self) -> None:
        """清空历史与 run 计数。"""
        self.messages.clear()
        self.run_count = 0

    async def advance_run_index(self) -> int:
        """递增并返回 run 计数。"""
        self.run_count += 1
        return self.run_count

    async def get_run_count(self) -> int:
        """返回当前 run 计数。"""
        return self.run_count


class _Runtime:
    """只实现 ThreadManager boot 与公开历史门户所需的 runtime 表面。"""

    def __init__(
        self,
        *,
        fail_after: int | None = None,
        append_gate: tuple[threading.Event, threading.Event] | None = None,
    ) -> None:
        self._sessions: dict[str, _Session] = {}
        self._fail_after = fail_after
        self._append_gate = append_gate
        self._spec = AgentSpec(name="root", instructions="test", default_model="fake")
        self.aclose = AsyncMock(return_value=None)

    @property
    def agent_spec(self) -> AgentSpec:
        """返回 HostDispatcher 装配所需的 root spec。"""
        return self._spec

    def session_for_test(self, session_id: str) -> _Session:
        """测试断言按 session_id 创建或复用底层 session。"""
        session = self._sessions.get(session_id)
        if session is None:
            session = _Session(
                session_id,
                fail_after=self._fail_after,
                append_gate=self._append_gate,
            )
            self._sessions[session_id] = session
        return session

    async def read_session_history(self, session_id: str) -> list[Message]:
        """通过公开 runtime 门户读取结构化历史。"""
        return await self.session_for_test(session_id).history()

    async def append_session_message(
        self,
        session_id: str,
        message: Message,
        *,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """通过公开 runtime 门户追加单条消息。"""
        await self.session_for_test(session_id).append(message, usage=usage)

    async def seed_empty_session_history(
        self,
        session_id: str,
        messages: list[Message],
    ) -> None:
        """通过公开 runtime 门户原子播种空 session。"""
        session = self.session_for_test(session_id)
        if await session.history():
            raise ValueError("target session history must be empty")
        try:
            for message in messages:
                await session.append(message)
        except BaseException:
            await session.clear()
            raise

    async def clear_session_history(self, session_id: str) -> None:
        """通过公开 runtime 门户清空测试 session。"""
        await self.session_for_test(session_id).clear()

    async def run(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        reasoning_effort: str | None = None,
        event_context: dict[str, Any] | None = None,
        agent_id: str = "",
    ) -> Result:
        """提供 HostDispatcher 所需的普通 run 入口。"""
        del user_input, reasoning_effort, event_context, agent_id
        return Result(
            run_id="run-test",
            session_id=session_id or "",
            status="completed",
            final_message=Message.assistant("ok"),
            turn_count=1,
        )

    def steer(self, session_id: str, text: str) -> bool:
        """测试 runtime 不接受运行中 steer。"""
        del session_id, text
        return False


def _config(tmp_path: Path) -> Config:
    """构造本地 provider 配置，避免任何外部服务访问。"""
    (tmp_path / "model-providers.yaml").write_text(
        """\
version: 2
providers:
  - provider_id: test
    default_preset_id: preset-a
    display_name: Test
    region_label: Local
    description: test
    logo_text: T
    protocol: openai
    default_base_url: http://127.0.0.1:1234/v1
    request_defaults: {}
    models:
      - preset_id: preset-a
        display_name: Preset A
        model: fake
""",
        encoding="utf-8",
    )
    return Config.model_validate(
        {
            "model": {"preset_id": "preset-a"},
            "session": {
                "backend": "file",
                "file_store_path": str(tmp_path / "sessions"),
            },
            "web": {
                "enabled": True,
                "idle_timeout_seconds": 60,
                "idle_check_interval_seconds": 10,
            },
        }
    )


def _manager(
    tmp_path: Path,
    *,
    fail_target_after: int | None = None,
    asset_storage: AssetStorage | None = None,
    target_append_gate: tuple[threading.Event, threading.Event] | None = None,
    config: Config | None = None,
) -> tuple[ThreadManager, dict[str, _Runtime], AssetStorage]:
    """创建真实 ThreadManager，并记录每个 thread 的测试 runtime。"""
    runtimes: dict[str, _Runtime] = {}
    created_count = 0

    async def factory(
        thread_id: str,
        preset_id: str,
        adapter: Any,
        sinks: list[Any],
    ) -> tuple[Any, Any]:
        """按创建顺序给 fork 目标注入可选 append 失败。"""
        nonlocal created_count
        del preset_id, adapter, sinks
        fail_after = fail_target_after if created_count == 1 else None
        created_count += 1
        append_gate = target_append_gate if created_count == 2 else None
        runtime = _Runtime(fail_after=fail_after, append_gate=append_gate)
        runtimes[thread_id] = runtime
        return runtime, HostDispatcher(runtime=runtime, session_id=thread_id)  # type: ignore[arg-type]

    storage = asset_storage or AssetStorage(base_dir=tmp_path / "uploads")
    return (
        ThreadManager(
            config or _config(tmp_path),
            kongming_home=tmp_path,
            runtime_factory=factory,
            asset_storage=storage,
        ),
        runtimes,
        storage,
    )


class _PendingApprovalManager:
    """为指定 thread 投影等待审批状态。"""

    def __init__(self, thread_id: str) -> None:
        self._thread_id = thread_id

    def has_pending_for_thread(self, thread_id: str, *, channel: str) -> bool:
        """只对目标 generic_chat thread 返回等待审批。"""
        return thread_id == self._thread_id and channel == "generic_chat"


class _BlockingAssetStorage(AssetStorage):
    """把 fork 资产复制停在可观测窗口，验证发布顺序与取消清理。"""

    def __init__(self, base_dir: Path) -> None:
        super().__init__(base_dir)
        self.copy_started = threading.Event()
        self.copy_release = threading.Event()

    def copy_thread_assets(
        self,
        *,
        source_thread_id: str,
        target_thread_id: str,
        references: object = (),
    ) -> int:
        """通知测试复制已开始，并等待显式释放后返回。"""
        del source_thread_id, target_thread_id, references
        self.copy_started.set()
        if not self.copy_release.wait(timeout=5):
            raise TimeoutError("asset copy release timed out")
        return 0


@pytest.mark.asyncio
async def test_fork_copies_full_llm_history_lineage_and_assets(tmp_path: Path) -> None:
    """公开 Manager 入口贯穿 metadata、Session、tool pair 与附件独立副本。"""
    manager, runtimes, storage = _manager(tmp_path)
    source = await manager.create_thread(
        "研究对话",
        "preset-a",
        cwd=str(tmp_path),
    )
    await manager.boot_or_attach(source.id)
    source_session = runtimes[source.id].session_for_test(source.id)
    messages = [
        Message.user(
            "分析图片",
            metadata={
                "attachments": [
                    {
                        "asset_id": "a" * 32,
                        "kind": "image",
                        "mime_type": "image/png",
                        "size_bytes": 3,
                        "preview_url": f"/api/uploads/{'a' * 32}",
                        "status": "ready",
                    }
                ]
            },
        ),
        Message.assistant(
            None,
            tool_calls=[
                ToolCall(
                    call_id="call-1",
                    tool_name="read_file",
                    arguments={"path": "a.txt"},
                )
            ],
            metadata={"reasoning_content": "inspect exact file"},
        ),
        Message.tool_result(
            "call-1",
            "file body",
            name="read_file",
            metadata={"ok": True},
        ),
        Message.assistant("完成"),
    ]
    for message in messages:
        await source_session.append(message)

    asset_id = "a" * 32
    storage.write_asset(
        asset_id=asset_id,
        thread_id=source.id,
        kind="image",
        ext=".png",
        payload=b"png",
        metadata={
            "asset_id": asset_id,
            "thread_id": source.id,
            "kind": "image",
            "mime_type": "image/png",
            "storage_path": f"images/{source.id}/{asset_id}.png",
        },
    )

    forked = await manager.fork_thread(source.id)

    assert forked.forked_from_id == source.id
    assert forked.forked_from_history_index == len(messages) - 1
    assert forked.name == "研究对话（分支）"
    assert forked.preset_id == source.preset_id
    assert forked.cwd == source.cwd
    assert forked.message_count == len(messages)
    assert read_thread_metadata(tmp_path, forked.id) == forked
    forked_history = await runtimes[forked.id].session_for_test(forked.id).history()
    assert forked_history == messages
    assert forked_history[0] is not messages[0]
    assert forked_history[0].metadata is not messages[0].metadata
    assert forked_history[1].tool_calls is not None
    assert messages[1].tool_calls is not None
    assert forked_history[1].tool_calls[0].arguments is not messages[1].tool_calls[0].arguments
    assert await runtimes[forked.id].session_for_test(forked.id).get_run_count() == 0

    copied_metadata = storage.read_asset_metadata(
        asset_id=asset_id,
        thread_id=forked.id,
        kind="image",
    )
    assert copied_metadata["thread_id"] == forked.id
    assert forked.id in copied_metadata["storage_path"]
    await manager.delete_thread(source.id)
    assert (
        storage.read_asset_bytes(
            asset_id=asset_id,
            thread_id=forked.id,
            kind="image",
            ext=".png",
        )
        == b"png"
    )
    await manager.aclose_all()


@pytest.mark.asyncio
async def test_fork_at_terminal_assistant_keeps_complete_tool_pair_and_exact_prefix(
    tmp_path: Path,
) -> None:
    """回复级 fork 只复制到目标 assistant，并保留此前完整工具请求/结果对。"""
    manager, runtimes, _ = _manager(tmp_path)
    source = await manager.create_thread("reply boundary", "preset-a")
    await manager.boot_or_attach(source.id)
    source_session = runtimes[source.id].session_for_test(source.id)
    messages = [
        Message.user("第一轮"),
        Message.assistant(
            None,
            tool_calls=[
                ToolCall(
                    call_id="call-1",
                    tool_name="read_file",
                    arguments={"path": "a.txt"},
                )
            ],
        ),
        Message.tool_result("call-1", "file body", name="read_file"),
        Message.assistant("第一轮完成"),
        Message.user("第二轮"),
        Message.assistant("第二轮完成"),
    ]
    for message in messages:
        await source_session.append(message)

    forked = await manager.fork_thread(source.id, history_index=3)

    forked_history = await runtimes[forked.id].session_for_test(forked.id).history()
    assert forked_history == messages[:4]
    assert forked.message_count == 4
    assert forked.forked_from_history_index == 3
    assert forked_history[1].tool_calls is not None
    assert forked_history[1].tool_calls[0].call_id == forked_history[2].tool_call_id
    await manager.aclose_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("history_index", [0, 1, 2])
async def test_fork_rejects_user_tool_request_and_tool_result_boundaries(
    tmp_path: Path,
    history_index: int,
) -> None:
    """回复级 fork 只接受无 tool_calls 的 assistant 最终回复。"""
    manager, runtimes, _ = _manager(tmp_path)
    source = await manager.create_thread("invalid boundary", "preset-a")
    await manager.boot_or_attach(source.id)
    source_session = runtimes[source.id].session_for_test(source.id)
    messages = [
        Message.user("第一轮"),
        Message.assistant(
            None,
            tool_calls=[
                ToolCall(
                    call_id="call-1",
                    tool_name="read_file",
                    arguments={"path": "a.txt"},
                )
            ],
        ),
        Message.tool_result("call-1", "file body", name="read_file"),
        Message.assistant("第一轮完成"),
    ]
    for message in messages:
        await source_session.append(message)

    with pytest.raises(ValueError, match="terminal assistant"):
        await manager.fork_thread(source.id, history_index=history_index)

    assert [item.id for item in list_thread_metadata(tmp_path)] == [source.id]
    await manager.aclose_all()


@pytest.mark.asyncio
async def test_fork_rejects_terminal_assistant_after_unpaired_tool_request(
    tmp_path: Path,
) -> None:
    """防御损坏历史：目标 assistant 前存在未配对工具请求时拒绝 fork。"""
    manager, runtimes, _ = _manager(tmp_path)
    source = await manager.create_thread("unpaired tool", "preset-a")
    await manager.boot_or_attach(source.id)
    source_session = runtimes[source.id].session_for_test(source.id)
    await source_session.append(Message.user("第一轮"))
    await source_session.append(
        Message.assistant(
            None,
            tool_calls=[
                ToolCall(
                    call_id="call-1",
                    tool_name="read_file",
                    arguments={"path": "a.txt"},
                )
            ],
        )
    )
    await source_session.append(Message.assistant("损坏历史里的最终回复"))

    with pytest.raises(ValueError, match="tool request/result"):
        await manager.fork_thread(source.id, history_index=2)

    assert [item.id for item in list_thread_metadata(tmp_path)] == [source.id]
    await manager.aclose_all()


@pytest.mark.asyncio
async def test_fork_rejects_pending_approval_without_creating_target(
    tmp_path: Path,
) -> None:
    """等待审批属于未闭合 turn，fork 必须返回快照冲突。"""
    manager, _, _ = _manager(tmp_path)
    source = await manager.create_thread("approval", "preset-a")
    await manager.boot_or_attach(source.id)
    manager.set_approval_manager(_PendingApprovalManager(source.id))

    with pytest.raises(ThreadForkConflictError):
        await manager.fork_thread(source.id)

    assert [item.id for item in list_thread_metadata(tmp_path)] == [source.id]
    await manager.aclose_all()


@pytest.mark.asyncio
async def test_fork_rejects_send_now_claim_without_creating_target(
    tmp_path: Path,
) -> None:
    """已认领且尚未注入的 send-now 输入阻止 fork。"""
    manager, _, _ = _manager(tmp_path)
    source = await manager.create_thread("claim", "preset-a")
    cell = await manager.boot_or_attach(source.id)
    cell.pending_input_send_now_claims.append(object())

    with pytest.raises(ThreadForkConflictError):
        await manager.fork_thread(source.id)

    assert [item.id for item in list_thread_metadata(tmp_path)] == [source.id]
    await manager.aclose_all()


@pytest.mark.asyncio
async def test_fork_rejects_pending_queue_without_mutating_source(
    tmp_path: Path,
) -> None:
    """排队输入阻止 fork，源队列内容和版本保持原值。"""
    manager, _, _ = _manager(tmp_path)
    source = await manager.create_thread("queued", "preset-a")
    cell = await manager.boot_or_attach(source.id)
    pending = PendingInputDTO(
        id="pin-fork",
        thread_id=source.id,
        source="user_input",
        priority="user_message",
        content="queued input",
        preview="queued input",
        created_at_ms=1,
        updated_at_ms=1,
        sequence=1,
        metadata={"request_id": "req-fork"},
    )
    cell.pending_inputs.append(pending)
    cell.pending_input_version = 7

    with pytest.raises(ThreadForkConflictError):
        await manager.fork_thread(source.id)

    assert cell.pending_inputs == [pending]
    assert cell.pending_input_version == 7
    assert [item.id for item in list_thread_metadata(tmp_path)] == [source.id]
    await manager.aclose_all()


@pytest.mark.asyncio
async def test_fork_rejects_active_source_without_creating_target(tmp_path: Path) -> None:
    """运行中快照返回冲突，磁盘只保留源 thread。"""
    manager, _, _ = _manager(tmp_path)
    source = await manager.create_thread("active", "preset-a")
    cell = await manager.boot_or_attach(source.id)
    release = asyncio.Event()

    async def active_run() -> Result:
        """阻塞到测试释放，模拟仍在写历史的 run。"""
        await release.wait()
        return Result(
            run_id="run-active",
            session_id=source.id,
            status="completed",
            final_message=Message.assistant("done"),
            turn_count=1,
        )

    task = asyncio.create_task(active_run())
    cell.current_run_task = task
    with pytest.raises(ThreadForkConflictError):
        await manager.fork_thread(source.id)
    assert [item.id for item in list_thread_metadata(tmp_path)] == [source.id]
    release.set()
    await task
    cell.current_run_task = None
    await manager.aclose_all()


@pytest.mark.asyncio
async def test_fork_keeps_target_private_until_asset_copy_finishes(
    tmp_path: Path,
) -> None:
    """资产与历史完整落盘前，列表看不到目标 metadata。"""
    storage = _BlockingAssetStorage(tmp_path / "uploads")
    manager, runtimes, _ = _manager(tmp_path, asset_storage=storage)
    source = await manager.create_thread("private", "preset-a")
    await manager.boot_or_attach(source.id)
    await (
        runtimes[source.id]
        .session_for_test(source.id)
        .append(
            Message.user(
                "asset",
                metadata={
                    "attachments": [{"asset_id": "a" * 32, "kind": "image", "status": "ready"}]
                },
            )
        )
    )

    fork_task = asyncio.create_task(manager.fork_thread(source.id))
    assert await asyncio.to_thread(storage.copy_started.wait, 2)
    assert [item.id for item in list_thread_metadata(tmp_path)] == [source.id]
    storage.copy_release.set()
    forked = await fork_task
    assert {item.id for item in list_thread_metadata(tmp_path)} == {
        source.id,
        forked.id,
    }
    await manager.aclose_all()


@pytest.mark.asyncio
async def test_fork_cancellation_waits_for_io_and_removes_private_target(
    tmp_path: Path,
) -> None:
    """请求取消等待资产 IO 收口，并清理目标 metadata、Session 与资产。"""
    storage = _BlockingAssetStorage(tmp_path / "uploads")
    manager, runtimes, _ = _manager(tmp_path, asset_storage=storage)
    source = await manager.create_thread("cancel", "preset-a")
    await manager.boot_or_attach(source.id)
    await (
        runtimes[source.id]
        .session_for_test(source.id)
        .append(
            Message.user(
                "asset",
                metadata={
                    "attachments": [{"asset_id": "a" * 32, "kind": "image", "status": "ready"}]
                },
            )
        )
    )

    fork_task = asyncio.create_task(manager.fork_thread(source.id))
    assert await asyncio.to_thread(storage.copy_started.wait, 2)
    fork_task.cancel()
    storage.copy_release.set()
    with pytest.raises(asyncio.CancelledError):
        await fork_task

    assert [item.id for item in list_thread_metadata(tmp_path)] == [source.id]
    target_ids = [thread_id for thread_id in runtimes if thread_id != source.id]
    assert len(target_ids) == 1
    target_id = target_ids[0]
    assert await runtimes[target_id].session_for_test(target_id).history() == []
    assert not (storage.base_dir / "images" / target_id).exists()
    await manager.aclose_all()


@pytest.mark.asyncio
async def test_fork_cancellation_during_session_append_clears_target(
    tmp_path: Path,
) -> None:
    """请求取消等待 Session append 收口，并清理已追加的目标历史。"""
    append_started = threading.Event()
    append_release = threading.Event()
    manager, runtimes, storage = _manager(
        tmp_path,
        target_append_gate=(append_started, append_release),
    )
    source = await manager.create_thread("cancel append", "preset-a")
    await manager.boot_or_attach(source.id)
    await runtimes[source.id].session_for_test(source.id).append(Message.user("one"))

    fork_task = asyncio.create_task(manager.fork_thread(source.id))
    assert await asyncio.to_thread(append_started.wait, 2)
    fork_task.cancel()
    append_release.set()
    with pytest.raises(asyncio.CancelledError):
        await fork_task

    assert [item.id for item in list_thread_metadata(tmp_path)] == [source.id]
    target_ids = [thread_id for thread_id in runtimes if thread_id != source.id]
    assert len(target_ids) == 1
    target_id = target_ids[0]
    assert await runtimes[target_id].session_for_test(target_id).history() == []
    assert not (storage.base_dir / "images" / target_id).exists()
    await manager.aclose_all()


@pytest.mark.asyncio
async def test_fork_cancellation_during_metadata_commit_removes_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """metadata 已可见时取消，竞争 boot 也不能复活被清理的目标。"""
    manager, runtimes, storage = _manager(tmp_path)
    source = await manager.create_thread("cancel metadata", "preset-a")
    await manager.boot_or_attach(source.id)
    await runtimes[source.id].session_for_test(source.id).append(Message.user("one"))
    commit_started = threading.Event()
    commit_release = threading.Event()
    original_write = thread_manager_module.write_thread_metadata

    def blocking_write(home: Path, meta: ThreadMetadata) -> None:
        """先发布 fork 目标 metadata，再阻塞以打开取消与 boot 竞争窗口。"""
        if meta.forked_from_id is not None:
            original_write(home, meta)
            commit_started.set()
            if not commit_release.wait(timeout=5):
                raise TimeoutError("metadata commit release timed out")
            return
        original_write(home, meta)

    monkeypatch.setattr(thread_manager_module, "write_thread_metadata", blocking_write)
    fork_task = asyncio.create_task(manager.fork_thread(source.id))
    assert await asyncio.to_thread(commit_started.wait, 2)
    target_ids = [thread_id for thread_id in runtimes if thread_id != source.id]
    assert len(target_ids) == 1
    target_id = target_ids[0]
    boot_task = asyncio.create_task(manager.boot_or_attach(target_id))
    await asyncio.sleep(0)
    fork_task.cancel()
    commit_release.set()
    with pytest.raises(asyncio.CancelledError):
        await fork_task
    with pytest.raises(KeyError, match="metadata not found"):
        await boot_task

    assert [item.id for item in list_thread_metadata(tmp_path)] == [source.id]
    assert await runtimes[target_id].session_for_test(target_id).history() == []
    assert not (storage.base_dir / "images" / target_id).exists()
    assert target_id not in manager._cells
    await manager.aclose_all()


@pytest.mark.asyncio
async def test_fork_append_failure_rolls_back_metadata_session_and_assets(tmp_path: Path) -> None:
    """目标消息复制失败时清理全部目标状态。"""
    manager, runtimes, storage = _manager(tmp_path, fail_target_after=1)
    source = await manager.create_thread("rollback", "preset-a")
    await manager.boot_or_attach(source.id)
    source_session = runtimes[source.id].session_for_test(source.id)
    await source_session.append(Message.user("one"))
    await source_session.append(Message.assistant("two"))
    storage.write_asset(
        asset_id="b" * 32,
        thread_id=source.id,
        kind="image",
        ext=".png",
        payload=b"png",
        metadata={"thread_id": source.id},
    )

    with pytest.raises(RuntimeError, match="fork append failed"):
        await manager.fork_thread(source.id)

    assert [item.id for item in list_thread_metadata(tmp_path)] == [source.id]
    target_ids = [thread_id for thread_id in runtimes if thread_id != source.id]
    assert len(target_ids) == 1
    target_id = target_ids[0]
    assert await runtimes[target_id].session_for_test(target_id).history() == []
    assert not (storage.base_dir / "images" / target_id).exists()
    await manager.aclose_all()


@pytest.mark.asyncio
async def test_fork_metadata_commit_failure_rolls_back_private_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最终 metadata 提交失败时清理已写入的目标 Session。"""
    manager, runtimes, _ = _manager(tmp_path)
    source = await manager.create_thread("commit failure", "preset-a")
    await manager.boot_or_attach(source.id)
    await runtimes[source.id].session_for_test(source.id).append(Message.user("one"))
    original_write = thread_manager_module.write_thread_metadata

    def failing_write(home: Path, meta: ThreadMetadata) -> None:
        """对 fork 目标注入最终提交失败。"""
        if meta.forked_from_id is not None:
            raise OSError("fork metadata commit failed")
        original_write(home, meta)

    monkeypatch.setattr(thread_manager_module, "write_thread_metadata", failing_write)

    with pytest.raises(OSError, match="fork metadata commit failed"):
        await manager.fork_thread(source.id)

    assert [item.id for item in list_thread_metadata(tmp_path)] == [source.id]
    target_ids = [thread_id for thread_id in runtimes if thread_id != source.id]
    assert len(target_ids) == 1
    target_id = target_ids[0]
    assert await runtimes[target_id].session_for_test(target_id).history() == []
    await manager.aclose_all()


@pytest.mark.asyncio
async def test_fork_copies_only_ready_assets_referenced_by_history(
    tmp_path: Path,
) -> None:
    """ready 引用闭包进入目标，processing 与未引用资产留在源 thread。"""
    manager, runtimes, storage = _manager(tmp_path)
    source = await manager.create_thread("closure", "preset-a")
    await manager.boot_or_attach(source.id)
    source_session = runtimes[source.id].session_for_test(source.id)
    ready_id = "a" * 32
    processing_id = "b" * 32
    unreferenced_id = "c" * 32
    await source_session.append(
        Message.user(
            "assets",
            metadata={
                "attachments": [
                    {"asset_id": ready_id, "kind": "image", "status": "ready"},
                    {
                        "asset_id": processing_id,
                        "kind": "image",
                        "status": "processing",
                    },
                ]
            },
        )
    )
    for asset_id in (ready_id, processing_id, unreferenced_id):
        storage.write_asset(
            asset_id=asset_id,
            thread_id=source.id,
            kind="image",
            ext=".png",
            payload=asset_id.encode(),
            metadata={
                "asset_id": asset_id,
                "thread_id": source.id,
                "kind": "image",
                "storage_path": f"images/{source.id}/{asset_id}.png",
            },
        )

    forked = await manager.fork_thread(source.id)

    assert storage.asset_exists(
        asset_id=ready_id,
        thread_id=forked.id,
        kind="image",
        ext=".png",
    )
    for asset_id in (processing_id, unreferenced_id):
        assert not storage.asset_exists(
            asset_id=asset_id,
            thread_id=forked.id,
            kind="image",
            ext=".png",
        )
    await manager.aclose_all()


@pytest.mark.asyncio
async def test_fork_preserves_multi_suffix_file_payload(
    tmp_path: Path,
) -> None:
    """文件附件使用多段扩展名时仍按 asset_id 精确复制 payload。"""
    manager, runtimes, storage = _manager(tmp_path)
    source = await manager.create_thread("archive", "preset-a")
    await manager.boot_or_attach(source.id)
    asset_id = "e" * 32
    await (
        runtimes[source.id]
        .session_for_test(source.id)
        .append(
            Message.user(
                "archive",
                metadata={
                    "attachments": [{"asset_id": asset_id, "kind": "file", "status": "ready"}]
                },
            )
        )
    )
    storage.write_asset(
        asset_id=asset_id,
        thread_id=source.id,
        kind="file",
        ext=".tar.gz",
        payload=b"archive-payload",
        metadata={
            "asset_id": asset_id,
            "thread_id": source.id,
            "kind": "file",
            "storage_path": f"files/{source.id}/{asset_id}.tar.gz",
        },
    )

    forked = await manager.fork_thread(source.id)

    assert (
        storage.read_asset_bytes(
            asset_id=asset_id,
            thread_id=forked.id,
            kind="file",
            ext=".tar.gz",
        )
        == b"archive-payload"
    )
    await manager.aclose_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_part", ["metadata", "payload"])
async def test_fork_missing_referenced_asset_part_rolls_back(
    tmp_path: Path,
    missing_part: str,
) -> None:
    """ready 引用缺少 payload 或 metadata 时整体失败且不发布目标。"""
    manager, runtimes, storage = _manager(tmp_path)
    source = await manager.create_thread("missing", "preset-a")
    await manager.boot_or_attach(source.id)
    source_session = runtimes[source.id].session_for_test(source.id)
    asset_id = "d" * 32
    await source_session.append(
        Message.user(
            "asset",
            metadata={"attachments": [{"asset_id": asset_id, "kind": "image", "status": "ready"}]},
        )
    )
    storage.write_asset(
        asset_id=asset_id,
        thread_id=source.id,
        kind="image",
        ext=".png",
        payload=b"payload",
        metadata={
            "asset_id": asset_id,
            "thread_id": source.id,
            "kind": "image",
            "storage_path": f"images/{source.id}/{asset_id}.png",
        },
    )
    if missing_part == "metadata":
        (storage.base_dir / "images" / source.id / f"{asset_id}.json").unlink()
    else:
        (storage.base_dir / "images" / source.id / f"{asset_id}.png").unlink()

    with pytest.raises(FileNotFoundError):
        await manager.fork_thread(source.id)

    assert [item.id for item in list_thread_metadata(tmp_path)] == [source.id]
    await manager.aclose_all()


@pytest.mark.asyncio
async def test_fork_second_asset_failure_removes_first_copied_asset(
    tmp_path: Path,
) -> None:
    """附件 A 已复制后附件 B 缺失，回滚必须删除目标中的 A。"""
    manager, runtimes, storage = _manager(tmp_path)
    source = await manager.create_thread("partial asset rollback", "preset-a")
    await manager.boot_or_attach(source.id)
    source_session = runtimes[source.id].session_for_test(source.id)
    first_asset_id = "a" * 32
    second_asset_id = "b" * 32
    await source_session.append(
        Message.user(
            "two assets",
            metadata={
                "attachments": [
                    {
                        "asset_id": first_asset_id,
                        "kind": "image",
                        "status": "ready",
                    },
                    {
                        "asset_id": second_asset_id,
                        "kind": "image",
                        "status": "ready",
                    },
                ]
            },
        )
    )
    storage.write_asset(
        asset_id=first_asset_id,
        thread_id=source.id,
        kind="image",
        ext=".png",
        payload=b"first",
        metadata={
            "asset_id": first_asset_id,
            "thread_id": source.id,
            "kind": "image",
            "storage_path": f"images/{source.id}/{first_asset_id}.png",
        },
    )
    storage.write_asset(
        asset_id=second_asset_id,
        thread_id=source.id,
        kind="image",
        ext=".png",
        payload=b"second",
        metadata={
            "asset_id": second_asset_id,
            "thread_id": source.id,
            "kind": "image",
            "storage_path": f"images/{source.id}/{second_asset_id}.png",
        },
    )
    (storage.base_dir / "images" / source.id / f"{second_asset_id}.json").unlink()

    with pytest.raises(FileNotFoundError, match=second_asset_id):
        await manager.fork_thread(source.id)

    target_ids = [thread_id for thread_id in runtimes if thread_id != source.id]
    assert len(target_ids) == 1
    target_id = target_ids[0]
    assert [item.id for item in list_thread_metadata(tmp_path)] == [source.id]
    assert not (storage.base_dir / "images" / target_id).exists()
    await manager.aclose_all()


@pytest.mark.asyncio
async def test_fork_rejects_provider_owned_history(tmp_path: Path) -> None:
    """Claude/Codex thread 继续由各自 provider session 管理历史。"""
    manager, _, _ = _manager(tmp_path)
    source = await manager.create_thread(
        "codex source",
        "",
        backend_kind="codex",
    )

    with pytest.raises(ValueError, match="generic_chat"):
        await manager.fork_thread(source.id)

    assert [item.id for item in list_thread_metadata(tmp_path)] == [source.id]
    await manager.aclose_all()


@pytest.mark.asyncio
async def test_fork_rejects_scheduled_task_history(tmp_path: Path) -> None:
    """scheduled task thread 保持调度器所有权并拒绝通用 fork。"""
    manager, _, _ = _manager(tmp_path)
    source_id = await manager.create_scheduled_task_thread(
        task_id="task-daily",
        name="daily",
        preset_id="preset-a",
    )

    with pytest.raises(ValueError, match="scheduled task"):
        await manager.fork_thread(source_id)

    assert [item.id for item in list_thread_metadata(tmp_path)] == [source_id]
    await manager.aclose_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_fork_rejects_non_file_backend_before_runtime_creation(
    tmp_path: Path,
    backend: Literal["memory", "sqlite"],
) -> None:
    """完整 fork 只接受 FileSession，其他后端在创建目标 runtime 前拒绝。"""
    file_config = _config(tmp_path)
    sqlite_path = tmp_path / "sessions.db"
    non_file_config = file_config.model_copy(
        update={
            "session": file_config.session.model_copy(
                update={
                    "backend": backend,
                    "store_path": str(sqlite_path),
                },
            )
        }
    )
    manager, runtimes, _ = _manager(tmp_path, config=non_file_config)
    source = await manager.create_thread(f"{backend} source", "preset-a")

    with pytest.raises(ValueError, match=r"session\.backend=file"):
        await manager.fork_thread(source.id)

    assert runtimes == {}
    assert not sqlite_path.exists()
    assert [item.id for item in list_thread_metadata(tmp_path)] == [source.id]
    await manager.aclose_all()


@pytest.mark.asyncio
async def test_fork_rejects_legacy_provider_owned_generic_thread(
    tmp_path: Path,
) -> None:
    """带 provider session ID 的 legacy generic metadata 仍使用 provider 历史真源。"""
    manager, _, _ = _manager(tmp_path)
    source = await manager.create_thread("legacy claude", "preset-a")
    legacy = source.model_copy(update={"claude_thread_id": "sdk-session-1"})
    thread_manager_module.write_thread_metadata(tmp_path, legacy)

    with pytest.raises(ValueError, match="provider-owned"):
        await manager.fork_thread(source.id)

    assert [item.id for item in list_thread_metadata(tmp_path)] == [source.id]
    await manager.aclose_all()


@pytest.mark.asyncio
async def test_repeated_and_nested_forks_use_new_ids_direct_lineage_and_fresh_state(
    tmp_path: Path,
) -> None:
    """顺序重复 fork 创建独立目标，嵌套 fork 指向直接父且状态从零开始。"""
    manager, runtimes, _ = _manager(tmp_path)
    permissions = PermissionsManager(tmp_path)
    manager.set_permissions_manager(permissions)
    source = await manager.create_thread("研" * 200, "preset-a", cwd=str(tmp_path))
    await manager.boot_or_attach(source.id)
    source_session = runtimes[source.id].session_for_test(source.id)
    await source_session.append(Message.user("history"))
    await source_session.advance_run_index()
    await source_session.advance_run_index()
    await permissions.replace(
        source.id,
        allow=[PermissionRuleRecord(expression="read_file")],
        deny=[],
        expected_revision=0,
    )

    first = await manager.fork_thread(source.id)
    second = await manager.fork_thread(source.id)
    nested = await manager.fork_thread(first.id)

    assert len({first.id, second.id, nested.id, source.id}) == 4
    assert first.forked_from_id == source.id
    assert second.forked_from_id == source.id
    assert nested.forked_from_id == first.id
    for target in (first, second, nested):
        assert target.name.endswith("（分支）")
        assert len(target.name) <= 200
        assert target.preset_id == source.preset_id
        assert target.cwd == source.cwd
        assert await runtimes[target.id].session_for_test(target.id).get_run_count() == 0
        assert (await permissions.snapshot(target.id)).revision == 0
    assert (await permissions.snapshot(source.id)).revision == 1
    await manager.aclose_all()
