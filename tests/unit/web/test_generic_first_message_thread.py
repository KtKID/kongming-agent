"""generic_chat 空白页首发创建后端单测。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.message import Message
from core.result import Result
from hosts.web.threads.manager import ThreadManager
from hosts.web.threads.metadata import list_thread_metadata, read_thread_metadata
from hosts.web.websocket.routes import _send_history_frame
from infrastructure.config.models import Config


class _FakeSession:
    def __init__(self, session_id: str, *, fail_append: bool = False) -> None:
        self.session_id = session_id
        self._messages: list[Message] = []
        self._fail_append = fail_append
        self._run_index = 0

    async def append(self, message: Message, *, usage: dict[str, Any] | None = None) -> None:
        del usage
        if self._fail_append:
            raise RuntimeError("append failed")
        self._messages.append(message)

    async def history(self) -> list[Message]:
        return list(self._messages)

    async def clear(self) -> None:
        self._messages.clear()

    async def advance_run_index(self) -> int:
        self._run_index += 1
        return self._run_index


class _FakeRuntime:
    def __init__(self, *, fail_append: bool = False) -> None:
        self._fail_append = fail_append
        self._sessions: dict[str, _FakeSession] = {}
        self.aclose = AsyncMock(return_value=None)

    def _get_or_create_session(self, session_id: str) -> _FakeSession:
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing
        created = _FakeSession(session_id, fail_append=self._fail_append)
        self._sessions[session_id] = created
        return created

    def _session_factory(self, session_id: str) -> _FakeSession:
        return self._get_or_create_session(session_id)


class _FakeBridge:
    def __init__(self, *, block_continue: bool = False) -> None:
        self.block_continue = block_continue
        self.continue_started = asyncio.Event()
        self.continue_release = asyncio.Event()
        self.continue_calls: list[str | None] = []
        self.run_once = AsyncMock()

    async def continue_from_last_user_message(
        self,
        *,
        reasoning_effort: str | None = None,
    ) -> Result:
        self.continue_calls.append(reasoning_effort)
        self.continue_started.set()
        if self.block_continue:
            await self.continue_release.wait()
        return Result(
            run_id="run-continued",
            session_id="thread-id",
            status="completed",
            final_message=Message.assistant("continued"),
            turn_count=1,
        )


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


def _make_cfg() -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {
                "enabled": True,
                "idle_timeout_seconds": 60,
                "idle_check_interval_seconds": 10,
                "pending_approval_timeout_seconds": 60,
                "llm_presets": [
                    {
                        "id": "preset-a",
                        "display_name": "Preset A",
                        "provider": "openai_compatible",
                        "base_url": "http://127.0.0.1:1234/v1",
                        "model": "fake",
                    }
                ],
            },
        }
    )


def _make_manager(
    tmp_path: Path,
    *,
    fail_append: bool = False,
    block_continue: bool = False,
) -> tuple[ThreadManager, list[_FakeRuntime], list[_FakeBridge]]:
    runtimes: list[_FakeRuntime] = []
    bridges: list[_FakeBridge] = []

    async def factory(
        thread_id: str,
        preset_id: str,
        adapter: Any,
        sinks: list[Any],
    ) -> tuple[Any, Any]:
        del thread_id, preset_id, adapter, sinks
        runtime = _FakeRuntime(fail_append=fail_append)
        runtimes.append(runtime)
        bridge = _FakeBridge(block_continue=block_continue)
        bridges.append(bridge)
        return runtime, bridge

    return (
        ThreadManager(_make_cfg(), kongming_home=tmp_path, runtime_factory=factory),
        runtimes,
        bridges,
    )


@pytest.mark.asyncio
async def test_create_generic_thread_from_first_message_persists_metadata_and_user_message(
    tmp_path: Path,
) -> None:
    mgr, runtimes, bridges = _make_manager(tmp_path)
    project_dir = tmp_path / "project-a"
    project_dir.mkdir()

    meta = await mgr.create_generic_thread_from_first_message(
        text="  hello generic  ",
        preset_id="preset-a",
        cwd=str(project_dir),
        reasoning_effort="high",
    )

    assert meta.backend_kind == "generic_chat"
    assert meta.preset_id == "preset-a"
    assert meta.cwd == str(project_dir.resolve())
    assert meta.name == "hello generic"
    assert meta.message_count == 1
    assert read_thread_metadata(tmp_path, meta.id) == meta
    history = await runtimes[0]._sessions[meta.id].history()
    assert [(message.role, message.content) for message in history] == [("user", "hello generic")]
    await asyncio.wait_for(bridges[0].continue_started.wait(), timeout=1)
    assert bridges[0].continue_calls == ["high"]


@pytest.mark.asyncio
async def test_create_generic_thread_from_first_message_queues_continuation_without_duplicate_user(
    tmp_path: Path,
) -> None:
    mgr, runtimes, bridges = _make_manager(tmp_path, block_continue=True)

    meta = await mgr.create_generic_thread_from_first_message(
        text="hello once",
        preset_id="preset-a",
        cwd=str(tmp_path),
    )

    await asyncio.wait_for(bridges[0].continue_started.wait(), timeout=1)
    cell = await mgr.boot_or_attach(meta.id)
    assert cell.current_run_task is not None
    history = await runtimes[0]._sessions[meta.id].history()
    assert [(message.role, message.content) for message in history] == [("user", "hello once")]
    assert bridges[0].continue_calls == [None]

    task = cell.current_run_task
    bridges[0].continue_release.set()
    await asyncio.wait_for(task, timeout=1)
    assert cell.current_run_task is None


@pytest.mark.asyncio
async def test_create_generic_thread_from_first_message_empty_cwd_uses_home(
    tmp_path: Path,
) -> None:
    mgr, _, _ = _make_manager(tmp_path)

    meta = await mgr.create_generic_thread_from_first_message(
        text="hello",
        preset_id="preset-a",
        cwd="",
    )

    assert meta.cwd == str(Path.home().resolve())


@pytest.mark.asyncio
async def test_create_generic_thread_from_first_message_append_failure_cleans_metadata(
    tmp_path: Path,
) -> None:
    mgr, _, _ = _make_manager(tmp_path, fail_append=True)

    with pytest.raises(RuntimeError, match="append failed"):
        await mgr.create_generic_thread_from_first_message(
            text="hello",
            preset_id="preset-a",
            cwd=str(tmp_path),
        )

    assert list_thread_metadata(tmp_path) == []


@pytest.mark.asyncio
async def test_create_generic_thread_from_first_message_rejects_unknown_preset(
    tmp_path: Path,
) -> None:
    mgr, _, _ = _make_manager(tmp_path)

    with pytest.raises(ValueError, match="unknown preset_id"):
        await mgr.create_generic_thread_from_first_message(
            text="hello",
            preset_id="missing",
            cwd=str(tmp_path),
        )

    assert list_thread_metadata(tmp_path) == []


@pytest.mark.asyncio
async def test_first_message_is_replayed_by_generic_ws_history(tmp_path: Path) -> None:
    mgr, _, _ = _make_manager(tmp_path)
    meta = await mgr.create_generic_thread_from_first_message(
        text="hello history",
        preset_id="preset-a",
        cwd=str(tmp_path),
    )
    cell = await mgr.boot_or_attach(meta.id)
    ws = _FakeWS()

    await _send_history_frame(ws, cell)

    assert ws.sent
    frame = ws.sent[-1]
    assert frame["frame_type"] == "thread.history"
    assert frame["messages"][0]["role"] == "user"
    assert frame["messages"][0]["content"] == "hello history"
