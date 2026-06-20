"""Thread fleet 单例管理器。

:class:`ThreadManager` 持有 ``dict[thread_id, ThreadCell]``，是 v0.1.5 web
后端的 backbone：FastAPI 路由 / WS endpoint / REST endpoint 通过它拿到
"已 boot 的 cell"或"未 boot 的 metadata 列表"，再走 SessionBridge.run_once。

设计要点：

- **runtime_factory 注入**：本类不直接耦合 :meth:`NativeRuntime.build` 的
  全部参数。装配方（web-app-shell 任务的 startup hook）传入一个
  ``async (thread_id, preset_id) -> NativeRuntime`` 的 factory，让本类专
  注于 fleet 生命周期管理。
- **per-thread asyncio.Lock**：boot_or_attach 对同 thread_id 的并发调用
  靠 ``cell.boot_lock`` 串行化。全局锁 ``self._lock`` 只在读写 self.cells
  dict 时短持，**不**跨 boot 过程持有。
- **后台 idle eviction**：``_idle_eviction_loop`` 是 ``start()`` 启动的
  ``asyncio.Task``；shutdown 时 cancel。
- **aclose_all 不发 WS**：服务端 shutdown 时连接已断 / 即将断；省掉一次
  send 失败日志。

依赖方向：

- runtime_factory 由调用方注入，本类不直接 import provider 细节
- evict 路径推 ``CellEvictedFrame`` 直接走 cell.adapter._ws（内部接口），
  绕开 EventSink，因为 cell.evicted 不在 EventSink 协议覆盖范围内
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from core.contracts import ApprovalAction
from core.message import Message
from hosts.web.app_support.host_adapter import WebHostAdapter
from hosts.web.app_support.path_utils import is_absolute_workspace_path
from hosts.web.integrations.claude_code.jsonl_history import jsonl_path_for
from hosts.web.integrations.codex.projects_scanner import list_codex_projects
from hosts.web.protocol import CellEvictedFrame, CellSummaryDTO, EvictReason
from hosts.web.threads.cell import ThreadCell
from hosts.web.threads.errors import ThreadPresetRefreshError
from hosts.web.threads.metadata import (
    ThreadMetadata,
    delete_thread_metadata_dir,
    list_thread_metadata,
    read_thread_metadata,
    thread_metadata_path,
    write_thread_metadata,
)
from hosts.web.uploads.storage import AssetStorage
from hosts.web.usage.usage_token_v2 import (
    ClaudeJsonlLocator,
    CodexRolloutLocator,
    ProviderKind,
    ThreadMetadataReader,
    UsageTokenManager,
)
from hosts.web.websocket.event_sink import WSEventSink
from hosts.web.websocket.fanout import WebSocketFanout
from hosts.web.websocket.thread_status import ThreadStatusEventSink
from infrastructure.config.models import Config, LLMPresetConfig
from network.network_log import log_network_exception

logger = logging.getLogger(__name__)


class ClaudeThreadAlreadyBoundError(ValueError):
    """thread.claude_thread_id 已经非空，禁止覆盖（v0.2 invariant：写入后只读）。"""


class ClaudeThreadConflictError(ValueError):
    """claude_thread_id 已被另一 thread 绑定，禁止重复绑定（v0.2 invariant：1:1 绑定）。"""


class _ThreadMetadataReaderImpl:
    """``ThreadMetadataReader`` v2 Protocol 实现 —— 从 thread metadata.json 读
    轻量字段（backend_kind / cwd / claude_thread_id / codex_thread_id / preset_id）
    给 UsageTokenManager v2 派发派生器用。
    """

    def __init__(self, home: Path) -> None:
        self._home = home

    async def read(self, thread_id: str) -> dict[str, Any] | None:
        meta = await asyncio.to_thread(read_thread_metadata, self._home, thread_id)
        if meta is None:
            return None
        return {
            "backend_kind": meta.backend_kind,
            "cwd": meta.cwd,
            "claude_thread_id": meta.claude_thread_id,
            "codex_thread_id": meta.codex_thread_id,
            "preset_id": meta.preset_id,
        }


class _ClaudeJsonlLocatorImpl:
    """``ClaudeJsonlLocator`` v2 Protocol 实现 —— 拼 Claude SDK jsonl 路径。

    非 ``backend_kind="claude_code"`` 或缺 ``cwd`` / ``claude_thread_id`` → None。
    """

    def __init__(self, home: Path) -> None:
        self._home = home

    async def locate(self, thread_id: str) -> Path | None:
        meta = await asyncio.to_thread(read_thread_metadata, self._home, thread_id)
        if meta is None or meta.backend_kind != "claude_code":
            return None
        if not meta.cwd or not meta.claude_thread_id:
            return None
        return jsonl_path_for(meta.cwd, meta.claude_thread_id)


class _CodexRolloutLocatorImpl:
    """``CodexRolloutLocator`` v2 Protocol 实现 —— 扫 ~/.codex/sessions/ 找 rollout 路径。

    非 ``backend_kind="codex"`` 或缺 ``codex_thread_id`` → None。
    复用 ``web/codex/projects_scanner.py::list_codex_projects`` 的扫描结果，
    按 codex_thread_id 匹配 rollout uuid。
    """

    def __init__(self, home: Path) -> None:
        self._home = home

    async def locate(self, thread_id: str) -> Path | None:
        meta = await asyncio.to_thread(read_thread_metadata, self._home, thread_id)
        if meta is None or meta.backend_kind != "codex":
            return None
        codex_tid = meta.codex_thread_id
        if not codex_tid:
            return None
        # 扫 codex sessions 找匹配 uuid 的 rollout 文件
        try:
            projects = await asyncio.to_thread(list_codex_projects, registry_cwds=[])
        except Exception:
            logger.warning(
                "_CodexRolloutLocatorImpl.locate failed for %s", thread_id, exc_info=True
            )
            return None
        for proj in projects:
            for session in getattr(proj, "sessions", []):
                if getattr(session, "session_id", "") == codex_tid:
                    rollout_path = getattr(session, "rollout_path", None)
                    if isinstance(rollout_path, (str, Path)):
                        return Path(rollout_path)
        return None


class _GenericChatSessionLocatorImpl:
    """``GenericChatSessionLocator`` v2 Protocol 实现 —— 拼 FileSession messages.jsonl
    路径 + 按 preset_id 决定 provider 厂商。

    返回 None：
    - 非 ``backend_kind="generic_chat"``
    - session backend 不是 FileSession（memory / sqlite 不支持派生）
    - 找不到 preset_id 对应的 provider
    - thread 未跑过（messages.jsonl 未 materialize）
    """

    def __init__(self, home: Path, cfg: Config) -> None:
        self._home = home
        self._cfg = cfg
        # 建索引：preset_id → LLMPresetConfig（启动时一次，运行时只读）
        presets: dict[str, LLMPresetConfig] = {}
        web_cfg = getattr(cfg, "web", None)
        if web_cfg is not None:
            for preset in getattr(web_cfg, "llm_presets", []) or []:
                presets[preset.id] = preset
        self._presets = presets

    async def locate(self, thread_id: str) -> tuple[Path, ProviderKind] | None:
        meta = await asyncio.to_thread(read_thread_metadata, self._home, thread_id)
        if meta is None or meta.backend_kind != "generic_chat":
            return None
        # 检查 session backend 是否 file（D-1 决策：只支持 FileSession）
        session_cfg = getattr(self._cfg, "session", None)
        backend_kind = getattr(session_cfg, "backend", "file") if session_cfg else "file"
        if backend_kind != "file":
            # memory / sqlite backend → 不支持派生
            return None
        # 拼 FileSession messages.jsonl 路径
        jsonl_path = self._home / "sessions" / thread_id / "messages.jsonl"
        if not jsonl_path.is_file():
            return None
        # 查 preset_id → provider 厂商
        preset = self._presets.get(meta.preset_id)
        if preset is None:
            return None
        provider = preset.provider
        if provider not in ("anthropic", "openai_compatible"):
            return None
        return (jsonl_path, provider)


# Protocol 兼容性自检（启动时一次性，运行时零开销）
assert isinstance(_ThreadMetadataReaderImpl(Path("/tmp")), ThreadMetadataReader)
assert isinstance(_ClaudeJsonlLocatorImpl(Path("/tmp")), ClaudeJsonlLocator)
assert isinstance(_CodexRolloutLocatorImpl(Path("/tmp")), CodexRolloutLocator)
# _GenericChatSessionLocatorImpl 需要 Config，无法静态实例化校验；
# 运行时 ThreadManager.__init__ 装配时隐式校验


class CodexThreadAlreadyBoundError(ValueError):
    """thread.codex_thread_id 已经非空，禁止覆盖。"""


class CodexThreadConflictError(ValueError):
    """codex_thread_id 已被另一 thread 绑定，禁止重复绑定。"""


# runtime_factory 签名：根据 thread_id + preset_id + adapter + sinks 构造
# 完整 NativeRuntime + SessionBridge。装配方负责把 :class:`NativeRuntime.build`
# 的参数（cfg, event_sinks, instructions, ...）注入进 closure。
#
# 返回值用 tuple，避免本类需要知道 SessionBridge 怎么从 runtime / adapter 拼装。
RuntimeFactory = Callable[
    [str, str, WebHostAdapter, list[Any]],
    Awaitable[tuple[Any, Any]],  # (NativeRuntime, SessionBridge)
]


def _generate_thread_id() -> str:
    """生成 ``thread-<hex12>`` 格式 ID。

    使用 :mod:`secrets` 而非 ``uuid.uuid4()`` 因为 12 位 hex 已经够冲突避免
    （理论冲突概率 ~1/2^48），且 secrets 输出更短。
    """
    return f"thread-{secrets.token_hex(6)}"


def _now() -> float:
    return time.time()


def _title_from_first_message(text: str) -> str:
    normalized = " ".join(text.strip().split())
    return normalized[:40] or "新会话"


def _resolve_first_message_cwd(cwd: str) -> str:
    normalized = cwd.strip()
    if not normalized:
        return str(Path.home().resolve())
    if not is_absolute_workspace_path(normalized):
        raise ValueError("cwd must be an absolute path")
    if PurePosixPath(normalized).is_absolute():
        return str(Path(normalized).expanduser().resolve())
    return normalized


class ThreadManager:
    """单进程 thread fleet 管理器。

    Attributes:
        _cfg: 整体 :class:`Config`；本类只读 ``cfg.web.*`` 字段。
        _home: ``.kongming/`` 根目录（由调用方注入，便于测试 tmp_path）。
        _runtime_factory: 见 :data:`RuntimeFactory`。
        _cells: 当前活的 cell（dict[thread_id, ThreadCell]）。
        _lock: 全局短持锁，仅保护 self._cells dict 读写不竞争；不持锁等
            boot 完成。
        _idle_task: 后台 idle eviction task；start() 时启，aclose_all
            时 cancel。
        _started: start() 是否已被调用（一次性，不可二次启动）。
        _closed: aclose_all 是否已完成。
    """

    def __init__(
        self,
        cfg: Config,
        *,
        kongming_home: Path,
        runtime_factory: RuntimeFactory,
        asset_storage: AssetStorage | None = None,
    ) -> None:
        """
        Args:
            asset_storage: 可选注入的 :class:`AssetStorage`,用于 ``delete_thread``
                时同步清理 thread 名下所有上传资产(claude-image-paste-e2e P1 #2
                R2 boundary fix)。``None`` 时跳过资产清理(CLI / 测试路径常态)。
        """
        self._cfg = cfg
        self._home = kongming_home
        self._runtime_factory = runtime_factory
        self._asset_storage = asset_storage
        self._cells: dict[str, ThreadCell] = {}
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False
        # usage-token-v2-bigbang: UsageTokenManager v2 注入——无状态门面。
        # token 真源来自 SDK 写的 jsonl/rollout，由 manager 内部派生器现场算。
        # metadata.json 不再缓存 token（schema v9 已物理删 3 个 token 字段）。
        self._usage_manager: UsageTokenManager = UsageTokenManager(
            meta_reader=_ThreadMetadataReaderImpl(kongming_home),
            claude_locator=_ClaudeJsonlLocatorImpl(kongming_home),
            codex_locator=_CodexRolloutLocatorImpl(kongming_home),
            generic_locator=_GenericChatSessionLocatorImpl(kongming_home, cfg),
        )

    @property
    def usage_manager(self) -> UsageTokenManager:
        """v2 manager: 暴露给 router / ws handler / service 调
        ``get_thread_usage(thread_id)`` 唯一公共方法。

        外部消费方**只能**调这一个方法；v1 时代的 record_run_usage /
        set_last_assistant_usage / get_thread_summary 等方法 v2 全部删除。
        """
        return self._usage_manager

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """uvicorn startup 时调用。

        操作：
        1. （扫盘逻辑放到首次 ``list_threads`` / ``boot_or_attach`` 时按需读取，
            ``start`` 这里仅校验目录可访问，避免阻塞 startup。）
        2. 启动 ``_idle_eviction_loop`` 后台 task。

        幂等：重复调用直接返回。
        """
        if self._started:
            return
        self._started = True
        # 触发一次预热扫盘，确保目录可访问；扫盘结果不缓存（list_threads 每次重读）
        await asyncio.to_thread(list_thread_metadata, self._home)
        # 启动 idle eviction
        self._idle_task = asyncio.create_task(
            self._idle_eviction_loop(),
            name="thread-manager-idle-eviction",
        )

    async def aclose_all(self) -> None:
        """uvicorn shutdown 钩子。

        - cancel idle task
        - 对所有活的 cell 走 ``evict_cell(notify_ws=False, reason=server_shutdown)``
        - 用 ``asyncio.gather + return_exceptions=True`` 避免单个 cell 卡
          住整体 shutdown
        - 5s 超时仍在跑的 ``current_run_task`` 直接 cancel

        幂等：重复调用直接返回。
        """
        if self._closed:
            return
        self._closed = True
        # 1. cancel idle loop
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._idle_task
            self._idle_task = None

        # 2. 收集当前所有 thread_id（避免迭代时改 dict）
        async with self._lock:
            thread_ids = list(self._cells.keys())

        if not thread_ids:
            return

        # 3. 并发 evict（不发 WS，连接已断）
        await asyncio.gather(
            *(
                self.evict_cell(
                    tid,
                    reason="server_shutdown",
                    notify_ws=False,
                )
                for tid in thread_ids
            ),
            return_exceptions=True,
        )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create_thread(
        self,
        name: str,
        preset_id: str = "",
        *,
        backend_kind: Literal["generic_chat", "claude_code", "codex"] = "generic_chat",
        cwd: str = "",
    ) -> ThreadMetadata:
        """创建一个新 thread 的 metadata（不 boot cell）。

        浏览器先 ``POST /api/threads`` 创建，再单独建 WS 触发 boot。

        Args:
            name: thread 名（可选；为空时用 thread_id 兜底）。
            preset_id: ``backend_kind="generic_chat"`` 时必须非空，
                ``backend_kind="claude_code"`` / ``"codex"`` 时允许空字符串占位。
            backend_kind: 后端类型；决定后续 WS endpoint 走通用 chat、Claude Code
                或 Codex 通道。
            cwd: 可选 workspace 根目录；为空表示纯聊天 thread。
        """
        if backend_kind == "generic_chat" and (not preset_id or not preset_id.strip()):
            raise ValueError("preset_id required for generic_chat backend")
        return await self._create_thread_metadata(
            name=name,
            preset_id=preset_id,
            backend_kind=backend_kind,
            thread_kind="chat",
            source_kind="",
            source_id="",
            cwd=cwd,
        )

    async def create_scheduled_task_thread(
        self,
        *,
        task_id: str,
        name: str,
        preset_id: str,
        cwd: str = "",
    ) -> str:
        """创建定时任务专属 generic_chat thread 并返回 thread id。"""
        normalized_task_id = task_id.strip()
        if not normalized_task_id:
            raise ValueError("task_id must not be empty")
        if not preset_id or not preset_id.strip():
            raise ValueError("preset_id required for scheduled task thread")
        meta = await self._create_thread_metadata(
            name=name or normalized_task_id,
            preset_id=preset_id,
            backend_kind="generic_chat",
            thread_kind="scheduled_task",
            source_kind="scheduled_task",
            source_id=normalized_task_id,
            cwd=cwd,
        )
        return meta.id

    async def _create_thread_metadata(
        self,
        *,
        name: str,
        preset_id: str,
        backend_kind: Literal["generic_chat", "claude_code", "codex"],
        thread_kind: Literal["chat", "scheduled_task"],
        source_kind: str,
        source_id: str,
        cwd: str,
    ) -> ThreadMetadata:
        """内部创建 thread metadata；允许 scheduled_task 门户传入业务类型字段。"""
        if backend_kind == "generic_chat" and (not preset_id or not preset_id.strip()):
            raise ValueError("preset_id required for generic_chat backend")
        normalized_cwd = cwd.strip()
        normalized_source_kind = source_kind.strip()
        normalized_source_id = source_id.strip()
        thread_id = _generate_thread_id()
        resolved_name = name.strip() or thread_id
        now = time.time()
        meta = ThreadMetadata(
            id=thread_id,
            name=resolved_name,
            preset_id=preset_id,
            backend_kind=backend_kind,
            thread_kind=thread_kind,
            source_kind=normalized_source_kind,
            source_id=normalized_source_id,
            cwd=normalized_cwd,
            created_at=now,
            updated_at=now,
            message_count=0,
        )
        await asyncio.to_thread(write_thread_metadata, self._home, meta)
        return meta

    async def create_generic_thread_from_first_message(
        self,
        *,
        text: str,
        preset_id: str,
        cwd: str = "",
        reasoning_effort: Literal["low", "medium", "high"] | None = None,
    ) -> ThreadMetadata:
        """创建通用频道 thread，并在返回前持久化第一条 user message。

        首发接口的成功边界是 metadata 与 user message 都已落盘；任一步失败
        都删除 metadata，避免左侧列表出现空 thread。落盘成功后立即排队
        后续 assistant run，但不等待 LLM 完整回复。
        """
        normalized_text = text.strip()
        if not normalized_text:
            raise ValueError("text must not be empty")
        normalized_preset_id = preset_id.strip()
        if not normalized_preset_id:
            raise ValueError("preset_id must not be empty")
        preset_ids = {preset.id for preset in getattr(self._cfg.web, "llm_presets", [])}
        if normalized_preset_id not in preset_ids:
            raise ValueError(f"unknown preset_id: {normalized_preset_id!r}")
        resolved_cwd = _resolve_first_message_cwd(cwd)

        meta = await self.create_thread(
            _title_from_first_message(normalized_text),
            normalized_preset_id,
            backend_kind="generic_chat",
            cwd=resolved_cwd,
        )
        try:
            cell = await self.boot_or_attach(meta.id)
            session = self._session_for_first_message(cell)
            await session.append(Message.user(normalized_text))
            updated = meta.model_copy(
                update={
                    "message_count": 1,
                    "updated_at": _now(),
                    "schema_version": meta.schema_version,
                }
            )
            await asyncio.to_thread(write_thread_metadata, self._home, updated)
            async with self._lock:
                live_cell = self._cells.get(meta.id)
                if live_cell is not None:
                    live_cell.metadata = updated
            self._start_first_message_run(cell, reasoning_effort=reasoning_effort)
            return updated
        except Exception:
            with suppress(Exception):
                await self.delete_thread(meta.id, keep_history=False)
            raise

    def _start_first_message_run(
        self,
        cell: ThreadCell,
        *,
        reasoning_effort: Literal["low", "medium", "high"] | None,
    ) -> None:
        continue_run = getattr(cell.bridge, "continue_from_last_user_message", None)
        if not callable(continue_run):
            raise RuntimeError("bridge does not expose continue_from_last_user_message")

        task = asyncio.create_task(
            continue_run(reasoning_effort=reasoning_effort),
            name=f"web-first-message-run-{cell.thread_id}",
        )
        cell.current_run_task = task

        def _clear_first_message_task(
            t: asyncio.Task[Any],
            *,
            _cell: ThreadCell = cell,
            _task: asyncio.Task[Any] = task,
        ) -> None:
            if getattr(_cell, "current_run_task", None) is _task:
                _cell.current_run_task = None
            try:
                t.result()
            except asyncio.CancelledError:
                logger.info(
                    "first message continuation cancelled for thread=%s",
                    _cell.thread_id,
                )
            except Exception as exc:
                logger.exception(
                    "first message continuation failed for thread=%s: %s",
                    _cell.thread_id,
                    exc,
                )

        task.add_done_callback(_clear_first_message_task)

    @staticmethod
    def _session_for_first_message(cell: ThreadCell) -> Any:
        runtime = cell.runtime
        get_or_create = getattr(runtime, "_get_or_create_session", None)
        if callable(get_or_create):
            return get_or_create(cell.thread_id)
        session_factory = getattr(runtime, "_session_factory", None)
        if callable(session_factory):
            session = session_factory(cell.thread_id)
            sessions = getattr(runtime, "_sessions", None)
            if isinstance(sessions, dict):
                sessions[cell.thread_id] = session
            return session
        raise RuntimeError("runtime does not expose a session factory")

    async def rename_thread(self, thread_id: str, new_name: str) -> ThreadMetadata:
        """重命名 thread；返回更新后的 metadata。

        失败：thread 不存在抛 :class:`KeyError`；name 空抛 :class:`ValueError`。
        如果 cell 已 boot，同步更新 cell.metadata 引用。
        """
        if not new_name or not new_name.strip():
            raise ValueError("new name must not be empty")

        meta = await asyncio.to_thread(read_thread_metadata, self._home, thread_id)
        if meta is None:
            raise KeyError(f"thread not found: {thread_id}")
        updated = ThreadMetadata(
            id=meta.id,
            name=new_name.strip(),
            preset_id=meta.preset_id,
            backend_kind=meta.backend_kind,
            claude_thread_id=meta.claude_thread_id,
            codex_thread_id=meta.codex_thread_id,
            cwd=meta.cwd,
            created_at=meta.created_at,
            updated_at=_now(),
            message_count=meta.message_count,
            is_pinned=meta.is_pinned,
            is_archived=meta.is_archived,
        )
        await asyncio.to_thread(write_thread_metadata, self._home, updated)
        async with self._lock:
            cell = self._cells.get(thread_id)
            if cell is not None:
                cell.metadata = updated
        return updated

    async def pin_thread(self, thread_id: str, is_pinned: bool) -> ThreadMetadata:
        """置顶/取消置顶 thread。"""
        meta = await asyncio.to_thread(read_thread_metadata, self._home, thread_id)
        if meta is None:
            raise KeyError(f"thread not found: {thread_id}")
        updated = ThreadMetadata(
            id=meta.id,
            name=meta.name,
            preset_id=meta.preset_id,
            backend_kind=meta.backend_kind,
            claude_thread_id=meta.claude_thread_id,
            codex_thread_id=meta.codex_thread_id,
            cwd=meta.cwd,
            created_at=meta.created_at,
            updated_at=meta.updated_at,  # pin 不改 updated_at
            message_count=meta.message_count,
            is_pinned=is_pinned,
            is_archived=meta.is_archived,
        )
        await asyncio.to_thread(write_thread_metadata, self._home, updated)
        async with self._lock:
            cell = self._cells.get(thread_id)
            if cell is not None:
                cell.metadata = updated
        return updated

    async def set_archived(self, thread_id: str, is_archived: bool) -> ThreadMetadata:
        """归档/取消归档 thread（v10 claude-session-rename-archive-metadata-source）。

        与 ``pin_thread`` 同款：read → model_copy → atomic write → 同步 cell.metadata。
        归档不算"活跃"，所以**不**更新 ``updated_at``（保持与 pin 一致）。

        失败：thread 不存在抛 :class:`KeyError`。
        """
        meta = await asyncio.to_thread(read_thread_metadata, self._home, thread_id)
        if meta is None:
            raise KeyError(f"thread not found: {thread_id}")
        updated = ThreadMetadata(
            id=meta.id,
            name=meta.name,
            preset_id=meta.preset_id,
            backend_kind=meta.backend_kind,
            claude_thread_id=meta.claude_thread_id,
            codex_thread_id=meta.codex_thread_id,
            cwd=meta.cwd,
            created_at=meta.created_at,
            updated_at=meta.updated_at,  # archive 不改 updated_at（与 pin 一致）
            message_count=meta.message_count,
            is_pinned=meta.is_pinned,
            is_archived=is_archived,
        )
        await asyncio.to_thread(write_thread_metadata, self._home, updated)
        async with self._lock:
            cell = self._cells.get(thread_id)
            if cell is not None:
                cell.metadata = updated
        return updated

    async def update_thread_preset(self, thread_id: str, preset_id: str) -> ThreadMetadata:
        """更新 Generic Chat thread 的 preset，并在可行时刷新活跃 runtime。

        当前 run 已经开始时不热切换 provider；metadata 先落盘，下一次发送前
        ``ensure_cell_runtime_preset_current`` 会重建 runtime。
        """
        normalized_preset_id = preset_id.strip()
        if not normalized_preset_id:
            raise ValueError("preset_id must not be empty")

        meta = await asyncio.to_thread(read_thread_metadata, self._home, thread_id)
        if meta is None:
            raise KeyError(f"thread not found: {thread_id}")
        if meta.backend_kind != "generic_chat":
            raise ValueError("preset can only be changed for generic_chat threads")

        updated = ThreadMetadata(
            id=meta.id,
            name=meta.name,
            preset_id=normalized_preset_id,
            backend_kind=meta.backend_kind,
            claude_thread_id=meta.claude_thread_id,
            codex_thread_id=meta.codex_thread_id,
            cwd=meta.cwd,
            created_at=meta.created_at,
            updated_at=_now(),
            message_count=meta.message_count,
            is_pinned=meta.is_pinned,
            is_archived=meta.is_archived,
        )
        await asyncio.to_thread(write_thread_metadata, self._home, updated)
        async with self._lock:
            cell = self._cells.get(thread_id)
            if cell is not None:
                cell.metadata = updated
        refreshed = await self.ensure_cell_runtime_preset_current(thread_id)
        if not refreshed:
            await asyncio.to_thread(write_thread_metadata, self._home, meta)
            async with self._lock:
                rollback_cell = self._cells.get(thread_id)
                if rollback_cell is not None:
                    rollback_cell.metadata = meta
            raise ThreadPresetRefreshError(
                f"failed to refresh runtime for preset_id: {normalized_preset_id}"
            )
        return updated

    async def ensure_cell_runtime_preset_current(self, thread_id: str) -> bool:
        """让已启动 cell 的 runtime preset 与 metadata 保持一致。"""
        async with self._lock:
            cell = self._cells.get(thread_id)
        if cell is None:
            return True
        async with cell.preset_refresh_lock:
            if cell.runtime_preset_id == cell.metadata.preset_id:
                return True
            run_task = cell.current_run_task
            if run_task is not None and not run_task.done():
                return True

            old_runtime = cell.runtime
            try:
                runtime, bridge = await self._runtime_factory(
                    cell.metadata.id,
                    cell.metadata.preset_id,
                    cell.adapter,
                    cell.event_sinks,
                )
            except Exception:
                logger.exception(
                    "failed to refresh thread runtime for preset switch: thread=%s preset=%s",
                    cell.metadata.id,
                    cell.metadata.preset_id,
                )
                return False
            cell.runtime = runtime
            cell.bridge = bridge
            cell.runtime_preset_id = cell.metadata.preset_id
            with suppress(Exception):
                await old_runtime.aclose()
            return True

    # task#3.3：``add_thread_usage`` 已删除——UsagePersistSink 改走
    # ``self._usage_manager.record_run_usage(channel, raw_payload, ...)``；
    # router / WS handler 通过 ``self.usage_manager`` 属性拿数据。
    # 历史调用方（包括测试 mock）应同步迁移到 manager API。

    async def delete_thread(
        self,
        thread_id: str,
        *,
        keep_history: bool = False,
    ) -> None:
        """删除 thread。

        默认操作（``keep_history=False``）：
        1. evict cell（如已 boot）
        2. 删 metadata 目录（``.kongming/web/threads/<thread_id>/``）
        3. 删 session backend 历史（v0.1.5 暂不实现 backend 清理；留 TODO）

        ``keep_history=True``：保留 session backend 历史，仅删 metadata，
        给"误删恢复"留口子。

        幂等：thread 不存在时直接返回（不抛）。
        """
        async with self._lock:
            cell_exists = thread_id in self._cells
        if cell_exists:
            await self.evict_cell(thread_id, reason="manual_stop", notify_ws=True)

        # 删 metadata 目录
        await asyncio.to_thread(
            delete_thread_metadata_dir,
            self._home,
            thread_id,
        )

        # P1 #2 (claude-image-paste-e2e R2 boundary fix)：清理 thread 名下所有
        # 上传资产(images / videos / files)。``_asset_storage`` 在 web 装配层
        # 注入(CLI / 测试路径常为 None → 跳过,因为这些路径压根不会有上传资产)。
        # delete_thread_assets 内部对不存在的目录直接跳过,容错 idempotent。
        if self._asset_storage is not None:
            try:
                removed = await asyncio.to_thread(
                    self._asset_storage.delete_thread_assets,
                    thread_id=thread_id,
                )
                if removed > 0:
                    logger.info(
                        "delete_thread(%s): removed %d asset files",
                        thread_id,
                        removed,
                    )
            except Exception:
                # 资产清理失败不阻断 thread 删除主流程
                logger.warning(
                    "delete_thread(%s): asset cleanup failed; metadata already removed",
                    thread_id,
                    exc_info=True,
                )

        # v0.1.5 不删 session backend 历史（需要 cfg.session.backend 路径推算）；
        # 留给 web-app-shell 任务在装配时按需 wire 一个 history_cleaner closure。
        # keep_history=True 时显式跳过；False 时也仅记 TODO。
        if not keep_history:
            logger.info(
                "delete_thread(%s): metadata removed; session history cleanup is "
                "deferred to web-app-shell task (v0.1.5 does not wire backend deletion)",
                thread_id,
            )

    async def boot_or_attach(self, thread_id: str) -> ThreadCell:
        """获取已存在的 cell 或懒启动一个。

        并发安全：per-thread :class:`asyncio.Lock`（``cell.boot_lock``）
        让同 thread_id 的并发调用串行；100 个 task 同时调，runtime_factory
        只被调一次，其余 99 个直接拿到第一次 build 的 cell。

        Raises:
            KeyError: thread 的 metadata 不存在（先 :meth:`create_thread` 再 boot）。
        """
        # 1. 快速路径：cell 已存在
        async with self._lock:
            existing = self._cells.get(thread_id)
        if existing is not None:
            existing.touch()
            return existing

        # 2. 慢路径：拿 metadata，准备懒启动
        meta = await asyncio.to_thread(read_thread_metadata, self._home, thread_id)
        if meta is None:
            raise KeyError(f"thread metadata not found: {thread_id}")

        # 3. 拿 / 创建 per-thread boot_lock
        # 用 self._lock 保护"创建占位 cell"的窗口期，避免两个 task 同时进入 lock 创建
        async with self._lock:
            existing = self._cells.get(thread_id)
            if existing is not None:
                existing.touch()
                return existing
            # 占位锁：先创建只含 boot_lock 的"半 cell"——但这样字段不全，会让
            # ThreadCell dataclass 校验失败。改用本地锁字典更干净。
            boot_lock = self._boot_locks.setdefault(thread_id, asyncio.Lock())

        # 4. 拿 boot_lock，串行化 boot 过程
        async with boot_lock:
            # double-check：等锁期间别人可能已经 build 完了
            async with self._lock:
                existing = self._cells.get(thread_id)
            if existing is not None:
                existing.touch()
                return existing

            # 5. 真正的 build
            cell = await self._build_cell(meta)

            # 6. 注册到 dict
            async with self._lock:
                self._cells[thread_id] = cell
            return cell

    # boot_locks 字典：thread_id → asyncio.Lock（只在 boot_or_attach 路径用）
    # 用 lazy init pattern：__init__ 时声明，第一次访问时 setdefault 创建
    @property
    def _boot_locks(self) -> dict[str, asyncio.Lock]:
        if not hasattr(self, "_boot_locks_storage"):
            self._boot_locks_storage: dict[str, asyncio.Lock] = {}
        return self._boot_locks_storage

    # claude_bind_locks 字典：claude_thread_id → asyncio.Lock
    # 只在 create_and_bind_claude_thread 路径用，串行化"反查→不存在则 create+bind"
    # 的临界区，避免两个 import_claude_session 请求同时进入"不存在"分支产生重复
    # thread metadata（"幽灵 thread"——曾在用户机器上发现 3 组 ctid 各挂 2-3 条
    # thread metadata，根因是该临界区非原子）。
    @property
    def _claude_bind_locks(self) -> dict[str, asyncio.Lock]:
        if not hasattr(self, "_claude_bind_locks_storage"):
            self._claude_bind_locks_storage: dict[str, asyncio.Lock] = {}
        return self._claude_bind_locks_storage

    async def create_and_bind_claude_thread(
        self,
        *,
        claude_thread_id: str,
        cwd: str,
        name: str,
        preset_id: str = "",
    ) -> tuple[ThreadMetadata, bool]:
        """原子地完成"反查 ctid → 不存在则 create_thread + bind_claude_thread"。

        替代老的两步非原子调用（import_claude_session router 旧实现），消除
        race window（参见 ``docs/fixes/claude-session-rename-archive-
        metadata-source.md`` 的根因调查）。

        实现：per-claude_thread_id :class:`asyncio.Lock` 串行化临界区。

        Returns:
            元组 ``(meta, imported)``：

            - ``imported=False``：``claude_thread_id`` 已绑过另一个 thread，
              返回 existing thread metadata。
            - ``imported=True``：新建了 thread + 完成 bind。

        Raises:
            ValueError: ``claude_thread_id`` 为空。
            ClaudeThreadConflictError: 极端 race（如 worktree 共享 .kongming
                并行写盘）下仍可能抛出；create_thread 阶段写入的 metadata 会被
                回滚（``delete_thread``），不留幽灵。
        """
        if not claude_thread_id:
            raise ValueError("claude_thread_id must not be empty")

        # 拿/创建 per-ctid lock；用 self._lock 保护字典 setdefault 防止并发
        # 创建两把不同 Lock 实例（那样就完全失去串行化效果了）
        async with self._lock:
            ctid_lock = self._claude_bind_locks.setdefault(claude_thread_id, asyncio.Lock())

        async with ctid_lock:
            # 临界区开始：double-check 反查（等锁期间可能有人已经绑好）
            existing = self.find_thread_by_claude_thread_id(claude_thread_id)
            if existing is not None:
                return existing, False

            # 未绑定 → create_thread + bind_claude_thread 原子完成
            new_thread = await self.create_thread(
                name,
                preset_id,
                backend_kind="claude_code",
                cwd=cwd,
            )
            try:
                bound = await self.bind_claude_thread(
                    new_thread.id,
                    claude_thread_id,
                    cwd,
                )
            except Exception:
                # bind 失败（如 ClaudeThreadConflictError）→ 回滚 create_thread
                # 避免留下未绑定的孤儿 thread metadata（"幽灵 thread"）
                with suppress(Exception):
                    await self.delete_thread(new_thread.id, keep_history=False)
                raise
            return bound, True

    async def _build_cell(self, meta: ThreadMetadata) -> ThreadCell:
        """装配单个 cell。

        实现：
        1. 创建 :class:`WebHostAdapter`（先用占位 ws=None；boot 后由 WS
           endpoint 调 cell.attach_ws 设真实连接）
        2. 创建 :class:`WSEventSink`（同上 ws=None）
        3. 调用 runtime_factory 拿到 (NativeRuntime, SessionBridge)
        4. 装配 ThreadCell

        注意：v0.1.5 cell 创建时 ws 还没建立 —— WS handshake 是在 cell 已
        boot 后异步发生的。WebHostAdapter 接受 ``ws=None`` 时所有 send
        会因 closed=True（初始 ws None 触发的隐式 closed）静默丢——但本设
        计选择**初始 closed=False, ws=None**，让首次 send 触发 AttributeError
        被 _safe_send_json 吞掉并标 closed；后续 attach_ws 重置 closed
        即可正常发。
        """
        fanout = WebSocketFanout()
        adapter = WebHostAdapter(
            ws=fanout,
            pending_approval_timeout_seconds=float(self._cfg.web.pending_approval_timeout_seconds),
            # 阶段 1 (smart-approval-manager-v0.5)：传 thread_id 让 close() 能调
            # ApprovalManager.cancel_by_thread 清 manager 路径的 pending（R10 防护）。
            thread_id=meta.id,
        )
        ws_sink = WSEventSink(fanout, thread_id=meta.id)
        # usage-token-v2-bigbang: UsagePersistSink 已删除——v2 manager 是无状态门面，
        # 不接受外部 push token。usage 事件无需持久化 sink；前端通过
        # GET /threads/<tid>/usage 端点拉取派生结果。

        status_sink = ThreadStatusEventSink(meta.id)
        sinks: list[Any] = [ws_sink, status_sink]
        runtime, bridge = await self._runtime_factory(
            meta.id,
            meta.preset_id,
            adapter,
            sinks,
        )

        cell = ThreadCell(
            thread_id=meta.id,
            metadata=meta,
            runtime=runtime,
            bridge=bridge,
            adapter=adapter,
            event_sinks=sinks,
            runtime_preset_id=meta.preset_id,
        )
        return cell

    # ------------------------------------------------------------------
    # Eviction
    # ------------------------------------------------------------------

    async def evict_cell(
        self,
        thread_id: str,
        reason: EvictReason,
        message: str | None = None,
        *,
        notify_ws: bool = True,
    ) -> None:
        """主动回收一个 cell。

        步骤：
        1. 标记 status = "evicting"（防止再 boot）
        2. 取 cell 出 dict（防止其他路径再用）
        3. 推 ``CellEvictedFrame``（``notify_ws=False`` 时跳过；shutdown 路径用）
        4. cancel ``current_run_task``（带 5s 超时；超时直接 cancel）
        5. 关 adapter（cancel pending approvals）
        6. 关 runtime（``runtime.aclose()`` 释放 httpx pool）

        幂等：thread_id 不在 dict 时直接返回；不抛。
        """
        async with self._lock:
            cell = self._cells.pop(thread_id, None)
            self._boot_locks.pop(thread_id, None)
        if cell is None:
            return

        cell.status = "evicting"

        # 1. 推 cell.evicted 帧（best-effort）
        if notify_ws:
            frame = CellEvictedFrame(
                thread_id=thread_id,
                reason=reason,
                message=message,
                timestamp_ms=int(_now() * 1000),
            )
            try:
                await cell.adapter._safe_send_json(frame.model_dump())
            except Exception as exc:
                log_network_exception(
                    "hosts.web.threads.manager",
                    "cell_evicted_notify_failed",
                    exc,
                    thread_id=thread_id,
                    reason=reason,
                )

        # 2. cancel current_run_task（5s 上限）
        run_task = cell.current_run_task
        if run_task is not None and not run_task.done():
            run_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(run_task, timeout=5.0)

        # 3. 关 adapter（resolve pending approvals 为 False）
        with suppress(Exception):
            await cell.adapter.close()

        # 4. 清理本 thread 的 session grants（修 bug-report-20260427-235232：
        # 长期运行 server 否则会累积旧 thread 的 grants）。CLI 路径不需要做
        # 这一步：进程退出时 GrantStore 跟着 GC 回收。
        grant_store = getattr(cell.runtime, "grant_store", None)
        if grant_store is not None:
            with suppress(Exception):
                grant_store.clear_session(cell.thread_id)

        # 5. 关 runtime
        with suppress(Exception):
            await cell.runtime.aclose()

    async def _idle_eviction_loop(self) -> None:
        """后台 task：周期扫描 cell 列表，命中空闲阈值的执行 evict。

        策略：
        - 每 ``cfg.web.idle_check_interval_seconds`` 秒扫一次
        - 命中条件：``now - cell.last_active_at > cfg.web.idle_timeout_seconds``
          且 ``not cell.has_pending_approvals``（有待审批不 evict，避免用户
          回来时发现 thread 没了）
        - 命中即 ``evict_cell(reason="idle", notify_ws=True)``
        """
        interval = max(1, int(self._cfg.web.idle_check_interval_seconds))
        threshold = float(self._cfg.web.idle_timeout_seconds)
        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

            now = _now()
            try:
                async with self._lock:
                    candidates = [
                        cell.thread_id
                        for cell in self._cells.values()
                        if (now - cell.last_active_at) > threshold
                        and not cell.has_pending_approvals
                    ]
                for tid in candidates:
                    with suppress(Exception):
                        await self.evict_cell(tid, reason="idle", notify_ws=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # idle loop 永不退出（除非 cancel）；记 warning 继续
                logger.warning("idle eviction loop iteration failed: %s", exc)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_thread(self, thread_id: str) -> ThreadMetadata | None:
        """按 id 查询单个 thread metadata；优先返回活跃 cell 的内存版本。"""
        cell = self._cells.get(thread_id)
        if cell is not None:
            return cell.metadata
        return read_thread_metadata(self._home, thread_id)

    def list_threads(self) -> list[ThreadMetadata]:
        """REST ``GET /api/threads`` 数据源；含未 boot 的（从扫盘 + dict 联合算）。

        策略：
        - 走 :func:`list_thread_metadata` 扫盘（同步 IO，调用方应在 router
          里用 ``asyncio.to_thread`` 隔离）
        - 已 boot cell 的 metadata 用内存版本覆盖（rename 后内存版本可能
          newer，扫盘版本可能 stale）
        """
        disk_metas = list_thread_metadata(self._home)
        in_memory = {tid: cell.metadata for tid, cell in self._cells.items()}
        merged: dict[str, ThreadMetadata] = {m.id: m for m in disk_metas}
        merged.update(in_memory)
        out = list(merged.values())
        # 与 list_thread_metadata 保持一致：置顶优先、最近活跃优先、同秒按 id 稳定排序。
        out.sort(key=lambda m: (m.is_pinned, m.updated_at, m.id), reverse=True)
        return out

    def list_cells(self) -> list[CellSummaryDTO]:
        """REST ``GET /api/manage/cells`` 数据源；仅活的 cell。"""
        out: list[CellSummaryDTO] = []
        for cell in self._cells.values():
            # status 不应出现 evicting（evicting 立即从 dict pop）
            status = (
                cell.status if cell.status in ("idle", "running", "awaiting_approval") else "idle"
            )
            out.append(
                CellSummaryDTO(
                    thread_id=cell.thread_id,
                    thread_name=cell.metadata.name,
                    preset_id=cell.metadata.preset_id,
                    created_at=cell.metadata.created_at,
                    last_active_at=cell.last_active_at,
                    current_turn=None,  # v0.1.5 不暴露 current_turn 精确值
                    pending_approval_count=cell.adapter.pending_approval_count,
                    status=status,
                )
            )
        return out

    def get_cell(self, thread_id: str) -> ThreadCell | None:
        """同步查 cell；不触发 boot。"""
        return self._cells.get(thread_id)

    def find_thread_by_claude_thread_id(self, claude_thread_id: str) -> ThreadMetadata | None:
        """按 ``claude_thread_id`` 反查 thread metadata。

        v0.2.0 claude-code-history-resume 用：

        - import claude session 路径上做防重复（命中即返回已有 thread）
        - 续聊路径上从 SDK session UUID 找回对应 thread

        实现：复用 :meth:`list_threads`（已合并扫盘 + 内存覆盖）线性扫，
        命中第一个返回。空 ``claude_thread_id`` 不匹配任何 thread（默认值
        是空字符串，不能用作 key）。

        同步函数：扫盘 IO 量级与 list_threads 相同，路由层按需可在
        ``asyncio.to_thread`` 内调用。
        """
        if not claude_thread_id:
            return None
        for meta in self.list_threads():
            if meta.claude_thread_id == claude_thread_id:
                return meta
        return None

    def find_thread_by_codex_thread_id(self, codex_thread_id: str) -> ThreadMetadata | None:
        """按 ``codex_thread_id`` 反查 thread metadata。"""
        if not codex_thread_id:
            return None
        for meta in self.list_threads():
            if meta.codex_thread_id == codex_thread_id:
                return meta
        return None

    async def bind_claude_thread(
        self,
        thread_id: str,
        claude_thread_id: str,
        cwd: str,
    ) -> ThreadMetadata:
        """把 ``claude_thread_id`` + ``cwd`` 持久化到已存在的 thread。

        v0.2.0 invariant：

        - **写入后只读**：thread 当前 ``claude_thread_id != ""`` 时抛
          :class:`ClaudeThreadAlreadyBoundError`，不允许覆盖
        - **1:1 绑定**：一个 Kongming thread 绑定一个 Claude session，
          ``claude_thread_id`` 已被另一 thread 绑定时抛
          :class:`ClaudeThreadConflictError`
        - **空字符串非法**：``claude_thread_id == ""`` 抛 :class:`ValueError`
          （绑空等于不绑，禁止）

        失败：thread 不存在抛 :class:`KeyError`。

        若 cell 已 boot，同步更新 cell.metadata 引用（与 rename_thread 同款）。
        """
        if not claude_thread_id:
            raise ValueError("claude_thread_id must not be empty")

        meta = await asyncio.to_thread(read_thread_metadata, self._home, thread_id)
        if meta is None:
            raise KeyError(f"thread not found: {thread_id}")

        # invariant 双重检查
        if meta.claude_thread_id:
            raise ClaudeThreadAlreadyBoundError(
                f"thread {thread_id} already bound to claude_thread_id="
                f"{meta.claude_thread_id!r}; refuse to overwrite"
            )
        existing = self.find_thread_by_claude_thread_id(claude_thread_id)
        if existing is not None and existing.id != thread_id:
            raise ClaudeThreadConflictError(
                f"claude_thread_id={claude_thread_id!r} already bound to "
                f"thread {existing.id}; refuse duplicate bind on {thread_id}"
            )

        updated = meta.model_copy(
            update={
                "claude_thread_id": claude_thread_id,
                "cwd": cwd,
                "schema_version": 7,
                "updated_at": _now(),
            }
        )
        await asyncio.to_thread(write_thread_metadata, self._home, updated)
        async with self._lock:
            cell = self._cells.get(thread_id)
            if cell is not None:
                cell.metadata = updated
        return updated

    async def bind_codex_thread(
        self,
        thread_id: str,
        codex_thread_id: str,
        cwd: str,
    ) -> ThreadMetadata:
        """把 Codex 底层 thread id + cwd 持久化到已存在的 Kongming thread。

        一个 Kongming thread 绑定一个 Codex session/thread。绑定后只读，防止
        同一产品 thread 指向多个 provider session。
        """
        if not codex_thread_id:
            raise ValueError("codex_thread_id must not be empty")

        meta = await asyncio.to_thread(read_thread_metadata, self._home, thread_id)
        if meta is None:
            raise KeyError(f"thread not found: {thread_id}")

        if meta.codex_thread_id:
            raise CodexThreadAlreadyBoundError(
                f"thread {thread_id} already bound to codex_thread_id="
                f"{meta.codex_thread_id!r}; refuse to overwrite"
            )
        existing = self.find_thread_by_codex_thread_id(codex_thread_id)
        if existing is not None and existing.id != thread_id:
            raise CodexThreadConflictError(
                f"codex_thread_id={codex_thread_id!r} already bound to "
                f"thread {existing.id}; refuse duplicate bind on {thread_id}"
            )

        updated = meta.model_copy(
            update={
                "codex_thread_id": codex_thread_id,
                "cwd": cwd,
                "schema_version": 7,
                "updated_at": _now(),
            }
        )
        await asyncio.to_thread(write_thread_metadata, self._home, updated)
        async with self._lock:
            cell = self._cells.get(thread_id)
            if cell is not None:
                cell.metadata = updated
        return updated

    # ------------------------------------------------------------------
    # 审批
    # ------------------------------------------------------------------

    def resolve_approval(self, thread_id: str, call_id: str, action: ApprovalAction | str) -> None:
        """WS 收到 ``approval.ack`` 时由路由层调用（v0.1.6 三态）。

        thread_id 不存在 / call_id 不存在均静默丢 —— 重放 / 旧请求都吞掉。

        ``action`` 接受 :class:`ApprovalAction` 枚举或字符串字面值
        （``"accept_once"`` / ``"accept_for_session"`` / ``"reject"``）；
        路由层 ``ws.py`` 是 app shell 层不允许 import ``core.contracts``，
        统一在 thread_manager 这一装配层做字符串 → 枚举转换。非法字符串降级
        为 REJECT（fail-safe）+ 日志告警。

        ``ACCEPT_FOR_SESSION`` 会通过下游 ``InteractiveApproval`` →
        ``SafetyGatedApproval`` 触发 GrantStore 写入，本 thread 后续同
        capability 自动 silent_allow。
        """
        cell = self._cells.get(thread_id)
        if cell is None:
            return
        if isinstance(action, str):
            try:
                action = ApprovalAction(action)
            except ValueError:
                logger.warning(
                    "approval.ack with invalid action=%r; downgrading to REJECT "
                    "(thread_id=%s call_id=%s)",
                    action,
                    thread_id,
                    call_id,
                )
                action = ApprovalAction.REJECT
        cell.adapter.resolve_approval(call_id, action)
        cell.touch()

    # ------------------------------------------------------------------
    # Cron 定向投递（v0.4）
    # ------------------------------------------------------------------

    async def append_cron_message(self, thread_id: str, text: str) -> bool:
        """Append a cron delivery message to an existing thread's session.

        v0.4 cron-thread-preset：``ThreadTargetSink`` 调此方法把 cron 结果
        追加到目标 thread 的会话历史里，并通过 WS fanout 通知前端。

        策略：
        - cell 未 boot（idle evicted / 从未启动）→ 返回 False（v0.4 不重建
          session；后续可考虑 lazy boot 或 enqueue）
        - cell 已 boot 但 session 不存在（理论不应该；防御性兜底）→ False
        - 追加成功 → 通知 WS → True

        Returns:
            True if message was appended successfully, False otherwise.
        """
        async with self._lock:
            cell = self._cells.get(thread_id)
        if cell is None:
            return False

        # 从 runtime._sessions 获取该 thread 的 session 实例
        # NativeRuntime._sessions 是 private attr，此处为内部集成跨层访问
        sessions: dict[str, Any] | None = getattr(cell.runtime, "_sessions", None)
        if sessions is None:
            return False
        session = sessions.get(thread_id)
        if session is None:
            return False

        # 追加 assistant 消息
        msg = Message(role="assistant", content=text)
        try:
            await session.append(msg)
        except Exception as exc:
            logger.warning(
                "append_cron_message(%s): session.append failed: %s",
                thread_id,
                exc,
            )
            return False

        # 通知 WS 前端（best-effort）
        try:
            fanout = getattr(cell.adapter, "_ws", None)
            if fanout is not None and hasattr(fanout, "send_json"):
                await fanout.send_json(
                    {
                        # protocol-frame-type-unify-v0.2：wire 协议判别字段
                        # 从 ``kind`` 切到 ``frame_type``。
                        "frame_type": "cron.message.appended",
                        "thread_id": thread_id,
                        "content": text,
                    }
                )
        except Exception as exc:
            log_network_exception(
                "hosts.web.threads.manager",
                "cron_message_notify_failed",
                exc,
                thread_id=thread_id,
            )

        cell.touch()
        return True

    # ------------------------------------------------------------------
    # 路径辅助（供路由层 / 测试断言）
    # ------------------------------------------------------------------

    def metadata_path(self, thread_id: str) -> Path:
        return thread_metadata_path(self._home, thread_id)

    @property
    def kongming_home(self) -> Path:
        return self._home

    @property
    def started(self) -> bool:
        return self._started

    @property
    def closed(self) -> bool:
        return self._closed


class _NullWS:
    """ws 引用占位：cell 创建后未 attach_ws 时使用。

    实现：``send_json`` 抛 RuntimeError 让 ``_safe_send_json`` 捕获 + 标 closed；
    ``close`` 是 no-op。

    比 None 更友好：避免在 adapter 内每次 send 都做 ``if self._ws is None``
    特判。
    """

    async def send_json(self, _payload: Any) -> None:
        raise RuntimeError("ws not attached")

    async def close(self) -> None:
        return None


__all__ = [
    "ClaudeThreadAlreadyBoundError",
    "ClaudeThreadConflictError",
    "CodexThreadAlreadyBoundError",
    "CodexThreadConflictError",
    "RuntimeFactory",
    "ThreadManager",
    "ThreadPresetRefreshError",
]
