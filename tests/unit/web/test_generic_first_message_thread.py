"""generic_chat 空白页首发创建后端单测。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.agent_spec import AgentSpec
from core.contracts import LLMRequest
from core.message import Message
from core.result import Result
from hosts.shared.host_dispatcher import HostDispatcher
from hosts.web.protocol import CreateGenericThreadFromFirstMessageRequest
from hosts.web.routers.threads import (
    create_generic_thread_from_first_message as first_message_route,
)
from hosts.web.threads.manager import ThreadManager
from hosts.web.threads.metadata import list_thread_metadata, read_thread_metadata
from hosts.web.websocket.routes import _send_history_frame
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.model_provider_catalog import ModelProviderCatalogError
from infrastructure.config.models import Config
from infrastructure.llm_providers.openai_responses import OpenAIResponsesProvider

_TEST_CATALOG = """\
version: 2
providers:
  - provider_id: test
    default_preset_id: preset-a
    display_name: Test
    region_label: Local
    description: test provider
    logo_text: T
    protocol: openai
    default_base_url: http://127.0.0.1:1234/v1
    request_defaults: {}
    models:
      - preset_id: preset-a
        display_name: Preset A
        model: fake
        reasoning:
          adapter: configurable_patch
          supported_efforts: [high]
          default_effort: high
          supports_disabled: true
          enabled_patch: {thinking: {type: enabled}}
          disabled_patch: {thinking: {type: disabled}}
          effort_path: reasoning_effort
"""


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
    def __init__(self, *, fail_append: bool = False, block_continue: bool = False) -> None:
        self._fail_append = fail_append
        self._sessions: dict[str, _FakeSession] = {}
        self.aclose = AsyncMock(return_value=None)
        self._spec = AgentSpec(name="root", instructions="i", default_model="fake")
        self.block_continue = block_continue
        self.continue_started = asyncio.Event()
        self.continue_release = asyncio.Event()
        self.continue_calls: list[dict[str, Any]] = []
        self.run_calls: list[dict[str, Any]] = []

    @property
    def agent_spec(self) -> AgentSpec:
        return self._spec

    def _get_or_create_session(self, session_id: str) -> _FakeSession:
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing
        created = _FakeSession(session_id, fail_append=self._fail_append)
        self._sessions[session_id] = created
        return created

    def _session_factory(self, session_id: str) -> _FakeSession:
        return self._get_or_create_session(session_id)

    async def read_session_history(self, session_id: str) -> list[Message]:
        """通过公开 runtime 门户读取测试历史。"""
        return await self._get_or_create_session(session_id).history()

    async def append_session_message(
        self,
        session_id: str,
        message: Message,
        *,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """通过公开 runtime 门户追加测试消息。"""
        await self._get_or_create_session(session_id).append(message, usage=usage)

    async def seed_empty_session_history(
        self,
        session_id: str,
        messages: list[Message],
    ) -> None:
        """按公开 runtime 合同向空测试 session 播种。"""
        session = self._get_or_create_session(session_id)
        if await session.history():
            raise ValueError("target session history must be empty")
        try:
            for message in messages:
                await session.append(message)
        except BaseException:
            await session.clear()
            raise

    async def clear_session_history(self, session_id: str) -> None:
        """通过公开 runtime 门户清空测试历史。"""
        await self._get_or_create_session(session_id).clear()

    async def continue_from_last_user_message(
        self,
        *,
        session_id: str,
        reasoning_effort: str | None = None,
        event_context: dict[str, Any] | None = None,
        agent_id: str = "",
    ) -> Result:
        self.continue_calls.append(
            {
                "session_id": session_id,
                "reasoning_effort": reasoning_effort,
                "event_context": event_context,
                "agent_id": agent_id,
            }
        )
        self.continue_started.set()
        if self.block_continue:
            await self.continue_release.wait()
        return Result(
            run_id="run-continued",
            session_id=session_id,
            status="completed",
            final_message=Message.assistant("continued"),
            turn_count=1,
        )

    async def run(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        reasoning_effort: str | None = None,
        event_context: dict[str, Any] | None = None,
        agent_id: str = "",
    ) -> Result:
        self.run_calls.append(
            {
                "user_input": user_input,
                "session_id": session_id,
                "reasoning_effort": reasoning_effort,
                "event_context": event_context,
                "agent_id": agent_id,
            }
        )
        return Result(
            run_id="run-normal",
            session_id=session_id or "",
            status="completed",
            final_message=Message.assistant("normal"),
            turn_count=1,
        )

    def steer(self, session_id: str, text: str) -> bool:
        del session_id, text
        return False


class _PayloadRuntime(_FakeRuntime):
    """把首发 reasoning effort 继续转换为真实 provider payload。"""

    def __init__(
        self,
        provider: OpenAIResponsesProvider,
        payloads: list[dict[str, Any]],
    ) -> None:
        super().__init__()
        self._provider = provider
        self._payloads = payloads

    async def continue_from_last_user_message(
        self,
        *,
        session_id: str,
        reasoning_effort: str | None = None,
        event_context: dict[str, Any] | None = None,
        agent_id: str = "",
    ) -> Result:
        """记录 route 传入的 effort 对应最终 HTTP payload。"""
        _ = event_context, agent_id
        self._payloads.append(
            self._provider._build_payload(
                LLMRequest(
                    model=None,
                    messages=(Message.user("hello"),),
                    reasoning_effort=reasoning_effort,  # type: ignore[arg-type]
                )
            )
        )
        self.continue_started.set()
        return Result(
            run_id=f"run-{session_id}",
            session_id=session_id,
            status="completed",
            final_message=Message.assistant("ok"),
            turn_count=1,
        )


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


def _make_cfg(tmp_path: Path) -> Config:
    (tmp_path / "model-providers.yaml").write_text(_TEST_CATALOG, encoding="utf-8")
    return Config.model_validate(
        {
            "model": {"preset_id": "preset-a", "reasoning_effort": "high"},
            "web": {
                "enabled": True,
                "idle_timeout_seconds": 60,
                "idle_check_interval_seconds": 10,
            },
        }
    )


def _make_manager(
    tmp_path: Path,
    *,
    fail_append: bool = False,
    block_continue: bool = False,
) -> tuple[ThreadManager, list[_FakeRuntime], list[_FakeRuntime]]:
    runtimes: list[_FakeRuntime] = []
    bridges: list[_FakeRuntime] = []

    async def factory(
        thread_id: str,
        preset_id: str,
        adapter: Any,
        sinks: list[Any],
    ) -> tuple[Any, Any]:
        del preset_id, adapter, sinks
        runtime = _FakeRuntime(fail_append=fail_append, block_continue=block_continue)
        dispatcher = HostDispatcher(runtime=runtime, session_id=thread_id)  # type: ignore[arg-type]
        runtimes.append(runtime)
        bridges.append(runtime)
        return runtime, dispatcher

    return (
        ThreadManager(_make_cfg(tmp_path), kongming_home=tmp_path, runtime_factory=factory),
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
    cell = await mgr.boot_or_attach(meta.id)
    agent_manager = cell.host_dispatcher.agent_manager
    assert agent_manager is not None
    assert bridges[0].continue_calls == [
        {
            "session_id": meta.id,
            "reasoning_effort": "high",
            "event_context": {
                "run_epoch": 0,
                "mail_kind": "user_message",
                "mail_task_id": "",
                "conversation_id": meta.id,
            },
            "agent_id": agent_manager._root_agent_id,
        }
    ]
    await mgr.aclose_all()


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
    agent_manager = cell.host_dispatcher.agent_manager
    assert agent_manager is not None
    assert bridges[0].continue_calls == [
        {
            "session_id": meta.id,
            "reasoning_effort": None,
            "event_context": {
                "run_epoch": 0,
                "mail_kind": "user_message",
                "mail_task_id": "",
                "conversation_id": meta.id,
            },
            "agent_id": agent_manager._root_agent_id,
        }
    ]

    task = cell.current_run_task
    bridges[0].continue_release.set()
    await asyncio.wait_for(task, timeout=1)
    assert cell.current_run_task is None
    await mgr.aclose_all()


@pytest.mark.asyncio
async def test_create_generic_thread_from_first_message_empty_cwd_stays_empty(
    tmp_path: Path,
) -> None:
    mgr, _, _ = _make_manager(tmp_path)

    meta = await mgr.create_generic_thread_from_first_message(
        text="hello",
        preset_id="preset-a",
        cwd="",
    )

    assert meta.cwd == ""
    await mgr.aclose_all()


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
    await mgr.aclose_all()


@pytest.mark.asyncio
async def test_create_generic_thread_from_first_message_rejects_unknown_preset(
    tmp_path: Path,
) -> None:
    mgr, _, _ = _make_manager(tmp_path)

    with pytest.raises(ModelProviderCatalogError) as exc_info:
        await mgr.create_generic_thread_from_first_message(
            text="hello",
            preset_id="missing",
            cwd=str(tmp_path),
        )
    assert exc_info.value.code.value == "preset_unknown"

    assert list_thread_metadata(tmp_path) == []
    await mgr.aclose_all()


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
    await mgr.aclose_all()


@pytest.mark.asyncio
async def test_first_message_route_none_and_high_reach_provider_payload(tmp_path: Path) -> None:
    """Web 首发 DTO 到 provider payload 的薄链路保留显式 none/high。"""
    cfg = _make_cfg(tmp_path)
    catalog_manager = ModelCatalogManager(
        builtin_path=tmp_path / "model-providers.yaml",
        user_path=tmp_path / "missing.yaml",
    )
    payloads: list[dict[str, Any]] = []
    runtimes: list[_PayloadRuntime] = []

    async def factory(
        thread_id: str,
        preset_id: str,
        adapter: Any,
        sinks: list[Any],
    ) -> tuple[_PayloadRuntime, HostDispatcher]:
        """按 thread preset 构造真实 provider 与可控 runtime。"""
        _ = adapter, sinks
        snapshot = catalog_manager.resolve_runtime(cfg.model, preset_id=preset_id)
        credential = catalog_manager.resolve_credential(snapshot)
        provider = OpenAIResponsesProvider(
            model_config=snapshot,
            credential=credential,
        )
        runtime = _PayloadRuntime(provider, payloads)
        runtimes.append(runtime)
        return runtime, HostDispatcher(runtime=runtime, session_id=thread_id)  # type: ignore[arg-type]

    manager = ThreadManager(
        cfg,
        kongming_home=tmp_path,
        runtime_factory=factory,
        model_catalog_manager=catalog_manager,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(thread_manager=manager)))

    for effort in ("none", "high"):
        await first_message_route(
            CreateGenericThreadFromFirstMessageRequest(
                text=f"reasoning {effort}",
                preset_id="preset-a",
                cwd=str(tmp_path),
                reasoning_effort=effort,
            ),
            request,  # type: ignore[arg-type]
        )
        await asyncio.wait_for(runtimes[-1].continue_started.wait(), timeout=1)

    assert payloads[0]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payloads[0]
    assert payloads[1]["thinking"] == {"type": "enabled"}
    assert payloads[1]["reasoning_effort"] == "high"
    await manager.aclose_all()
