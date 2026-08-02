"""完整对话 fork 浏览器 E2E 专用后端。

功能：在隔离临时目录装配真实 FastAPI、ThreadManager、FileSession、
metadata store 与 AssetStorage，并预置一条含工具调用和图片附件的源对话。

关键流程：
1. ``_seed_source`` 写入源 metadata、FileSession 历史和附件闭包。
2. ``_runtime_factory`` 只替换外部 LLM，内部 Session 与 HostDispatcher 走真实实现。
3. ``_RecoveringForkThreadManager.fork_thread`` 在提交成功后回收目标 cell，
   让浏览器首次打开目标时从新的 FileSession 实例恢复历史。
4. ``main`` 在 127.0.0.1:8080 启动 uvicorn，供 Playwright 的 Vite 代理访问。
"""

from __future__ import annotations

import asyncio
import base64
import tempfile
import time
from pathlib import Path
from typing import Any

import uvicorn
from typing_extensions import override

from core.agent_spec import AgentSpec
from core.contracts import ProviderUsageSnapshot
from core.message import Message, ToolCall
from core.result import Result
from hosts.shared.host_dispatcher import HostDispatcher
from hosts.web.app import create_app
from hosts.web.app_support.host_adapter import WebHostAdapter
from hosts.web.auth.secrets import hash_password
from hosts.web.threads.manager import ThreadManager
from hosts.web.threads.metadata import ThreadMetadata, write_thread_metadata
from hosts.web.uploads.storage import AssetStorage
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.models import Config
from sessions.file_session import FileSession
from sessions.session_bootstrap import SessionBootstrap

SOURCE_THREAD_ID = "thread-aaaaaaaaaaaa"
SOURCE_THREAD_NAME = "Fork E2E Source"
ASSET_ID = "b" * 32
PASSWORD = "fork-e2e-pwd"


class _FileRuntime:
    """提供真实 FileSession 与隔离 LLM 边界的最小 runtime。"""

    def __init__(self, store_path: Path, workspace: Path) -> None:
        """保存持久化坐标并准备 HostDispatcher 所需的 agent spec。"""
        self._store_path = store_path
        self._workspace = workspace
        self._sessions: dict[str, FileSession] = {}
        self._spec = AgentSpec(
            name="root",
            instructions="fork browser e2e",
            default_model="fake",
        )
        self._session_factory = self._new_session

    @property
    def agent_spec(self) -> AgentSpec:
        """返回 HostDispatcher 装配使用的 root spec。"""
        return self._spec

    def _new_session(self, session_id: str) -> FileSession:
        """创建可从磁盘恢复的独立 FileSession 实例。"""
        return FileSession(
            session_id,
            _session_bootstrap(self._workspace),
            str(self._store_path),
        )

    def get_or_create_session(self, session_id: str) -> FileSession:
        """按 session_id 创建或复用 runtime 内的 FileSession。"""
        session = self._sessions.get(session_id)
        if session is None:
            session = self._new_session(session_id)
            self._sessions[session_id] = session
        return session

    async def read_session_history(self, session_id: str) -> list[Message]:
        """通过公开 runtime 门户读取真实 FileSession 历史。"""
        return await self.get_or_create_session(session_id).history()

    async def append_session_message(
        self,
        session_id: str,
        message: Message,
        *,
        usage: ProviderUsageSnapshot | None = None,
    ) -> None:
        """通过公开 runtime 门户追加结构化消息。"""
        await self.get_or_create_session(session_id).append(message, usage=usage)

    async def seed_empty_session_history(
        self,
        session_id: str,
        messages: list[Message],
    ) -> None:
        """原子播种空 FileSession，失败时清空部分前缀。"""
        session = self.get_or_create_session(session_id)
        if await session.history():
            raise ValueError("target session history must be empty")
        try:
            for message in messages:
                await session.append(message)
        except BaseException:
            await session.clear()
            raise

    async def clear_session_history(self, session_id: str) -> None:
        """通过公开 runtime 门户清空真实 FileSession。"""
        await self.get_or_create_session(session_id).clear()

    async def run(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        reasoning_effort: str | None = None,
        event_context: dict[str, Any] | None = None,
        agent_id: str = "",
    ) -> Result:
        """返回固定完成结果；浏览器 E2E 不触发外部模型服务。"""
        del user_input, reasoning_effort, event_context, agent_id
        return Result(
            run_id="fork-browser-e2e-run",
            session_id=session_id or "",
            status="completed",
            final_message=Message.assistant("fake"),
            turn_count=1,
        )

    def steer(self, session_id: str, text: str) -> bool:
        """声明测试 runtime 当前没有可插入的活跃 run。"""
        del session_id, text
        return False

    async def aclose(self) -> None:
        """关闭 runtime；FileSession 每次 append 已同步完成落盘。"""


class _RecoveringForkThreadManager(ThreadManager):
    """fork 成功后回收目标 cell，强制后续浏览器连接走持久化恢复。"""

    @override
    async def fork_thread(
        self,
        source_thread_id: str,
        *,
        history_index: int | None = None,
    ) -> ThreadMetadata:
        """提交回复级 fork，随后关闭目标 runtime 并保留持久化历史。"""
        target = await super().fork_thread(
            source_thread_id,
            history_index=history_index,
        )
        await self.evict_cell(
            target.id,
            reason="manual_stop",
            notify_ws=False,
        )
        return target


def _session_bootstrap(workspace: Path) -> SessionBootstrap:
    """构造 FileSession 稳定启动快照。"""
    return SessionBootstrap(
        agent_name="root",
        model_name="fake",
        instruction_sources=["fork-browser-e2e"],
        instruction_text_hash="fork-browser-e2e",
        instruction_text="fork browser e2e",
        created_at=time.time(),
        cwd=str(workspace),
    )


def _write_model_catalog(home: Path) -> None:
    """写入本地 fake preset，使 generic_chat metadata 通过模型目录校验。"""
    (home / "model-providers.yaml").write_text(
        """\
version: 2
providers:
  - provider_id: fork-e2e
    default_preset_id: preset-a
    display_name: Fork E2E
    region_label: Local
    description: browser fork test
    logo_text: F
    protocol: openai
    default_base_url: http://127.0.0.1:9/v1
    request_defaults: {}
    models:
      - preset_id: preset-a
        display_name: Preset A
        model: fake
""",
        encoding="utf-8",
    )


async def _seed_source(
    *,
    home: Path,
    session_store: Path,
    asset_storage: AssetStorage,
    workspace: Path,
) -> None:
    """预置源 metadata、完整结构化历史和一份可独立复制的 PNG 资产。"""
    now = time.time()
    source = ThreadMetadata(
        id=SOURCE_THREAD_ID,
        name=SOURCE_THREAD_NAME,
        preset_id="preset-a",
        backend_kind="generic_chat",
        cwd=str(workspace),
        created_at=now,
        updated_at=now,
        message_count=4,
    )
    write_thread_metadata(home, source)
    session = FileSession(
        SOURCE_THREAD_ID,
        _session_bootstrap(workspace),
        str(session_store),
    )
    messages = [
        Message.user(
            "fork browser source marker",
            metadata={
                "attachments": [
                    {
                        "asset_id": ASSET_ID,
                        "kind": "image",
                        "mime_type": "image/png",
                        "size_bytes": 68,
                        "width": 1,
                        "height": 1,
                        "duration_ms": None,
                        "preview_url": f"/api/uploads/{ASSET_ID}",
                        "status": "ready",
                    }
                ]
            },
        ),
        Message.assistant(
            None,
            tool_calls=[
                ToolCall(
                    call_id="fork-e2e-call",
                    tool_name="read_file",
                    arguments={"path": "fork-e2e.txt"},
                )
            ],
            metadata={"reasoning_content": "fork e2e reasoning"},
        ),
        Message.tool_result(
            "fork-e2e-call",
            "fork e2e tool output",
            name="read_file",
            metadata={"ok": True},
        ),
        Message.assistant("fork browser final marker"),
    ]
    for message in messages:
        await session.append(message)

    png_payload = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNg"
        "YAAAAAMAASsJTYQAAAAASUVORK5CYII="
    )
    asset_storage.write_asset(
        asset_id=ASSET_ID,
        thread_id=SOURCE_THREAD_ID,
        kind="image",
        ext=".png",
        payload=png_payload,
        metadata={
            "asset_id": ASSET_ID,
            "thread_id": SOURCE_THREAD_ID,
            "kind": "image",
            "mime_type": "image/png",
            "size_bytes": len(png_payload),
            "storage_path": f"images/{SOURCE_THREAD_ID}/{ASSET_ID}.png",
            "preview_url": f"/api/uploads/{ASSET_ID}",
            "status": "ready",
        },
    )


def _config() -> Config:
    """构造关闭 scheduler、启用 Web dev mode 的隔离配置。"""
    return Config.model_validate(
        {
            "model": {"preset_id": "preset-a"},
            "session": {
                "backend": "file",
                "file_store_path": ".kongming/sessions",
            },
            "web": {
                "enabled": True,
                "dev_mode": True,
                "host": "127.0.0.1",
                "port": 8080,
                "idle_timeout_seconds": 60,
                "idle_check_interval_seconds": 10,
            },
            "scheduler": {"enabled": False},
        }
    )


def main() -> None:
    """装配隔离应用并启动 Playwright 可访问的 uvicorn 服务。"""
    temporary_home = tempfile.TemporaryDirectory(prefix="kongming-fork-e2e-")
    workspace = Path(temporary_home.name)
    home = workspace / ".kongming"
    home.mkdir(parents=True, exist_ok=True)
    _write_model_catalog(home)
    web_dir = home / "web"
    web_dir.mkdir(parents=True, exist_ok=True)
    (web_dir / "password.hash").write_text(
        hash_password(PASSWORD),
        encoding="utf-8",
    )
    session_store = home / "sessions"
    asset_storage = AssetStorage(base_dir=web_dir / "uploads")
    asyncio.run(
        _seed_source(
            home=home,
            session_store=session_store,
            asset_storage=asset_storage,
            workspace=workspace,
        )
    )
    config = _config()
    model_catalog_manager = ModelCatalogManager(user_path=home / "model-providers.yaml")

    async def _runtime_factory(
        thread_id: str,
        preset_id: str,
        adapter: WebHostAdapter,
        event_sinks: list[Any],
    ) -> tuple[Any, Any]:
        """装配真实 FileSession runtime，并把外部模型边界固定为 fake。"""
        del preset_id, adapter, event_sinks
        runtime = _FileRuntime(session_store, workspace)
        return runtime, HostDispatcher(runtime=runtime, session_id=thread_id)  # type: ignore[arg-type]

    manager = _RecoveringForkThreadManager(
        config,
        kongming_home=home,
        runtime_factory=_runtime_factory,
        asset_storage=asset_storage,
        model_catalog_manager=model_catalog_manager,
    )
    app = create_app(
        config,
        manager,
        home_dir=home,
        asset_storage=asset_storage,
        model_catalog_manager=model_catalog_manager,
        lifespan_shutdown_timeout=1.0,
    )
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning")


if __name__ == "__main__":
    main()
