"""Web 完整对话 fork 的持久化 smoke。

关键流程通过真实 FastAPI TestClient、ThreadManager、FileSession、metadata store
和 AssetStorage 完成：REST 首发创建、WS history、REST fork、真实失败回滚与
进程恢复。fake 只替换外部 LLM。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from core.contracts import (
    LLMRequest,
    LLMResponse,
    ProviderUsageFamily,
    ProviderUsageSnapshot,
)
from core.message import Message, ToolCall
from hosts.shared.host_dispatcher import HostDispatcher
from hosts.web.app import create_app
from hosts.web.app_support.host_adapter import WebHostAdapter
from hosts.web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from hosts.web.auth.secrets import hash_password
from hosts.web.threads.manager import ThreadManager
from hosts.web.threads.metadata import list_thread_metadata
from hosts.web.uploads.storage import AssetStorage
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.models import Config
from infrastructure.llm_providers.usage import ProviderUsageManager
from runtime_assembly.session_engine import SessionEngine
from sessions.file_session import FileSession
from sessions.session_bootstrap import SessionBootstrap

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


class _StubLLM:
    """隔离外部模型边界的确定性 LLMProvider。"""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """记录请求语义由 Runner 负责，本测试固定返回终态 assistant。"""
        del request
        return LLMResponse(message=Message.assistant("ok"), finish_reason="stop")


class _FailingSeedFileSession(FileSession):
    """真实 FileSession 在指定 append 次序注入持久化失败。"""

    def __init__(
        self,
        session_id: str,
        bootstrap: SessionBootstrap,
        store_path: str,
        *,
        fail_at: int,
    ) -> None:
        super().__init__(session_id, bootstrap, store_path)
        self._fail_at = fail_at
        self._append_attempts = 0

    async def append(
        self,
        message: Message,
        *,
        usage: ProviderUsageSnapshot | None = None,
    ) -> None:
        """在第 fail_at 条消息写盘前抛出原始 OSError。"""
        self._append_attempts += 1
        if self._append_attempts == self._fail_at:
            raise OSError("injected REST fork FileSession failure")
        await super().append(message, usage=usage)


class _PublicOnlyRuntime:
    """包装真实 SessionEngine，记录任务级门户并拒绝 raw Session 入口。"""

    _BLOCKED_SESSION_NAMES = frozenset(
        {
            "_session_factory",
            "_sessions",
            "_get_or_create_session",
            "session_factory",
            "get_or_create_session",
        }
    )

    def __init__(self, engine: SessionEngine) -> None:
        self._engine = engine
        self.history_calls: list[str] = []
        self.append_calls: list[str] = []
        self.seed_calls: list[str] = []
        self.clear_calls: list[str] = []
        self.private_accesses: list[str] = []

    def __getattribute__(self, name: str) -> Any:
        """让 Web 对 raw Session 名称的任何访问立即暴露为测试失败。"""
        blocked = object.__getattribute__(self, "_BLOCKED_SESSION_NAMES")
        if name in blocked:
            private_accesses = object.__getattribute__(self, "private_accesses")
            private_accesses.append(name)
            raise AssertionError(f"raw Session access: {name}")
        return object.__getattribute__(self, name)

    def __getattr__(self, name: str) -> Any:
        """其余公开运行时能力委托给真实 SessionEngine。"""
        return getattr(self._engine, name)

    @property
    def engine_for_test(self) -> SessionEngine:
        """向测试断言暴露真实引擎，Web 生产入口不会消费此属性。"""
        return self._engine

    async def read_session_history(self, session_id: str) -> list[Message]:
        """记录 Web history/fork 读取并委托真实门户。"""
        self.history_calls.append(session_id)
        return await self._engine.read_session_history(session_id)

    async def append_session_message(
        self,
        session_id: str,
        message: Message,
        *,
        usage: ProviderUsageSnapshot | None = None,
    ) -> None:
        """记录首条消息写入并委托真实门户。"""
        self.append_calls.append(session_id)
        await self._engine.append_session_message(session_id, message, usage=usage)

    async def seed_empty_session_history(
        self,
        session_id: str,
        messages: Sequence[Message],
    ) -> None:
        """记录 fork 播种并委托真实门户。"""
        self.seed_calls.append(session_id)
        await self._engine.seed_empty_session_history(session_id, messages)

    async def clear_session_history(self, session_id: str) -> None:
        """记录 fork 补偿清空并委托真实门户。"""
        self.clear_calls.append(session_id)
        await self._engine.clear_session_history(session_id)


def _write_model_catalog(home: Path) -> None:
    """写入本地模型目录，使创建 generic_chat thread 通过 preset 校验。"""
    home.mkdir(parents=True, exist_ok=True)
    (home / "model-providers.yaml").write_text(
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


def _seed_password(home: Path, password: str) -> None:
    """写入认证 hash，让 TestClient 走真实登录与 CSRF 中间件。"""
    web_dir = home / "web"
    web_dir.mkdir(parents=True, exist_ok=True)
    (web_dir / "password.hash").write_text(
        hash_password(password),
        encoding="utf-8",
    )


class _WebForkHarness:
    """装配真实 Web/ThreadManager/SessionEngine/FileSession 测试链。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.home = tmp_path / ".kongming"
        self.password = "pwd"
        _write_model_catalog(self.home)
        _seed_password(self.home, self.password)
        self.config = Config.model_validate(
            {
                "model": {"preset_id": "preset-a"},
                "session": {
                    "backend": "file",
                    "file_store_path": str(self.home / "sessions"),
                },
                "web": {
                    "enabled": True,
                    "dev_mode": True,
                    "idle_timeout_seconds": 60,
                    "idle_check_interval_seconds": 10,
                },
                "scheduler": {"enabled": False},
            }
        )
        self.session_store = self.home / "sessions"
        self.asset_storage = AssetStorage(base_dir=self.home / "uploads")
        self.catalog_manager = ModelCatalogManager(
            user_path=self.home / "model-providers.yaml",
        )
        self.runtimes: dict[str, _PublicOnlyRuntime] = {}
        self.fail_next_session_at: int | None = None
        self.manager = ThreadManager(
            self.config,
            kongming_home=self.home,
            runtime_factory=self.runtime_factory,
            asset_storage=self.asset_storage,
        )
        self.app = create_app(
            self.config,
            self.manager,
            home_dir=self.home,
            asset_storage=self.asset_storage,
        )

    def _bootstrap(self) -> SessionBootstrap:
        """构造所有真实 FileSession 共用的稳定测试快照。"""
        return SessionBootstrap(
            agent_name="root",
            model_name="fake",
            instruction_sources=["test"],
            instruction_text_hash="fork-smoke",
            instruction_text="fork smoke",
            created_at=time.time(),
            cwd=str(self.tmp_path),
        )

    async def runtime_factory(
        self,
        thread_id: str,
        preset_id: str,
        adapter: WebHostAdapter,
        event_sinks: list[Any],
    ) -> tuple[Any, HostDispatcher]:
        """装配真实引擎；可让下一目标 FileSession 在第 N 次 append 失败。"""
        del preset_id, adapter
        fail_at = self.fail_next_session_at
        self.fail_next_session_at = None

        def session_factory(session_id: str) -> FileSession:
            """为当前 thread 构造正常或失败注入的真实 FileSession。"""
            if fail_at is not None:
                return _FailingSeedFileSession(
                    session_id,
                    self._bootstrap(),
                    str(self.session_store),
                    fail_at=fail_at,
                )
            return FileSession(
                session_id,
                self._bootstrap(),
                str(self.session_store),
            )

        engine = SessionEngine.build(
            self.config,
            session_factory=session_factory,
            llm_provider=_StubLLM(),
            model_catalog_manager=self.catalog_manager,
            event_sinks=event_sinks,
        )
        runtime = _PublicOnlyRuntime(engine)
        self.runtimes[thread_id] = runtime
        return runtime, HostDispatcher(runtime=runtime, session_id=thread_id)

    def login(self, client: TestClient) -> None:
        """走真实认证与 CSRF 中间件建立测试会话。"""
        response = client.post(
            "/api/auth/login",
            json={"password": self.password},
            headers=CSRF_HEADERS,
        )
        assert response.status_code == 200


def test_rest_fork_persists_full_history_and_independent_asset(tmp_path: Path) -> None:
    """首发、WS history 与 fork 只走公开门户，目标可独立恢复。"""
    harness = _WebForkHarness(tmp_path)

    with TestClient(harness.app) as client:
        harness.login(client)
        created = client.post(
            "/api/threads/generic/first-message",
            json={
                "text": "initial request",
                "preset_id": "preset-a",
                "cwd": str(tmp_path),
            },
            headers=CSRF_HEADERS,
        )
        assert created.status_code == 200
        source_id = str(created.json()["thread"]["id"])
        assert client.portal is not None

        async def wait_for_first_run() -> list[Message]:
            """等待首发后台 run 收口，再从真实引擎读取持久历史。"""
            cell = await harness.manager.boot_or_attach(source_id)
            task = cell.current_run_task
            if task is not None:
                await task
            return await harness.runtimes[source_id].engine_for_test.read_session_history(source_id)

        first_turn = client.portal.call(wait_for_first_run)
        assert first_turn == [
            Message.user("initial request"),
            Message.assistant("ok"),
        ]
        extra_messages = [
            Message.user(
                "inspect",
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
            Message.tool_result("call-1", "file body", name="read_file"),
            Message.assistant("done", metadata={"provider_response_id": "resp-1"}),
        ]
        messages = [*first_turn, *extra_messages]

        async def seed_source_history() -> None:
            """经公开 append 门户补入含工具与附件的第二个闭合 turn。"""
            for index, message in enumerate(extra_messages):
                usage = (
                    ProviderUsageManager().normalize(
                        family=ProviderUsageFamily.OPENAI_RESPONSES,
                        raw_usage={
                            "input_tokens": 37,
                            "input_tokens_details": {"cached_tokens": 5},
                            "output_tokens": 11,
                            "output_tokens_details": {"reasoning_tokens": 3},
                            "total_tokens": 48,
                        },
                    )
                    if index == len(extra_messages) - 1
                    else None
                )
                await harness.runtimes[source_id].append_session_message(
                    source_id,
                    message,
                    usage=usage,
                )

        client.portal.call(seed_source_history)
        asset_id = "a" * 32
        harness.asset_storage.write_asset(
            asset_id=asset_id,
            thread_id=source_id,
            kind="image",
            ext=".png",
            payload=b"png",
            metadata={
                "asset_id": asset_id,
                "thread_id": source_id,
                "kind": "image",
                "mime_type": "image/png",
                "storage_path": f"images/{source_id}/{asset_id}.png",
            },
        )

        with client.websocket_connect(f"/ws/threads/{source_id}") as websocket:
            history_frame = websocket.receive_json()
        assert history_frame["frame_type"] == "thread.history"
        history_messages = history_frame["messages"]
        assert [item["frame_type"] for item in history_messages] == [
            "text",
            "text",
            "text",
            "tool_use",
            "tool_result",
            "text",
        ]
        assert [
            item.get("content")
            for item in history_messages
            if item["frame_type"] in {"text", "tool_result"}
        ] == [
            "initial request",
            "ok",
            "inspect",
            "file body",
            "done",
        ]

        forked = client.post(
            f"/api/threads/{source_id}/fork",
            headers=CSRF_HEADERS,
        )
        assert forked.status_code == 201
        forked_payload = forked.json()
        forked_id = str(forked_payload["id"])
        assert forked_payload["forked_from_id"] == source_id
        assert forked_payload["message_count"] == len(messages)
        source_usage = client.portal.call(
            harness.manager.usage_manager.get_thread_usage,
            source_id,
        )
        target_usage = client.portal.call(
            harness.manager.usage_manager.get_thread_usage,
            forked_id,
        )
        assert source_usage is not None
        assert source_usage.provider == "openai"
        assert source_usage.last.input_tokens == 37
        assert source_usage.last.total_tokens == 48
        assert target_usage is None

        async def recover_forked_history() -> list[Message]:
            """创建新的 FileSession 实例，模拟进程恢复后读取目标历史。"""
            bootstrap = SessionBootstrap(
                agent_name="root",
                model_name="fake",
                instruction_sources=["test"],
                instruction_text_hash="fork-smoke",
                instruction_text="fork smoke",
                created_at=time.time(),
                cwd=str(tmp_path),
            )
            recovered = FileSession(
                forked_id,
                bootstrap,
                str(harness.session_store),
            )
            return await recovered.history()

        assert client.portal.call(recover_forked_history) == messages

        async def continue_only_in_fork() -> tuple[list[Message], list[Message]]:
            """向恢复后的目标 Session 追加消息，并同时读取源/目标持久历史。"""
            bootstrap = SessionBootstrap(
                agent_name="root",
                model_name="fake",
                instruction_sources=["test"],
                instruction_text_hash="fork-smoke",
                instruction_text="fork smoke",
                created_at=time.time(),
                cwd=str(tmp_path),
            )
            target_session = FileSession(
                forked_id,
                bootstrap,
                str(harness.session_store),
            )
            source_session = FileSession(
                source_id,
                bootstrap,
                str(harness.session_store),
            )
            await target_session.append(Message.user("branch-only"))
            return await target_session.history(), await source_session.history()

        target_history, unchanged_source_history = client.portal.call(continue_only_in_fork)
        assert target_history == [*messages, Message.user("branch-only")]
        assert unchanged_source_history == messages
        deleted = client.delete(
            f"/api/threads/{source_id}",
            headers=CSRF_HEADERS,
        )
        assert deleted.status_code == 204
        served_asset = client.get(f"/api/uploads/{asset_id}")
        assert served_asset.status_code == 200
        assert served_asset.content == b"png"
        assert harness.runtimes[source_id].append_calls
        assert harness.runtimes[source_id].history_calls.count(source_id) >= 2
        assert harness.runtimes[forked_id].seed_calls == [forked_id]
        assert all(runtime.private_accesses == [] for runtime in harness.runtimes.values())


def test_rest_fork_file_seed_failure_rolls_back_every_target_resource(
    tmp_path: Path,
) -> None:
    """REST fork 经真实 FileSession 部分写入失败后清理全部目标资源。"""
    harness = _WebForkHarness(tmp_path)

    with TestClient(harness.app) as client:
        harness.login(client)
        created = client.post(
            "/api/threads",
            json={
                "name": "rollback source",
                "preset_id": "preset-a",
                "cwd": str(tmp_path),
            },
            headers=CSRF_HEADERS,
        )
        assert created.status_code == 201
        source_id = str(created.json()["id"])
        asset_id = "b" * 32
        source_history = [
            Message.user(
                "inspect rollback asset",
                metadata={
                    "attachments": [
                        {
                            "asset_id": asset_id,
                            "kind": "image",
                            "mime_type": "image/png",
                            "size_bytes": 3,
                            "preview_url": f"/api/uploads/{asset_id}",
                            "status": "ready",
                        }
                    ]
                },
            ),
            Message.assistant("closed response"),
        ]
        assert client.portal is not None

        async def seed_source() -> None:
            """经真实 Manager boot 和公开 seed 门户持久化源历史。"""
            await harness.manager.boot_or_attach(source_id)
            await harness.runtimes[source_id].seed_empty_session_history(
                source_id,
                source_history,
            )

        client.portal.call(seed_source)
        harness.asset_storage.write_asset(
            asset_id=asset_id,
            thread_id=source_id,
            kind="image",
            ext=".png",
            payload=b"png",
            metadata={
                "asset_id": asset_id,
                "thread_id": source_id,
                "kind": "image",
                "mime_type": "image/png",
                "storage_path": f"images/{source_id}/{asset_id}.png",
            },
        )
        harness.fail_next_session_at = 2

        with pytest.raises(
            OSError,
            match="injected REST fork FileSession failure",
        ):
            client.post(
                f"/api/threads/{source_id}/fork",
                headers=CSRF_HEADERS,
            )

        target_ids = [thread_id for thread_id in harness.runtimes if thread_id != source_id]
        assert len(target_ids) == 1
        target_id = target_ids[0]
        assert [item.id for item in list_thread_metadata(harness.home)] == [source_id]
        assert (harness.session_store / target_id).exists() is False
        assert (harness.asset_storage.base_dir / "images" / target_id).exists() is False
        source_after_failure = client.portal.call(
            harness.runtimes[source_id].engine_for_test.read_session_history,
            source_id,
        )
        assert source_after_failure == source_history
        assert harness.runtimes[target_id].seed_calls == [target_id]
        assert harness.runtimes[target_id].clear_calls == [target_id]
        assert all(runtime.private_accesses == [] for runtime in harness.runtimes.values())
