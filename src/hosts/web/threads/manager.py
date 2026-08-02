"""Thread fleet 单例管理器。

:class:`ThreadManager` 持有 ``dict[thread_id, ThreadCell]``，是 v0.1.5 web
后端的 backbone：FastAPI 路由 / WS endpoint / REST endpoint 通过它拿到
"已 boot 的 cell"或"未 boot 的 metadata 列表"，再走 host_dispatcher.submit。

设计要点：

- **runtime_factory 注入**：本类不直接耦合 :meth:`SessionEngine.build` 的
  全部参数。装配方（web-app-shell 任务的 startup hook）传入一个
  ``async (thread_id, preset_id) -> SessionEngine`` 的 factory，让本类专
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
import copy
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, cast

from typing_extensions import override

from application.agents.manager import SubmitMode
from core.clock import now_epoch_ms
from core.contracts import AttachmentKind, Event, EventSink, SteerRequest
from core.mail import Mail
from core.message import Message
from core.result import Result, RunEndReason, compute_run_end_reason
from hosts.shared.host_dispatcher import HostDispatcher
from hosts.web.app_support.host_adapter import WebHostAdapter
from hosts.web.app_support.path_utils import is_absolute_workspace_path
from hosts.web.integrations.claude_code.jsonl_history import jsonl_path_for
from hosts.web.integrations.codex.projects_scanner import list_codex_projects
from hosts.web.protocol import (
    CellEvictedFrame,
    CellSummaryDTO,
    EvictReason,
    PendingInputChangedFrame,
    PendingInputDTO,
    PendingInputSnapshotFrame,
    PendingInputStartedFrame,
    PendingInputSteeredFrame,
)
from hosts.web.threads.cell import ThreadCell, ThreadCellStatus
from hosts.web.threads.errors import ThreadForkConflictError, ThreadPresetRefreshError
from hosts.web.threads.metadata import (
    ThreadMetadata,
    delete_thread_metadata_dir,
    list_thread_metadata,
    read_thread_metadata,
    thread_metadata_path,
    write_thread_metadata,
)
from hosts.web.uploads.storage import AssetStorage, AttachmentAssetRef
from hosts.web.usage.usage_token_v2 import (
    ClaudeJsonlLocator,
    CodexRolloutLocator,
    ThreadMetadataReader,
    UsageTokenManager,
)
from hosts.web.websocket.event_sink import WSEventSink
from hosts.web.websocket.fanout import WebSocketFanout
from hosts.web.websocket.thread_status import ThreadStatusEventSink
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.models import Config
from infrastructure.config.paths import resolve_kongming_path
from network.network_log import log_network_event, log_network_exception
from safety.approval.permissions_manager import PermissionsManager

if TYPE_CHECKING:
    from hosts.web.threads.types import ThreadManagerProtocol

logger = logging.getLogger(__name__)

MAX_PENDING_INPUTS = 20
"""单个 thread 在 active run 期间可保留的后续输入上限。"""

PENDING_INPUT_QUEUE_FULL = "pending_input_queue_full"
"""队列满错误的稳定 reason，WS error 帧和前端草稿恢复共用。"""

PENDING_INPUT_NOT_FOUND = "pending_input_not_found"
"""pending input ID 不存在的稳定 reason。"""

PENDING_INPUT_NOT_INJECTABLE = "pending_input_not_injectable"
"""pending input 含结构化运行参数，不能安全插入当前 run。"""

PENDING_INPUT_SEND_NOW_INACTIVE = "pending_input_send_now_inactive"
"""当前 run 不接受立即插入，队列项保持原位。"""

ROOT_AGENT_REGISTRY_CLOSED = "root_agent_registry_closed"
"""root agent 生命周期账本已关闭，当前队列消费等待后续恢复策略。"""

NO_ROOT_AGENT = "no_root_agent"
"""root AgentManager 无法装配的稳定 reason。"""

_SEND_NOW_ALLOWED_METADATA_KEYS = frozenset({"request_id", "source_conn_id"})


@dataclass(frozen=True)
class PendingInputSubmitResult:
    """普通输入提交结果。

    accepted 表示后端已接收输入；started 表示本次输入已立即启动 run；
    pending_input 是被接收的队列项或即时启动项；snapshot 是提交后的队列真源快照。
    error_reason 预留给稳定错误码，当前队列满通过异常路径返回。

    状态归属：结果对象只描述本次提交的判定；队列真源仍在 ``ThreadCell``，
    前端同步以 snapshot / changed / started WS 帧为准。
    """

    accepted: bool
    started: bool
    pending_input: PendingInputDTO | None
    snapshot: PendingInputSnapshotFrame
    error_reason: str | None = None


class PendingInputQueueFullError(RuntimeError):
    """同 thread pending input 队列已满。

    reason 是 WS error 帧使用的稳定错误码，前端据此恢复 Composer 草稿并显示队列满提示。
    """

    reason = PENDING_INPUT_QUEUE_FULL


class PendingInputOperationError(RuntimeError):
    """pending input 操作失败。

    reason 是 WS error 帧使用的稳定错误码；message 给 UI 和测试断言读取。
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class ClaudeThreadAlreadyBoundError(ValueError):
    """thread.claude_thread_id 已经非空，禁止覆盖（v0.2 invariant：写入后只读）。"""


class ClaudeThreadConflictError(ValueError):
    """claude_thread_id 已被另一 thread 绑定，禁止重复绑定（v0.2 invariant：1:1 绑定）。"""


class _EphemeralSessionCell:
    """临时 session cell，用于非 thread metadata 会话。

    当前用途是定时任务 run history 的人工对话：session_id 来自
    `ScheduledRun.session_id`，格式通常是 `sched-*`，无法写入
    `ThreadMetadata.id` 的 `thread-*` 约束。该 cell 只服务当前 WS 连接，
    断开后由路由关闭 runtime。
    """

    def __init__(
        self,
        *,
        session_id: str,
        preset_id: str,
        runtime: Any,
        host_dispatcher: Any,
        adapter: WebHostAdapter,
        event_sinks: list[Any],
    ) -> None:
        self.thread_id = session_id
        self.metadata = SimpleNamespace(id=session_id, preset_id=preset_id)
        self.runtime = runtime
        self.host_dispatcher = host_dispatcher
        self.adapter = adapter
        self.event_sinks = event_sinks
        self.runtime_preset_id = preset_id
        self.current_run_task: asyncio.Task[Any] | None = None
        self.last_active_at = time.time()

    def attach_ws(self, new_ws: Any) -> None:
        self.adapter.attach_ws(new_ws)
        for sink in self.event_sinks:
            attach = getattr(sink, "attach_ws", None)
            if callable(attach):
                attach(new_ws)

    def detach_ws(self, ws: Any) -> None:
        detach = getattr(self.adapter, "detach_ws", None)
        if callable(detach):
            detach(ws)
        for sink in self.event_sinks:
            detach_sink = getattr(sink, "detach_ws", None)
            if callable(detach_sink):
                detach_sink(ws)

    def get_client_event_sink(self) -> Any | None:
        """返回临时 cell 装配的浏览器事件 sink。"""
        return self.event_sinks[0] if self.event_sinks else None

    def touch(self) -> None:
        self.last_active_at = time.time()


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
    """``GenericChatSessionLocator`` v2 Protocol 实现 —— 定位 FileSession JSONL。

    返回 None：
    - 非 ``backend_kind="generic_chat"``
    - session backend 不是 FileSession（memory / sqlite 不支持派生）
    - thread 未跑过（session jsonl 未 materialize）
    """

    def __init__(
        self,
        home: Path,
        cfg: Config,
    ) -> None:
        self._home = home
        self._cfg = cfg

    async def locate(self, thread_id: str) -> Path | None:
        meta = await asyncio.to_thread(read_thread_metadata, self._home, thread_id)
        if meta is None or meta.backend_kind != "generic_chat":
            return None
        # 检查 session backend 是否 file（D-1 决策：只支持 FileSession）
        session_cfg = getattr(self._cfg, "session", None)
        backend_kind = getattr(session_cfg, "backend", "file") if session_cfg else "file"
        if backend_kind != "file":
            # memory / sqlite backend → 不支持派生
            return None
        # 按 FileSession manifest 的 format 字段定位真实 JSONL 文件名。
        raw_store_path = (
            getattr(session_cfg, "file_store_path", ".kongming/sessions")
            if session_cfg
            else ".kongming/sessions"
        )
        session_root = resolve_kongming_path(raw_store_path, kongming_home=self._home)
        session_dir = session_root / thread_id
        manifest_path = session_dir / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        format_name = manifest.get("format")
        if (
            not isinstance(format_name, str)
            or not format_name.strip()
            or "/" in format_name
            or "\\" in format_name
        ):
            return None
        jsonl_path = session_dir / format_name
        if not jsonl_path.is_file():
            return None
        return jsonl_path


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
# 完整 SessionEngine + HostDispatcher。装配方负责把 :class:`SessionEngine.build`
# 的参数（cfg, event_sinks, instructions, ...）和 HostDispatcher 注入进 closure。
#
# 返回值用 tuple，避免本类需要知道 HostDispatcher 怎么从 runtime / adapter 拼装。
RuntimeFactory = Callable[
    [str, str, WebHostAdapter, list[Any]],
    Awaitable[tuple[Any, Any]],  # (SessionEngine, HostDispatcher)
]


def _generate_thread_id() -> str:
    """生成 ``thread-<hex12>`` 格式 ID。

    使用 :mod:`secrets` 而非 ``uuid.uuid4()`` 因为 12 位 hex 已经够冲突避免
    （理论冲突概率 ~1/2^48），且 secrets 输出更短。
    """
    return f"thread-{secrets.token_hex(6)}"


def _now() -> float:
    return time.time()


def _now_ms_int() -> int:
    """返回整数毫秒时间戳，供 pending input DTO 和 WS frame 使用。"""
    return now_epoch_ms()


def _pending_input_preview(text: str, limit: int = 120) -> str:
    """生成队列列表展示用预览文本。

    输入是完整用户内容；输出是压缩空白后的短文本，持久真源仍是 DTO.content。
    """
    normalized = " ".join(text.strip().split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit]}..."


def _title_from_first_message(text: str) -> str:
    normalized = " ".join(text.strip().split())
    return normalized[:40] or "新会话"


def _forked_thread_name(source_name: str) -> str:
    """生成 fork 后的默认标题，并保持 ThreadMetadata 的 200 字符上限。"""
    suffix = "（分支）"
    normalized = source_name.strip() or "新会话"
    return f"{normalized[: 200 - len(suffix)]}{suffix}"


def _fork_history_prefix(
    history: list[Message],
    *,
    history_index: int | None,
) -> list[Message]:
    """选择 fork 历史前缀，并验证工具请求与结果完整配对。"""
    if history_index is None:
        selected = history
    else:
        if isinstance(history_index, bool) or history_index < 0 or history_index >= len(history):
            raise ValueError(f"history_index {history_index} is outside source history")
        boundary = history[history_index]
        if boundary.role != "assistant" or boundary.tool_calls:
            raise ValueError("fork boundary must reference a terminal assistant message")
        selected = history[: history_index + 1]

    pending_tool_call_ids: set[str] = set()
    for message in selected:
        if message.role == "assistant" and message.tool_calls:
            if pending_tool_call_ids:
                raise ValueError("fork history contains an incomplete tool request/result pair")
            for tool_call in message.tool_calls:
                call_id = tool_call.call_id
                if not call_id or call_id in pending_tool_call_ids:
                    raise ValueError("fork history contains an invalid tool request/result pair")
                pending_tool_call_ids.add(call_id)
            continue
        if message.role == "tool":
            tool_call_id = message.tool_call_id
            if not isinstance(tool_call_id, str) or tool_call_id not in pending_tool_call_ids:
                raise ValueError("fork history contains an invalid tool request/result pair")
            pending_tool_call_ids.remove(tool_call_id)
            continue
        if pending_tool_call_ids:
            raise ValueError("fork history contains an incomplete tool request/result pair")
    if pending_tool_call_ids:
        raise ValueError("fork history contains an incomplete tool request/result pair")
    return selected


def _ready_attachment_references(
    history: list[Message],
) -> tuple[AttachmentAssetRef, ...]:
    """从历史快照提取去重、稳定排序的 ready 附件引用。"""
    references: set[AttachmentAssetRef] = set()
    for message in history:
        raw_attachments = message.metadata.get("attachments")
        if not isinstance(raw_attachments, list):
            continue
        for raw_attachment in raw_attachments:
            if not isinstance(raw_attachment, dict):
                continue
            if raw_attachment.get("status") != "ready":
                continue
            asset_id = raw_attachment.get("asset_id")
            kind = raw_attachment.get("kind")
            if not isinstance(asset_id, str) or not asset_id:
                raise ValueError("ready attachment requires asset_id")
            if kind not in {"image", "video", "file"}:
                raise ValueError(f"ready attachment has invalid kind: {kind!r}")
            references.add(
                AttachmentAssetRef(
                    kind=cast(AttachmentKind, kind),
                    asset_id=asset_id,
                )
            )
    return tuple(sorted(references))


async def _await_fork_io(operation: Awaitable[object]) -> None:
    """等待 fork IO 完成；外层取消发生时先收口在途操作再传播取消。"""
    task = asyncio.ensure_future(operation)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            logger.exception("fork IO failed while request cancellation was settling")
        raise


def _resolve_first_message_cwd(cwd: str) -> str:
    normalized = cwd.strip()
    if not normalized:
        return ""
    if not is_absolute_workspace_path(normalized):
        raise ValueError("cwd must be an absolute path")
    if PurePosixPath(normalized).is_absolute():
        return str(Path(normalized).expanduser().resolve())
    return normalized


class _PendingInputSteerEventSink(EventSink):
    """ThreadManager 内部 sink：把 runtime steer 事件映射回 pending input 气泡。"""

    def __init__(
        self,
        manager: ThreadManager,
        cell_getter: Callable[[], ThreadCell | None],
    ) -> None:
        self._manager = manager
        self._cell_getter = cell_getter

    @override
    async def emit(self, event: Event) -> None:
        cell = self._cell_getter()
        if cell is None:
            return
        await self._manager._handle_runtime_event(cell, event)


def _build_mail_event_context(mail: Mail, *, conversation_id: str) -> dict[str, Any]:
    """从 Mail 构造 Runner turn 事件上下文。

    输入为 Mail 和会话 conversation_id；输出为 trace payload 片段，挂在
    ``runtime.run`` 的 ``event_context`` 上，让下游事件能溯源到具体 mail。
    """
    return {
        "run_epoch": mail.epoch,
        "mail_kind": mail.kind,
        "mail_task_id": mail.task_id,
        "conversation_id": conversation_id,
    }


def _host_dispatcher_for(cell: Any) -> HostDispatcher:
    """读取 cell 持有的 HostDispatcher。

    host-dispatch-consolidation 完成后，``ThreadCell`` 和 ``_EphemeralSessionCell``
    都直接持有 ``host_dispatcher`` 字段。本 helper 集中访问，便于未来加守卫或观测。
    """
    dispatcher = getattr(cell, "host_dispatcher", None)
    if dispatcher is None:
        raise RuntimeError(
            f"cell has no host_dispatcher: thread={getattr(cell, 'thread_id', None)!r}"
        )
    return cast(HostDispatcher, dispatcher)


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
        approval_manager: Any | None = None,
        permissions_manager: PermissionsManager | None = None,
        model_catalog_manager: ModelCatalogManager | None = None,
    ) -> None:
        """
        Args:
            asset_storage: 可选注入的 :class:`AssetStorage`,用于 ``delete_thread``
                时同步清理 thread 名下所有上传资产(claude-image-paste-e2e P1 #2
                R2 boundary fix)。``None`` 时跳过资产清理(CLI / 测试路径常态)。
            approval_manager: 可选注入的显式审批管理器。generic_chat 的 pending
                审批状态由该 manager 维护，ThreadManager 只做只读投影。
        """
        self._cfg = cfg
        self._home = kongming_home
        self._runtime_factory = runtime_factory
        self._asset_storage = asset_storage
        self._approval_manager = approval_manager
        self._permissions_manager = permissions_manager
        self._model_catalog_manager = model_catalog_manager or ModelCatalogManager(
            user_path=kongming_home / "model-providers.yaml"
        )
        self._permissions_cleanup_retry: set[str] = set()
        self._permissions_cleanup_tasks: set[asyncio.Task[None]] = set()
        self._metadata_mutation_locks: dict[str, asyncio.Lock] = {}
        self._cells: dict[str, ThreadCell] = {}
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task[None] | None = None
        # run done callback 不能 await drain，这里保存后台 drain task，shutdown 时统一取消。
        self._pending_drain_tasks: set[asyncio.Task[Any]] = set()
        self._started = False
        self._closed = False
        # usage-token-v2-bigbang: UsageTokenManager v2 注入——无状态门面。
        # token 真源来自 SDK 写的 jsonl/rollout，由 manager 内部派生器现场算。
        # metadata.json 不再缓存 token（schema v9 已物理删 3 个 token 字段）。
        self._usage_manager: UsageTokenManager = UsageTokenManager(
            meta_reader=_ThreadMetadataReaderImpl(kongming_home),
            claude_locator=_ClaudeJsonlLocatorImpl(kongming_home),
            codex_locator=_CodexRolloutLocatorImpl(kongming_home),
            generic_locator=_GenericChatSessionLocatorImpl(
                kongming_home,
                cfg,
            ),
        )

    @property
    def usage_manager(self) -> UsageTokenManager:
        """v2 manager: 暴露给 router / ws handler / service 调
        ``get_thread_usage(thread_id)`` 唯一公共方法。

        外部消费方**只能**调这一个方法；v1 时代的 record_run_usage /
        set_last_assistant_usage / get_thread_summary 等方法 v2 全部删除。
        """
        return self._usage_manager

    def set_approval_manager(self, approval_manager: Any) -> None:
        """挂载显式审批管理器，供运行状态投影和 idle eviction 只读查询。"""
        self._approval_manager = approval_manager

    def set_permissions_manager(self, permissions_manager: PermissionsManager) -> None:
        """挂载 thread permissions 门户，供显式 thread 删除补偿使用。"""
        self._permissions_manager = permissions_manager

    def _metadata_mutation_lock(self, thread_id: str) -> asyncio.Lock:
        """返回单 thread metadata 创建、更新与删除共用的串行锁。"""
        return self._metadata_mutation_locks.setdefault(thread_id, asyncio.Lock())

    @property
    def pending_permissions_cleanup(self) -> tuple[str, ...]:
        """返回等待重试的 permissions 清理 thread id。"""
        return tuple(sorted(self._permissions_cleanup_retry))

    def _pending_approval_count(self, thread_id: str) -> int:
        """从 ApprovalManager 读取当前 thread 的 generic_chat 待审批数量。"""
        manager = self._approval_manager
        count_for_thread = getattr(manager, "pending_count_for_thread", None)
        if not callable(count_for_thread):
            return 0
        try:
            return int(count_for_thread(thread_id, channel="generic_chat"))
        except Exception:
            logger.exception(
                "approval pending count lookup failed for thread=%s",
                thread_id,
            )
            return 0

    def _has_pending_approval(self, thread_id: str) -> bool:
        """返回当前 thread 是否存在 generic_chat 待审批。"""
        manager = self._approval_manager
        has_pending = getattr(manager, "has_pending_for_thread", None)
        if callable(has_pending):
            try:
                return bool(has_pending(thread_id, channel="generic_chat"))
            except Exception:
                logger.exception(
                    "approval pending flag lookup failed for thread=%s",
                    thread_id,
                )
                return True
        return self._pending_approval_count(thread_id) > 0

    def _cancel_pending_approvals_for_thread(
        self,
        thread_id: str,
        *,
        reason: str = "cell_evict",
    ) -> None:
        """取消当前 thread 名下仍挂起的 ApprovalManager 审批。"""
        manager = self._approval_manager
        cancel_by_thread = getattr(manager, "cancel_by_thread", None)
        if not callable(cancel_by_thread):
            return
        try:
            cancelled = int(cancel_by_thread(thread_id, reason=reason))
        except Exception:
            logger.exception(
                "approval pending cancel failed for thread=%s",
                thread_id,
            )
            return
        if cancelled > 0:
            logger.debug(
                "ThreadManager cancelled %d approval pending for thread=%s",
                cancelled,
                thread_id,
            )

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
        - cancel 所有 pending input drain 后台 task
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
        cleanup_tasks = list(self._permissions_cleanup_tasks)
        for task in cleanup_tasks:
            if not task.done():
                task.cancel()
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

        if self._pending_drain_tasks:
            drain_tasks = list(self._pending_drain_tasks)
            for task in drain_tasks:
                task.cancel()
            await asyncio.gather(*drain_tasks, return_exceptions=True)
            self._pending_drain_tasks.clear()

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
        if backend_kind == "generic_chat":
            runtime = self._model_catalog_manager.resolve_runtime(
                self._cfg.model,
                preset_id=preset_id.strip(),
            )
            self._model_catalog_manager.resolve_credential(runtime)
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
        forked_from_id: str | None = None,
        forked_from_history_index: int | None = None,
    ) -> ThreadMetadata:
        """内部创建 thread metadata；允许 scheduled_task 门户传入业务类型字段。"""
        meta = self._new_thread_metadata(
            name=name,
            preset_id=preset_id,
            backend_kind=backend_kind,
            thread_kind=thread_kind,
            source_kind=source_kind,
            source_id=source_id,
            cwd=cwd,
            forked_from_id=forked_from_id,
            forked_from_history_index=forked_from_history_index,
        )
        await self._persist_new_thread_metadata(meta)
        return meta

    def _new_thread_metadata(
        self,
        *,
        name: str,
        preset_id: str,
        backend_kind: Literal["generic_chat", "claude_code", "codex"],
        thread_kind: Literal["chat", "scheduled_task"],
        source_kind: str,
        source_id: str,
        cwd: str,
        forked_from_id: str | None = None,
        forked_from_history_index: int | None = None,
    ) -> ThreadMetadata:
        """构造尚未公开的 metadata；调用方选择最终提交时机。"""
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
            forked_from_id=forked_from_id,
            forked_from_history_index=forked_from_history_index,
            created_at=now,
            updated_at=now,
            message_count=0,
        )
        return meta

    async def create_generic_thread_from_first_message(
        self,
        *,
        text: str,
        preset_id: str,
        cwd: str = "",
        reasoning_effort: Literal["none", "low", "medium", "high", "max"] | None = None,
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
        runtime = self._model_catalog_manager.resolve_runtime(
            self._cfg.model,
            preset_id=normalized_preset_id,
            reasoning_effort=reasoning_effort,
        )
        self._model_catalog_manager.resolve_credential(runtime)
        resolved_cwd = _resolve_first_message_cwd(cwd)

        meta = await self.create_thread(
            _title_from_first_message(normalized_text),
            normalized_preset_id,
            backend_kind="generic_chat",
            cwd=resolved_cwd,
        )
        try:
            cell = await self.boot_or_attach(meta.id)
            await cell.runtime.append_session_message(
                meta.id,
                Message.user(normalized_text),
            )
            updated = meta.model_copy(
                update={
                    "message_count": 1,
                    "updated_at": _now(),
                    "schema_version": meta.schema_version,
                }
            )
            await self._persist_thread_metadata(updated)
            self._start_first_message_run(
                cell,
                normalized_text,
                reasoning_effort=reasoning_effort,
            )
            return updated
        except Exception:
            with suppress(Exception):
                await self.delete_thread(meta.id, keep_history=False)
            raise

    async def fork_thread(
        self,
        source_thread_id: str,
        *,
        history_index: int | None = None,
    ) -> ThreadMetadata:
        """在 assistant 回复边界 fork 一个 idle generic chat thread。

        快照真源是 :class:`SessionEngine` 的结构化 ``Message`` 历史门户，因此 user /
        assistant / system / tool、tool_calls、tool_result 关联、附件与 provider
        metadata 都会进入新 session。源 thread 的 ``pending_input_lock`` 同时
        保护普通输入启动闸门；运行中或存在排队输入时返回冲突，保证快照里没有
        半截 turn。

        新 thread 继承 preset 与 cwd，run_count 从 0 开始，下一次发送产生自己的
        run lineage。复制中任一步失败都会清理目标 session、metadata 和附件。
        """
        source_meta = await asyncio.to_thread(
            read_thread_metadata,
            self._home,
            source_thread_id,
        )
        if source_meta is None:
            raise KeyError(f"thread not found: {source_thread_id}")
        if source_meta.backend_kind != "generic_chat":
            raise ValueError("fork supports generic_chat threads")
        if source_meta.claude_thread_id or source_meta.codex_thread_id:
            raise ValueError("provider-owned session history cannot be forked")
        if source_meta.thread_kind != "chat":
            raise ValueError("scheduled task threads cannot be forked")
        if self._cfg.session.backend != "file":
            raise ValueError("fork requires session.backend=file")

        source_cell = await self.boot_or_attach(source_thread_id)
        async with source_cell.pending_input_lock:
            if (
                source_cell.current_run_task is not None
                or self._has_pending_approval(source_thread_id)
                or source_cell.pending_inputs
                or source_cell.pending_input_send_now_claims
            ):
                raise ThreadForkConflictError(
                    f"thread {source_thread_id} must have a closed turn boundary"
                )
            complete_history = copy.deepcopy(
                await source_cell.runtime.read_session_history(source_thread_id)
            )
            source_history = _fork_history_prefix(
                complete_history,
                history_index=history_index,
            )
            forked_from_history_index = (
                len(source_history) - 1
                if source_history
                and source_history[-1].role == "assistant"
                and not source_history[-1].tool_calls
                else None
            )
            asset_references = _ready_attachment_references(source_history)

        target_meta = self._new_thread_metadata(
            name=_forked_thread_name(source_meta.name),
            preset_id=source_meta.preset_id,
            backend_kind="generic_chat",
            thread_kind="chat",
            source_kind="",
            source_id="",
            cwd=source_meta.cwd,
            forked_from_id=source_meta.id,
            forked_from_history_index=forked_from_history_index,
        )
        async with self._lock:
            target_boot_lock = self._boot_locks.setdefault(
                target_meta.id,
                asyncio.Lock(),
            )

        target_cell: ThreadCell | None = None
        try:
            async with target_boot_lock:
                try:
                    target_cell = await self._build_cell(target_meta)
                    if asset_references:
                        if self._asset_storage is None:
                            raise RuntimeError(
                                "asset storage is required for referenced attachments"
                            )
                        await _await_fork_io(
                            asyncio.to_thread(
                                self._asset_storage.copy_thread_assets,
                                source_thread_id=source_meta.id,
                                target_thread_id=target_meta.id,
                                references=asset_references,
                            )
                        )
                    await _await_fork_io(
                        target_cell.runtime.seed_empty_session_history(
                            target_meta.id,
                            source_history,
                        )
                    )

                    target_meta = target_meta.model_copy(
                        update={
                            "message_count": len(source_history),
                            "updated_at": _now(),
                        }
                    )
                    await _await_fork_io(self._persist_new_thread_metadata(target_meta))
                    target_cell.metadata = target_meta
                    async with self._lock:
                        self._cells[target_meta.id] = target_cell
                    return target_meta
                except asyncio.CancelledError:
                    await self._cleanup_unpublished_fork_target(
                        target_meta=target_meta,
                        target_cell=target_cell,
                    )
                    raise
                except Exception:
                    await self._cleanup_unpublished_fork_target(
                        target_meta=target_meta,
                        target_cell=target_cell,
                    )
                    raise
        finally:
            async with self._lock:
                if target_meta.id not in self._cells:
                    self._boot_locks.pop(target_meta.id, None)

    async def _cleanup_unpublished_fork_target(
        self,
        *,
        target_meta: ThreadMetadata,
        target_cell: ThreadCell | None,
    ) -> None:
        """通过公开 runtime 门户清理 fork 私有阶段的历史、metadata 与资产。"""
        with suppress(Exception):
            await _await_fork_io(
                asyncio.to_thread(
                    delete_thread_metadata_dir,
                    self._home,
                    target_meta.id,
                )
            )
        if target_cell is not None:
            with suppress(Exception):
                await _await_fork_io(target_cell.runtime.clear_session_history(target_meta.id))
        if self._asset_storage is not None:
            with suppress(Exception):
                await _await_fork_io(
                    asyncio.to_thread(
                        self._asset_storage.delete_thread_assets,
                        thread_id=target_meta.id,
                    )
                )
        if target_cell is not None:
            with suppress(Exception):
                await self._finish_evicted_cell(
                    target_cell,
                    reason="manual_stop",
                    notify_ws=False,
                )

    def _start_first_message_run(
        self,
        cell: ThreadCell,
        content: str,
        *,
        reasoning_effort: Literal["none", "low", "medium", "high", "max"] | None,
    ) -> None:
        task = self._start_host_dispatcher_run(
            cell,
            content,
            metadata={
                "reasoning_effort": reasoning_effort,
                "continue_from_last_user_message": True,
            },
            task_name=f"web-first-message-run-{cell.thread_id}",
        )

        def _done_callback(
            finished: asyncio.Task[Result],
            *,
            _cell: ThreadCell = cell,
            _task: asyncio.Task[Result] = task,
        ) -> None:
            self._on_pending_run_done(_cell, _task, finished)

        task.add_done_callback(_done_callback)

    async def submit_user_input(
        self,
        thread_id: str,
        text: str,
        *,
        request_id: str | None = None,
        reasoning_effort: Literal["none", "low", "medium", "high", "max"] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
        source_conn_id: str | None = None,
    ) -> PendingInputSubmitResult:
        """提交 Web 普通输入；idle 立即启动，running 入队。

        关键输入是用户文本、reasoning effort、附件、引用和来源连接 ID；关键输出是
        PendingInputSubmitResult。队列只覆盖 metadata thread，active run 期间最多保留
        MAX_PENDING_INPUTS 条后续输入。
        """
        metadata = {
            "request_id": request_id,
            "reasoning_effort": reasoning_effort,
            "attachments": attachments,
            "references": references,
            "source_conn_id": source_conn_id,
        }
        return await self._submit_pending_input(
            thread_id,
            text,
            source="user_input",
            priority="user_message",
            metadata=metadata,
        )

    async def submit_choice_result(
        self,
        thread_id: str,
        choice_text: str,
        *,
        request_id: str,
    ) -> PendingInputSubmitResult:
        """提交 choice 结果；running 时使用 choice_response 优先级入队。

        choice_response 在 drain 时排在普通用户消息前面，避免用户先回答选择题后又发
        新消息时，结构化回答被普通消息插队。
        """
        return await self._submit_pending_input(
            thread_id,
            choice_text,
            source="choice_submit",
            priority="choice_response",
            metadata={"request_id": request_id},
        )

    async def submit_avatar_input(
        self,
        thread_id: str,
        text: str,
        *,
        request_id: str | None = None,
        reasoning_effort: Literal["none", "low", "medium", "high", "max"] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        avatar_run_id: str | None = None,
    ) -> PendingInputSubmitResult:
        """提交 Avatar 输入；复用 generic_chat 普通 run gate。

        avatar_run_id 透传到 started 帧，便于前端把 Avatar 触发的一次运行和队列启动
        事件关联起来。
        """
        metadata = {
            "request_id": request_id,
            "reasoning_effort": reasoning_effort,
            "attachments": attachments,
            "avatar_run_id": avatar_run_id,
        }
        return await self._submit_pending_input(
            thread_id,
            text,
            source="avatar",
            priority="user_message",
            metadata=metadata,
        )

    async def cancel_pending_input(
        self,
        thread_id: str,
        pending_input_id: str,
    ) -> PendingInputSnapshotFrame:
        """删除尚未启动的 pending input 并广播队列变更。

        只删除仍在队列中的项；已经启动的输入由 current_run_task 接管，本入口不取消
        正在执行的 run。
        """
        cell = await self.boot_or_attach(thread_id)
        removed = False
        async with cell.pending_input_lock:
            before = len(cell.pending_inputs)
            cell.pending_inputs = [
                item for item in cell.pending_inputs if item.id != pending_input_id
            ]
            if len(cell.pending_inputs) != before:
                cell.pending_input_version += 1
                removed = True
            snapshot = self._pending_input_snapshot_from_cell(cell)
        if removed:
            await self._broadcast_pending_input_changed(cell, "removed")
        return snapshot

    async def update_pending_input(
        self,
        thread_id: str,
        pending_input_id: str,
        content: str,
    ) -> PendingInputSnapshotFrame:
        """编辑尚未启动的 pending input 并广播队列变更。

        content 会先 trim，空内容直接拒绝；只有 status 为 queued 的项会被更新。
        """
        normalized = content.strip()
        if not normalized:
            raise ValueError("content must not be empty")
        cell = await self.boot_or_attach(thread_id)
        updated = False
        async with cell.pending_input_lock:
            items: list[PendingInputDTO] = []
            for item in cell.pending_inputs:
                if item.id == pending_input_id and item.status == "queued":
                    item = item.model_copy(
                        update={
                            "content": normalized,
                            "preview": _pending_input_preview(normalized),
                            "updated_at_ms": _now_ms_int(),
                        }
                    )
                    updated = True
                items.append(item)
            cell.pending_inputs = self._ordered_pending_inputs(items)
            if updated:
                cell.pending_input_version += 1
            snapshot = self._pending_input_snapshot_from_cell(cell)
        if updated:
            await self._broadcast_pending_input_changed(cell, "updated")
        return snapshot

    async def reorder_pending_inputs(
        self,
        thread_id: str,
        ordered_ids: list[str],
    ) -> PendingInputSnapshotFrame:
        """按拖拽后的完整 ID 顺序重排 queued pending input。

        ordered_ids 必须覆盖当前队列里的全部 item，且不能包含重复或未知 ID。
        后端仍通过 _ordered_pending_inputs 生成最终快照，保持队列排序真源唯一。
        """
        cell = await self.boot_or_attach(thread_id)
        if not ordered_ids:
            raise ValueError("ordered_ids must not be empty")
        async with cell.pending_input_lock:
            current_items = self._ordered_pending_inputs(cell.pending_inputs)
            current_ids = [item.id for item in current_items]
            if len(set(ordered_ids)) != len(ordered_ids):
                raise ValueError("ordered_ids must not contain duplicates")
            if set(ordered_ids) != set(current_ids):
                raise ValueError("ordered_ids must match current pending input ids")

            changed = ordered_ids != current_ids
            if changed:
                by_id = {item.id: item for item in current_items}
                now = _now_ms_int()
                cell.pending_inputs = [
                    by_id[item_id].model_copy(update={"sequence": index + 1, "updated_at_ms": now})
                    for index, item_id in enumerate(ordered_ids)
                ]
                cell.pending_input_version += 1
            snapshot = self._pending_input_snapshot_from_cell(cell)
        if changed:
            await self._broadcast_pending_input_changed(cell, "reordered")
        return snapshot

    async def send_pending_input_now(
        self,
        thread_id: str,
        pending_input_id: str,
    ) -> PendingInputSnapshotFrame:
        """把 queued pending input 立即发送。

        active run 存在时只允许纯文本插入 Runner.steer；idle 时从队列移除并启动
        root mailbox run。插入失败会保留队列项，由调用方按 error 帧提示用户。

        host-dispatch-consolidation #4：send_now 改调 ``cell.host_dispatcher``。
        active 分支调用 ``submit(IMMEDIATE)`` 探测 steer；命中 steer 广播 steered，
        未命中回落 _start_pending_input_run（QUEUE）。idle 分支也走 host_dispatcher。
        """
        cell = await self.boot_or_attach(thread_id)
        started_pending: PendingInputDTO | None = None
        started_version: int | None = None
        steered_input: PendingInputDTO | None = None
        steered_version: int | None = None
        steered_active_run_id: str | None = None
        sent_now = False
        async with cell.pending_input_lock:
            current_items = self._ordered_pending_inputs(cell.pending_inputs)
            target = next((item for item in current_items if item.id == pending_input_id), None)
            if target is None:
                raise PendingInputOperationError(
                    PENDING_INPUT_NOT_FOUND,
                    "pending input not found",
                )

            registry_closed = self._is_root_agent_registry_closed(cell)
            active = cell.current_run_task is not None and not cell.current_run_task.done()
            if active:
                self._ensure_pending_input_can_send_now(target)
                dispatcher = _host_dispatcher_for(cell)
                receipt = await dispatcher.submit(
                    target.content,
                    mode=SubmitMode.IMMEDIATE,
                    steer_request=SteerRequest(
                        text=target.content,
                        pending_input_id=target.id,
                    ),
                )
                sent_now = receipt.merged
            elif registry_closed:
                cell.pending_input_drain_block_reason = ROOT_AGENT_REGISTRY_CLOSED
                raise PendingInputOperationError(
                    ROOT_AGENT_REGISTRY_CLOSED,
                    "root agent registry is closed",
                )
            elif cell.pending_input_drain_block_reason is not None:
                raise PendingInputOperationError(
                    PENDING_INPUT_SEND_NOW_INACTIVE,
                    "pending input drain is blocked",
                )

            cell.pending_inputs = [item for item in current_items if item.id != pending_input_id]
            cell.pending_input_version += 1
            if not sent_now:
                started_pending = target.model_copy(
                    update={"status": "starting", "updated_at_ms": _now_ms_int()}
                )
                started_version = cell.pending_input_version
                self._start_pending_input_run(cell, started_pending)
            else:
                steered_input = target.model_copy(
                    update={"status": "starting", "updated_at_ms": _now_ms_int()}
                )
                # 风险点：claim 当前只存在内存中，Runner drain 前刷新可通过后续
                # steer.injected 回补气泡；进程重启会丢失这条尚未落 history 的输入。
                cell.pending_input_send_now_claims.append(steered_input)
                steered_version = cell.pending_input_version
                steered_active_run_id = None
            snapshot = self._pending_input_snapshot_from_cell(cell)

        if started_pending is not None:
            await self._broadcast_pending_input_started(
                cell,
                started_pending,
                started_version if started_version is not None else snapshot.version,
            )
            await self._broadcast_pending_input_changed(cell, "drained")
        else:
            if steered_input is not None:
                await self._broadcast_pending_input_steered(
                    cell,
                    steered_input,
                    steered_version if steered_version is not None else snapshot.version,
                    active_run_id=steered_active_run_id,
                )
            await self._broadcast_pending_input_changed(
                cell,
                "sent_now",
            )
        return snapshot

    async def pending_input_snapshot(self, thread_id: str) -> PendingInputSnapshotFrame:
        """返回 thread 当前 pending input 队列快照。

        WS 新连接用它补齐后端真源状态；version 随队列变更递增，前端可丢弃旧帧。
        """
        cell = await self.boot_or_attach(thread_id)
        async with cell.pending_input_lock:
            return self._pending_input_snapshot_from_cell(cell)

    async def _submit_pending_input(
        self,
        thread_id: str,
        content: str,
        *,
        source: Literal["user_input", "choice_submit", "avatar"],
        priority: Literal["choice_response", "user_message"],
        metadata: dict[str, Any],
    ) -> PendingInputSubmitResult:
        """普通输入队列状态机入口。

        content 是已提交的用户输入；source 标记来源，priority 决定 drain 顺序，
        metadata 透传给 root agent mailbox run。函数在同一个 pending_input_lock 内完成
        "是否已有 active run" 判断和入队/启动，避免并发双启动。

        核心规则：
        1. active run 已占用执行权时，当前输入进入 pending_inputs。
        2. drain block 存在时，当前输入进入 pending_inputs，等待阻断原因清除。
        3. 当前没有 active run 且队列已有输入时，先把当前输入纳入队列，再启动
           队列头，保证已有输入继续按 priority + sequence 顺序消费。
        4. 当前没有 active run 且队列为空时，当前输入直接成为 active run。

        新增规则入口：只在下面的锁内分支扩展，并同时声明 owner 字段
        （current_run_task / pending_inputs / drain_block_reason）、snapshot version
        增量、实际启动的 pending item、锁外广播帧，以及
        ``tests/unit/web/test_pending_input_thread_manager.py`` 覆盖用例。

        并发边界：锁内只修改 cell 内存状态和创建 task；WS 广播在锁外执行，
        保持提交路径短持锁，前端最终以广播快照校准本地状态。
        """
        normalized = content.strip()
        if not normalized:
            raise ValueError("text must not be empty")
        cell = await self.boot_or_attach(thread_id)
        started_pending: PendingInputDTO | None = None
        started_version: int | None = None
        broadcast_queue_changed = False
        async with cell.pending_input_lock:
            pending = self._create_pending_input(
                cell,
                normalized,
                source=source,
                priority=priority,
                metadata=metadata,
            )
            # 状态机分支必须保持互斥：一个输入要么留在 pending_inputs，
            # 要么转交 current_run_task。新增分支需要同步维护 snapshot version
            # 与锁外广播，避免队列真源、运行 owner 和前端投影漂移。
            if self._has_active_run(cell) or cell.pending_input_drain_block_reason is not None:
                if len(cell.pending_inputs) >= MAX_PENDING_INPUTS:
                    raise PendingInputQueueFullError(PENDING_INPUT_QUEUE_FULL)
                cell.pending_inputs = self._ordered_pending_inputs([*cell.pending_inputs, pending])
                cell.pending_input_version += 1
                snapshot = self._pending_input_snapshot_from_cell(cell)
                started = False
            elif self._is_root_agent_registry_closed(cell):
                cell.pending_input_drain_block_reason = ROOT_AGENT_REGISTRY_CLOSED
                if len(cell.pending_inputs) >= MAX_PENDING_INPUTS:
                    raise PendingInputQueueFullError(PENDING_INPUT_QUEUE_FULL)
                cell.pending_inputs = self._ordered_pending_inputs([*cell.pending_inputs, pending])
                cell.pending_input_version += 1
                snapshot = self._pending_input_snapshot_from_cell(cell)
                started = False
            elif cell.pending_inputs:
                cell.pending_inputs = self._ordered_pending_inputs([*cell.pending_inputs, pending])
                cell.pending_input_version += 1
                started_pending = self._pop_next_pending_input(cell)
                if started_pending is None:
                    snapshot = self._pending_input_snapshot_from_cell(cell)
                    started = False
                else:
                    started_version = cell.pending_input_version
                    self._start_pending_input_run(cell, started_pending)
                    snapshot = self._pending_input_snapshot_from_cell(cell)
                    started = started_pending.id == pending.id
                    broadcast_queue_changed = True
            else:
                self._start_pending_input_run(cell, pending)
                snapshot = self._pending_input_snapshot_from_cell(cell)
                started = True
        if started_pending is not None:
            await self._broadcast_pending_input_started(
                cell,
                started_pending,
                started_version if started_version is not None else snapshot.version,
            )
        elif started:
            await self._broadcast_pending_input_started(cell, pending, snapshot.version)
        if broadcast_queue_changed:
            await self._broadcast_pending_input_changed(cell, "drained")
        elif not started:
            await self._broadcast_pending_input_changed(cell, "added")
        return PendingInputSubmitResult(
            accepted=True,
            started=started,
            pending_input=pending,
            snapshot=snapshot,
        )

    def _create_pending_input(
        self,
        cell: ThreadCell,
        content: str,
        *,
        source: Literal["user_input", "choice_submit", "avatar"],
        priority: Literal["choice_response", "user_message"],
        metadata: dict[str, Any],
    ) -> PendingInputDTO:
        """创建队列项 DTO。

        sequence 是 cell 内单调递增序号，用于同优先级 FIFO；metadata 会过滤 None，
        保持 WS payload 精简。
        """
        cell.pending_input_sequence += 1
        now = _now_ms_int()
        return PendingInputDTO(
            id=f"pin-{secrets.token_hex(6)}",
            thread_id=cell.thread_id,
            source=source,
            priority=priority,
            content=content,
            preview=_pending_input_preview(content),
            status="queued",
            created_at_ms=now,
            updated_at_ms=now,
            sequence=cell.pending_input_sequence,
            metadata={key: value for key, value in metadata.items() if value is not None},
        )

    def _start_pending_input_run(
        self,
        cell: ThreadCell,
        pending: PendingInputDTO,
    ) -> None:
        """把一个 pending input 启动为 root mailbox run 观察任务。

        输入来自已出队或即时提交的 PendingInputDTO；输出是写入 cell.current_run_task
        的 asyncio task。task 完成后统一回到 _on_pending_run_done drain 下一项。

        状态边界：启动后的输入离开 pending_inputs 列表，由 current_run_task 表示
        active run；取消、失败和后续 drain 都通过 done callback 收口。

        host-dispatch-consolidation #4：drain 改调 ``cell.host_dispatcher.submit``
        （QUEUE 阻塞到本轮 run 完成），由 HostDispatcher 统一持有 future FIFO +
        AgentManager + DeliverSink，不再自造第二套投递胶水。
        """
        task = self._start_host_dispatcher_run(
            cell,
            pending.content,
            metadata=dict(pending.metadata),
            task_name=f"web-run-once-{cell.thread_id}",
        )

        def _done_callback(
            finished: asyncio.Task[Result],
            *,
            _cell: ThreadCell = cell,
            _task: asyncio.Task[Result] = task,
        ) -> None:
            self._on_pending_run_done(_cell, _task, finished)

        task.add_done_callback(_done_callback)

    def _start_host_dispatcher_run(
        self,
        cell: ThreadCell,
        content: str,
        *,
        metadata: dict[str, Any],
        task_name: str,
    ) -> asyncio.Task[Result]:
        """投递 root mailbox run，并返回等待该 run Result 的 task（host-dispatch-consolidation #4）。

        内部把 ``cell.host_dispatcher.run_text(...)`` 包成 asyncio task，
        赋给 ``cell.current_run_task``。run_text 是 async（阻塞到本轮 run 完成），
        HostDispatcher 内部懒启动 AgentManager + boot_root，并经 DeliverSink 回填 future。
        """
        if self._is_root_agent_registry_closed(cell):
            cell.pending_input_drain_block_reason = ROOT_AGENT_REGISTRY_CLOSED
            raise PendingInputOperationError(
                ROOT_AGENT_REGISTRY_CLOSED,
                "root agent registry is closed",
            )
        task = asyncio.create_task(
            self._run_via_host_dispatcher(
                cell,
                content,
                metadata=metadata,
            ),
            name=task_name,
        )
        cell.current_run_task = task
        return task

    async def _run_via_host_dispatcher(
        self,
        cell: ThreadCell,
        content: str,
        *,
        metadata: dict[str, Any],
    ) -> Result:
        """通过 HostDispatcher 投递一次 QUEUE run 并返回 Result。

        ``metadata`` 仅保留与 mailbox run 相关的键（reasoning_effort / attachments /
        references / continue_from_last_user_message 等），透传给 HostDispatcher；
        run_text 阻塞到本轮 run 完成后返回 Runner 的真实 Result，供 pending drain
        按 status / metadata 做失败停止和 steer_undelivered 回投。
        """
        dispatcher = _host_dispatcher_for(cell)
        filtered = {key: value for key, value in metadata.items() if value is not None}
        attachments = (
            filtered.get("attachments") if isinstance(filtered.get("attachments"), list) else None
        )
        references = (
            filtered.get("references") if isinstance(filtered.get("references"), list) else None
        )
        result = await dispatcher.run_text(
            content,
            attachments=attachments,
            references=references,
            metadata=filtered,
            repost_undelivered=False,
        )
        await cell.adapter.render_result(result)
        return result

    def _on_pending_run_done(
        self,
        cell: ThreadCell,
        task: asyncio.Task[Any],
        finished: asyncio.Task[Any],
    ) -> None:
        """run_once 完成回调入口。

        asyncio done callback 不能直接 await，本函数只负责创建 drain task，并把它纳入
        ThreadManager 关闭时要等待/取消的后台任务集合。
        """
        with suppress(RuntimeError):
            drain_task = asyncio.create_task(
                self._handle_pending_run_done(cell, task, finished),
                name=f"pending-input-drain-{cell.thread_id}",
            )
            self._pending_drain_tasks.add(drain_task)
            drain_task.add_done_callback(self._pending_drain_tasks.discard)

    async def _handle_pending_run_done(
        self,
        cell: ThreadCell,
        task: asyncio.Task[Any],
        finished: asyncio.Task[Any],
    ) -> None:
        """处理 run 完成后的队列 drain。

        drain 策略（run-end-reason-fix，用户确认）：
        - 纯 COMPLETE（自然完成）→ 自动 drain 队列下一条。
        - MAX_TURNS / ERROR / INTERRUPT / EVICTED → 不自动 drain，队列保留，等用户
          手动发下一条触发。这把控制权交还用户，避免在预算耗尽 / 出错 / 被打断的
          runtime 上继续自动消费输入。

        死锁修复（run-end-reason-fix）：旧实现在 registry 已关闭时设
        ``pending_input_drain_block_reason = ROOT_AGENT_REGISTRY_CLOSED``，但清除它的
        唯一入口（``interrupt_agent_tree``）需要 ``_has_active_run`` 才能进入——idle
        状态下永远进不去，形成不可恢复的死锁。现在 run 结束收口时主动
        ``reset_for_reuse`` 拆掉旧 agent 树（消除残留的 closed registry），并在纯
        COMPLETE 时清除已有的 ``ROOT_AGENT_REGISTRY_CLOSED`` block reason——清除不再
        依赖 active run。
        """
        # 用 compute_run_end_reason 精确区分结束原因（错误分类器单一真源）。
        # 仅纯 COMPLETE 才 drain；MAX_TURNS / ERROR / INTERRUPT 都不 drain。
        reason = "normal"
        result: Any | None = None
        run_end_reason_int = 0
        try:
            result = finished.result()
            run_end_reason_int = int(compute_run_end_reason(result))
            reason = "normal" if run_end_reason_int == int(RunEndReason.COMPLETE) else "terminal"
        except asyncio.CancelledError:
            reason = "terminal"
        except Exception:
            logger.exception("pending input run failed for thread=%s", cell.thread_id)
            reason = "terminal"

        should_drain = reason == "normal"
        undelivered = self._pop_steer_undelivered(result)
        requeued_undelivered = False
        next_input: PendingInputDTO | None = None
        version: int | None = None
        async with cell.pending_input_lock:
            if cell.current_run_task is not task:
                return
            cell.current_run_task = None
            if undelivered:
                self._prepend_undelivered_pending_inputs(cell, undelivered)
                requeued_undelivered = True
            cell.pending_input_send_now_claims.clear()
            if cell.status is ThreadCellStatus.EVICTING:
                should_drain = False
            if cell.pending_input_drain_block_reason is not None:
                should_drain = False
            if self._is_root_agent_registry_closed(cell):
                # 死锁修复：run 已结束但 registry 残留关闭状态。主动 reset_for_reuse
                # 拆掉旧 agent 树（teardown_root 清掉 _root_agent_id + cells + registry），
                # 下次 boot_or_attach 会懒启动全新的未关闭 agent 树。
                # 这把"清 block reason"从"依赖 active run 的 interrupt 路径"解耦成
                # "run 结束即清"的独立操作，消灭 idle 状态下不可恢复的死锁。
                dispatcher = _host_dispatcher_for(cell)
                await dispatcher.reset_for_reuse()
                # reset_for_reuse 后 agent_manager=None，registry 不再残留 closed；
                # 清掉 block reason（仅 ROOT_AGENT_REGISTRY_CLOSED，保留 evict /
                # runtime_refresh_failed 等其他阻断原因）。
                if cell.pending_input_drain_block_reason == ROOT_AGENT_REGISTRY_CLOSED:
                    cell.pending_input_drain_block_reason = None
                # 拆树后 registry 不再 closed，原 block 条件消失——让 should_drain
                # 回归 reason 判断（纯 COMPLETE 仍可 drain）。
                should_drain = reason == "normal"
            if not should_drain:
                version = cell.pending_input_version
            else:
                next_input = self._pop_next_pending_input(cell)
                if next_input is not None:
                    version = cell.pending_input_version
                    self._start_pending_input_run(cell, next_input)
        if next_input is not None:
            await self._broadcast_pending_input_started(
                cell,
                next_input,
                version if version is not None else cell.pending_input_version,
            )
            await self._broadcast_pending_input_changed(cell, "drained")
        elif requeued_undelivered:
            await self._broadcast_pending_input_changed(cell, "steer_undelivered")

    @staticmethod
    def _pop_steer_undelivered(result: Any | None) -> list[tuple[str, str | None]]:
        """取出 Runner 收尾期未注入的 steer 项。

        输入为 Result；输出为 ``(text, pending_input_id)`` 列表，按 Runner 写入顺序。
        ``pending_input_id`` 为 None 表示该项无身份（旧路径或外部直调），回投时新建 pin id。
        """
        metadata = getattr(result, "metadata", None)
        if not isinstance(metadata, dict):
            return []
        leftovers = metadata.pop("steer_undelivered", None)
        if not isinstance(leftovers, list):
            return []
        out: list[tuple[str, str | None]] = []
        for item in leftovers:
            if isinstance(item, dict):
                text = str(item.get("text", "")).strip()
                raw_id = item.get("pending_input_id")
                pid = raw_id if isinstance(raw_id, str) and raw_id else None
                if text:
                    out.append((text, pid))
            elif isinstance(item, str) and item.strip():
                # 兼容旧 trace（理论上 steer 链路改造后不会出现纯 str 条目）
                out.append((item.strip(), None))
        return out

    def _prepend_undelivered_pending_inputs(
        self,
        cell: ThreadCell,
        items: list[tuple[str, str | None]],
    ) -> None:
        """把未注入的 steer 项放回队列头。

        输入为 ``(text, pending_input_id)`` 列表；按 id 精确复用原 claim（保留原
        pending id、metadata 等用户可见状态），找不到 id 时新建 pin id 保守回投。
        """
        if not items:
            return
        ordered = self._ordered_pending_inputs(cell.pending_inputs)
        claims = self._send_now_claims_for_undelivered(cell, items)
        first_sequence = min(
            (item.sequence for item in ordered),
            default=cell.pending_input_sequence + 1,
        )
        start_sequence = first_sequence - len(items)
        now = _now_ms_int()
        prepended = [
            (
                claim.model_copy(
                    update={
                        "status": "queued",
                        "updated_at_ms": now,
                        "sequence": start_sequence + index,
                    }
                )
                if claim is not None
                else PendingInputDTO(
                    id=f"pin-{secrets.token_hex(6)}",
                    thread_id=cell.thread_id,
                    source="user_input",
                    priority="user_message",
                    content=text,
                    preview=_pending_input_preview(text),
                    status="queued",
                    created_at_ms=now,
                    updated_at_ms=now,
                    sequence=start_sequence + index,
                    metadata={},
                )
            )
            for index, ((text, _pid), claim) in enumerate(zip(items, claims, strict=True))
        ]
        cell.pending_inputs = self._ordered_pending_inputs([*prepended, *ordered])
        cell.pending_input_version += 1

    @staticmethod
    def _send_now_claims_for_undelivered(
        cell: ThreadCell,
        items: list[tuple[str, str | None]],
    ) -> list[PendingInputDTO | None]:
        """按 undelivered 项的 pending_input_id 精确返回可复用的 send-now claim。

        缺 id 或 id 在 claims 里找不到 → 该位返回 None，调用方新建 pin id。
        """
        claims = cell.pending_input_send_now_claims
        if not claims:
            return [None for _ in items]
        out: list[PendingInputDTO | None] = [None for _ in items]
        unmatched = list(claims)
        for index, (_text, pid) in enumerate(items):
            if pid is None:
                continue
            match_index = next(
                (
                    pos
                    for pos, claim in enumerate(unmatched)
                    if str(getattr(claim, "id", "")) == pid
                ),
                None,
            )
            if match_index is None:
                continue
            out[index] = unmatched.pop(match_index)
        return out

    def _pop_next_pending_input(self, cell: ThreadCell) -> PendingInputDTO | None:
        """从队列头取出下一条输入并递增 snapshot version。

        调用方必须已持有 pending_input_lock；返回值已标记为 starting，表示所有权
        从队列列表转移到 current_run_task。
        """
        ordered = self._ordered_pending_inputs(cell.pending_inputs)
        if not ordered:
            return None
        next_input = ordered[0]
        cell.pending_inputs = ordered[1:]
        cell.pending_input_version += 1
        return next_input.model_copy(update={"status": "starting", "updated_at_ms": _now_ms_int()})

    @staticmethod
    def _has_active_run(cell: ThreadCell) -> bool:
        """判断当前 cell 是否已有正在执行的普通 run。"""
        return cell.current_run_task is not None

    @staticmethod
    def _ensure_pending_input_can_send_now(pending: PendingInputDTO) -> None:
        """校验 pending input 是否能安全插入当前 active run。"""
        unsupported = set(pending.metadata) - _SEND_NOW_ALLOWED_METADATA_KEYS
        if unsupported:
            raise PendingInputOperationError(
                PENDING_INPUT_NOT_INJECTABLE,
                "pending input has structured runtime metadata",
            )

    @staticmethod
    def _is_root_agent_registry_closed(cell: ThreadCell) -> bool:
        """判断 root agent 生命周期账本是否已被 tree interrupt 关闭。

        registry 归 HostDispatcher 的 AgentManager 持有（懒启动）。host_dispatcher
        未启动（无 agent_manager）时视为未关闭。
        """
        dispatcher = _host_dispatcher_for(cell)
        manager = getattr(dispatcher, "agent_manager", None)
        if manager is None:
            return False
        registry = getattr(manager, "registry", None)
        if registry is None:
            return False
        return bool(registry.is_closed)

    @staticmethod
    def _ordered_pending_inputs(items: list[PendingInputDTO]) -> list[PendingInputDTO]:
        """按优先级和 sequence 生成稳定队列顺序。"""
        priority_rank = {"choice_response": 0, "user_message": 1}
        return sorted(items, key=lambda item: (priority_rank[item.priority], item.sequence))

    def _pending_input_snapshot_from_cell(self, cell: ThreadCell) -> PendingInputSnapshotFrame:
        """从 cell 内存状态构造 S2C 队列快照帧。

        这是后端队列对前端的完整投影；items 顺序由 _ordered_pending_inputs 统一生成。
        """
        return PendingInputSnapshotFrame(
            timestamp_ms=_now_ms_int(),
            thread_id=cell.thread_id,
            items=self._ordered_pending_inputs(cell.pending_inputs),
            max_items=MAX_PENDING_INPUTS,
            active_run_id=None,
            version=cell.pending_input_version,
        )

    async def _broadcast_pending_input_changed(
        self,
        cell: ThreadCell,
        reason: Literal[
            "added",
            "updated",
            "removed",
            "reordered",
            "drained",
            "cleared",
            "sent_now",
            "steer_undelivered",
        ],
    ) -> None:
        """广播队列内容变化。

        reason 描述变化类型；payload 携带完整 items 快照，前端以服务端状态覆盖本地状态。
        """
        snapshot = self._pending_input_snapshot_from_cell(cell)
        frame = PendingInputChangedFrame(
            timestamp_ms=_now_ms_int(),
            thread_id=cell.thread_id,
            items=snapshot.items,
            max_items=snapshot.max_items,
            reason=reason,
            active_run_id=snapshot.active_run_id,
            version=snapshot.version,
        )
        await cell.adapter._safe_send_json(frame.model_dump())

    async def _broadcast_pending_input_steered(
        self,
        cell: ThreadCell,
        pending_input: PendingInputDTO,
        version: int,
        *,
        active_run_id: str | None,
        turn: int | None = None,
    ) -> None:
        """广播 send-now 已写入当前活跃 run 的 steer buffer。"""
        now = _now_ms_int()
        frame = PendingInputSteeredFrame(
            timestamp_ms=now,
            thread_id=cell.thread_id,
            pending_input_id=pending_input.id,
            pending_input=pending_input,
            active_run_id=active_run_id,
            run_id=active_run_id or "",
            turn=turn,
            version=version,
        )
        await cell.adapter._safe_send_json(frame.model_dump())

    async def _handle_runtime_event(self, cell: ThreadCell, event: Event) -> None:
        """处理 runtime 事件中的 Web pending input 补偿逻辑。

        消账主键是 ``pending_input_id``（来自 Runner ``steer.injected`` 事件 payload）。
        事件缺 id 或 id 在 claims 里找不到时记 error 日志、不消账——claim 随 run done 的
        ``claims.clear()`` 自然清掉，不积压；不盲弹 pop(0) 以免错账。
        """
        if event.kind != "steer.injected":
            return
        raw_id = (event.payload or {}).get("pending_input_id")
        if not isinstance(raw_id, str) or not raw_id:
            logger.error(
                "steer.injected missing pending_input_id; claim not settled "
                "thread=%s run_id=%s turn=%s content_length=%s",
                cell.thread_id,
                event.run_id,
                event.turn,
                (event.payload or {}).get("content_length"),
            )
            return
        async with cell.pending_input_lock:
            claim = self._pop_injected_send_now_claim(cell, raw_id)
            version = cell.pending_input_version
        if claim is None:
            return
        await self._broadcast_pending_input_steered(
            cell,
            claim,
            version,
            active_run_id=event.run_id,
            turn=event.turn,
        )

    @staticmethod
    def _pop_injected_send_now_claim(
        cell: ThreadCell,
        pending_input_id: str,
    ) -> PendingInputDTO | None:
        """按 steer.injected 事件的 pending_input_id 精确匹配并取走一个 send-now claim。

        匹配失败（事件 id 与现有 claim 对不上）记 error 日志、返回 None，不盲弹。
        """
        claims = cell.pending_input_send_now_claims
        for index, claim in enumerate(claims):
            if str(getattr(claim, "id", "")) == pending_input_id:
                return cast(PendingInputDTO, claims.pop(index))
        logger.error(
            "steer.injected pending_input_id=%s not found in %d claims; claim not settled",
            pending_input_id,
            len(claims),
        )
        return None

    async def _broadcast_pending_input_started(
        self,
        cell: ThreadCell,
        pending: PendingInputDTO,
        version: int,
    ) -> None:
        """广播某条 pending input 已启动。

        started 帧用于让前端清除本地队列项、用最终 content 生成聊天气泡并关联
        运行态；随后 changed/drained 帧会补齐完整队列快照。
        """
        started_pending = pending.model_copy(
            update={"status": "starting", "updated_at_ms": _now_ms_int()}
        )
        frame = PendingInputStartedFrame(
            timestamp_ms=_now_ms_int(),
            thread_id=cell.thread_id,
            pending_input_id=started_pending.id,
            pending_input=started_pending,
            run_id=str(started_pending.metadata.get("avatar_run_id") or ""),
            version=version,
        )
        await cell.adapter._safe_send_json(frame.model_dump())

    async def _persist_thread_metadata(self, meta: ThreadMetadata) -> ThreadMetadata:
        """串行更新现存 metadata，并阻止删除后的陈旧写入复活 thread。"""
        async with self._metadata_mutation_lock(meta.id):
            current = await asyncio.to_thread(
                read_thread_metadata,
                self._home,
                meta.id,
            )
            if current is None:
                raise KeyError(f"thread not found: {meta.id}")
            await asyncio.to_thread(write_thread_metadata, self._home, meta)
        async with self._lock:
            cell = self._cells.get(meta.id)
            if cell is not None:
                cell.metadata = meta
        return meta

    async def _persist_new_thread_metadata(self, meta: ThreadMetadata) -> None:
        """串行创建新 metadata，并拒绝覆盖已经存在的同 ID thread。"""
        async with self._metadata_mutation_lock(meta.id):
            current = await asyncio.to_thread(
                read_thread_metadata,
                self._home,
                meta.id,
            )
            if current is not None:
                raise FileExistsError(f"thread metadata already exists: {meta.id}")
            await asyncio.to_thread(write_thread_metadata, self._home, meta)

    async def _update_thread_metadata(
        self,
        meta: ThreadMetadata,
        *,
        name: str | None = None,
        preset_id: str | None = None,
        updated_at: float | None = None,
        is_pinned: bool | None = None,
        is_archived: bool | None = None,
    ) -> ThreadMetadata:
        """从当前快照复制局部字段，并通过统一入口持久化更新。"""
        updates: dict[str, object] = {}
        if name is not None:
            updates["name"] = name
        if preset_id is not None:
            updates["preset_id"] = preset_id
        if updated_at is not None:
            updates["updated_at"] = updated_at
        if is_pinned is not None:
            updates["is_pinned"] = is_pinned
        if is_archived is not None:
            updates["is_archived"] = is_archived
        return await self._persist_thread_metadata(meta.model_copy(update=updates))

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
        return await self._update_thread_metadata(
            meta,
            name=new_name.strip(),
            updated_at=_now(),
        )

    async def pin_thread(self, thread_id: str, is_pinned: bool) -> ThreadMetadata:
        """置顶/取消置顶 thread。"""
        meta = await asyncio.to_thread(read_thread_metadata, self._home, thread_id)
        if meta is None:
            raise KeyError(f"thread not found: {thread_id}")
        return await self._update_thread_metadata(meta, is_pinned=is_pinned)

    async def set_archived(self, thread_id: str, is_archived: bool) -> ThreadMetadata:
        """归档/取消归档 thread（v10 claude-session-rename-archive-metadata-source）。

        与 ``pin_thread`` 同款：read → model_copy → atomic write → 同步 cell.metadata。
        归档不算"活跃"，所以**不**更新 ``updated_at``（保持与 pin 一致）。

        失败：thread 不存在抛 :class:`KeyError`。
        """
        meta = await asyncio.to_thread(read_thread_metadata, self._home, thread_id)
        if meta is None:
            raise KeyError(f"thread not found: {thread_id}")
        return await self._update_thread_metadata(meta, is_archived=is_archived)

    async def update_thread_preset(self, thread_id: str, preset_id: str) -> ThreadMetadata:
        """更新 Generic Chat thread 的 preset，并在可行时刷新活跃 runtime。

        当前 run 已经开始时不热切换 provider；metadata 先落盘，下一次发送前
        ``ensure_cell_runtime_preset_current`` 会重建 runtime。

        回滚边界：runtime 重建失败时恢复旧 metadata，并解除本次失败留下的 drain
        阻塞标记，让调用方看到提交失败后的可重试状态。
        """
        normalized_preset_id = preset_id.strip()
        if not normalized_preset_id:
            raise ValueError("preset_id must not be empty")
        runtime = self._model_catalog_manager.resolve_runtime(
            self._cfg.model,
            preset_id=normalized_preset_id,
        )
        self._model_catalog_manager.resolve_credential(runtime)

        meta = await asyncio.to_thread(read_thread_metadata, self._home, thread_id)
        if meta is None:
            raise KeyError(f"thread not found: {thread_id}")
        if meta.backend_kind != "generic_chat":
            raise ValueError("preset can only be changed for generic_chat threads")

        updated = await self._update_thread_metadata(
            meta,
            preset_id=normalized_preset_id,
            updated_at=_now(),
        )
        refreshed = await self.ensure_cell_runtime_preset_current(thread_id)
        if not refreshed:
            await self._persist_thread_metadata(meta)
            async with self._lock:
                rollback_cell = self._cells.get(thread_id)
                if (
                    rollback_cell is not None
                    and rollback_cell.pending_input_drain_block_reason == "runtime_refresh_failed"
                ):
                    rollback_cell.pending_input_drain_block_reason = None
            raise ThreadPresetRefreshError(
                f"failed to refresh runtime for preset_id: {normalized_preset_id}"
            )
        return updated

    async def ensure_cell_runtime_preset_current(self, thread_id: str) -> bool:
        """让已启动 cell 的 runtime preset 与 metadata 保持一致。

        返回 True 表示当前提交路径可以继续；返回 False 表示 runtime 重建失败，
        pending_input_drain_block_reason 会写为 ``runtime_refresh_failed``，done callback
        停止消费队列，等待后续 preset 回滚或刷新成功。
        """
        async with self._lock:
            cell = self._cells.get(thread_id)
        if cell is None:
            return True
        async with cell.preset_refresh_lock:
            if cell.runtime_preset_id == cell.metadata.preset_id:
                if cell.pending_input_drain_block_reason == "runtime_refresh_failed":
                    cell.pending_input_drain_block_reason = None
                return True
            run_task = cell.current_run_task
            if run_task is not None and not run_task.done():
                return True

            old_runtime = cell.runtime
            try:
                runtime, host_dispatcher = await self._runtime_factory(
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
                # refresh 失败时，队列保留在内存中；drain 停止闸门阻止旧 runtime
                # 继续消费这些输入，REST 回滚成功后再清除该标记。
                cell.pending_input_drain_block_reason = "runtime_refresh_failed"
                return False
            cell.runtime = runtime
            cell.host_dispatcher = host_dispatcher
            cell.runtime_preset_id = cell.metadata.preset_id
            if cell.pending_input_drain_block_reason == "runtime_refresh_failed":
                cell.pending_input_drain_block_reason = None
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
            await self.evict_cell(
                thread_id,
                reason="manual_stop",
                notify_ws=True,
                drain_block_reason="deleted",
            )

        async with self._metadata_mutation_lock(thread_id):
            current = await asyncio.to_thread(
                read_thread_metadata,
                self._home,
                thread_id,
            )
            if current is None:
                return

            await asyncio.to_thread(
                delete_thread_metadata_dir,
                self._home,
                thread_id,
            )
            metadata_path = thread_metadata_path(self._home, thread_id)
            if await asyncio.to_thread(metadata_path.exists):
                raise OSError(f"thread metadata delete did not commit: {thread_id}")

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

            await self._cleanup_deleted_thread_permissions(thread_id)

        # v0.1.5 不删 session backend 历史（需要 cfg.session.backend 路径推算）；
        # 留给 web-app-shell 任务在装配时按需 wire 一个 history_cleaner closure。
        # keep_history=True 时显式跳过；False 时也仅记 TODO。
        if not keep_history:
            logger.info(
                "delete_thread(%s): metadata removed; session history cleanup is "
                "deferred to web-app-shell task (v0.1.5 does not wire backend deletion)",
                thread_id,
            )

    async def retry_permissions_cleanup(self) -> tuple[str, ...]:
        """重试队列中的本子清理，并返回本轮成功删除的 thread id。"""
        completed: list[str] = []
        for thread_id in tuple(self._permissions_cleanup_retry):
            if await self._try_delete_permissions(thread_id):
                completed.append(thread_id)
        return tuple(completed)

    async def _cleanup_deleted_thread_permissions(self, thread_id: str) -> None:
        """主状态删除后清理本子；失败进入可观测重试队列。"""
        if await self._try_delete_permissions(thread_id):
            return
        task = asyncio.create_task(
            self._retry_permissions_cleanup_once(),
            name=f"thread-permissions-cleanup:{thread_id}",
        )
        self._permissions_cleanup_tasks.add(task)
        task.add_done_callback(self._discard_permissions_cleanup_task)

    async def _try_delete_permissions(self, thread_id: str) -> bool:
        """尝试删除单 thread 本子，并记录成功或失败 audit。"""
        manager = self._permissions_manager
        if manager is None:
            return True
        try:
            await manager.delete_thread(thread_id)
        except Exception as exc:
            self._permissions_cleanup_retry.add(thread_id)
            log_network_exception(
                "hosts.web.threads.manager",
                "thread_permissions_cleanup_failed",
                exc,
                thread_id=thread_id,
            )
            return False
        self._permissions_cleanup_retry.discard(thread_id)
        log_network_event(
            "hosts.web.threads.manager",
            "thread_permissions_cleanup_completed",
            thread_id=thread_id,
        )
        return True

    async def _retry_permissions_cleanup_once(self) -> None:
        """在当前事件循环下一拍消费一次 permissions 清理重试队列。"""
        await asyncio.sleep(0)
        await self.retry_permissions_cleanup()

    def _discard_permissions_cleanup_task(self, task: asyncio.Task[None]) -> None:
        """回收已结束的 permissions 清理后台任务。"""
        self._permissions_cleanup_tasks.discard(task)
        with suppress(asyncio.CancelledError, Exception):
            task.result()

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

            # 等待 boot_lock 期间，metadata 可能被 fork 回滚或 delete_thread 删除。
            # 锁内重读保证 build 使用当前持久化真值，避免旧快照复活已清理的 thread。
            meta = await asyncio.to_thread(
                read_thread_metadata,
                self._home,
                thread_id,
            )
            if meta is None:
                raise KeyError(f"thread metadata not found: {thread_id}")

            # 5. 真正的 build
            cell = await self._build_cell(meta)

            # host-dispatch-consolidation #4：root agent 装配（AgentCell + agent_loop +
            # AgentManager + DeliverSink）已下沉到 HostDispatcher，由 runtime_factory
            # 装配时挂到 cell，首次 submit 时 ensure_started 懒启动。boot 不再需要显式 init。

            # 6. 注册到 dict
            async with self._lock:
                self._cells[thread_id] = cell
            return cell

    async def build_ephemeral_session_cell(
        self,
        *,
        session_id: str,
        preset_id: str,
    ) -> Any:
        """为非 thread metadata session 临时装配 runtime cell。

        用于 cron run history 人工对话。该入口复用同一套 runtime factory，
        但不会读取或写入 `.kongming/web/threads/<id>/metadata.json`，也不会
        注册到 `_cells`，生命周期由调用方负责关闭。
        """
        fanout = WebSocketFanout()
        adapter = WebHostAdapter(
            ws=fanout,
        )
        sinks: list[Any] = [WSEventSink(fanout, thread_id=session_id)]
        runtime, host_dispatcher = await self._runtime_factory(
            session_id,
            preset_id,
            adapter,
            sinks,
        )
        return _EphemeralSessionCell(
            session_id=session_id,
            preset_id=preset_id,
            runtime=runtime,
            host_dispatcher=host_dispatcher,
            adapter=adapter,
            event_sinks=sinks,
        )

    async def close_ephemeral_session_cell(
        self,
        cell: Any,
        *,
        reason: str = "session_close",
    ) -> None:
        """关闭非 metadata 临时 cell，并取消归属该 session 的待审批。"""
        thread_id = getattr(cell, "thread_id", None)
        if isinstance(thread_id, str) and thread_id:
            self._cancel_pending_approvals_for_thread(thread_id, reason=reason)

        await self._cancel_current_run_task(cell)
        await self._aclose_host_dispatcher(cell)

        adapter = getattr(cell, "adapter", None)
        if adapter is not None:
            with suppress(Exception):
                await adapter.close()

        runtime = getattr(cell, "runtime", None)
        if runtime is not None:
            # runtime 是主要生命周期资源，关闭时同步释放 provider 与 session。
            with suppress(Exception):
                await runtime.aclose()

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
        3. 调用 runtime_factory 拿到 (SessionEngine, HostDispatcher)
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
        )
        cell_ref: dict[str, ThreadCell] = {}
        ws_sink = WSEventSink(fanout, thread_id=meta.id)
        # usage-token-v2-bigbang: UsagePersistSink 已删除——v2 manager 是无状态门面，
        # 不接受外部 push token。usage 事件无需持久化 sink；前端通过
        # GET /threads/<tid>/usage 端点拉取派生结果。

        status_sink = ThreadStatusEventSink(meta.id)
        pending_steer_sink = _PendingInputSteerEventSink(self, lambda: cell_ref.get("cell"))
        sinks: list[Any] = [ws_sink, status_sink, pending_steer_sink]
        runtime, host_dispatcher = await self._runtime_factory(
            meta.id,
            meta.preset_id,
            adapter,
            sinks,
        )

        cell = ThreadCell(
            thread_id=meta.id,
            metadata=meta,
            runtime=runtime,
            host_dispatcher=host_dispatcher,
            adapter=adapter,
            event_sinks=sinks,
            runtime_preset_id=meta.preset_id,
        )
        cell_ref["cell"] = cell
        return cell

    # ------------------------------------------------------------------
    # agent-tree 转发入口（HostDispatcher 为唯一 owner）
    # ------------------------------------------------------------------
    #
    # root agent 装配（AgentCell + agent_loop + AgentManager + DeliverSink +
    # future FIFO）由 HostDispatcher 持有。ThreadManager 只在需要时取 cell 上的
    # host_dispatcher 并调用公开方法。

    async def interrupt_agent_tree(
        self,
        thread_id: str,
        *,
        reason: str = "user_interrupt",
    ) -> bool:
        """打断当前 root agent 树（host-dispatch-consolidation #4）。

        旧语义：cancel_subtree + bump_epoch + purge 旧世代内部 mail（自己装配的
        registry / agent_loop）。新语义：交给 ``cell.host_dispatcher.interrupt()``
        （内部调 AgentManager.interrupt，让 runner 收口为 cancelled Result）。
        epoch / registry / purge 的额外清理已由 HostDispatcher / AgentManager 收口，
        ThreadManager 不再持有第二套生命周期账本。

        Returns:
            True=已执行 interrupt；False=cell 不存在或当前没有活跃 root work。
        """
        async with self._lock:
            cell = self._cells.get(thread_id)
        if cell is None:
            return False
        dispatcher = _host_dispatcher_for(cell)
        async with cell.pending_input_lock:
            if not self._has_active_run(cell) and not dispatcher.has_active_work():
                return False
            await dispatcher.interrupt()
            await dispatcher.reset_for_reuse()
            if cell.pending_input_drain_block_reason == ROOT_AGENT_REGISTRY_CLOSED:
                cell.pending_input_drain_block_reason = None
        logger.info(
            "interrupt_agent_tree thread=%s reason=%s",
            thread_id,
            reason,
        )
        return True

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
        drain_block_reason: str | None = None,
    ) -> None:
        """主动回收一个 cell。

        步骤：
        1. 标记 status = "evicting"（防止再 boot）
        2. 取 cell 出 dict（防止其他路径再用）
        3. 推 ``CellEvictedFrame``（``notify_ws=False`` 时跳过；shutdown 路径用）
        4. cancel ``current_run_task``（带 5s 超时；超时直接 cancel）
        5. 取消 ApprovalManager pending，再关 adapter
        6. 关 runtime（``runtime.aclose()`` 释放 httpx pool）

        幂等：thread_id 不在 dict 时直接返回；不抛。

        队列边界：evict 会写入 pending_input_drain_block_reason，正在运行的 task
        即使随后触发 done callback，也会看到该闸门并停止 drain。
        """
        cell = await self._pop_cell_for_eviction(
            thread_id,
            reason=reason,
            drain_block_reason=drain_block_reason,
        )
        if cell is None:
            return

        await self._finish_evicted_cell(
            cell,
            reason=reason,
            message=message,
            notify_ws=notify_ws,
        )

    async def _pop_cell_for_eviction(
        self,
        thread_id: str,
        *,
        reason: EvictReason,
        drain_block_reason: str | None = None,
    ) -> ThreadCell | None:
        """从活跃表移出 cell，并写入统一的队列 drain 阻断状态。"""
        async with self._lock:
            cell = self._cells.pop(thread_id, None)
            self._boot_locks.pop(thread_id, None)
        if cell is not None:
            self._mark_cell_evicting(
                cell,
                reason=reason,
                drain_block_reason=drain_block_reason,
            )
        return cell

    @staticmethod
    def _set_cell_status(cell: ThreadCell, status: ThreadCellStatus) -> None:
        """cell.status 的唯一写入入口。

        实际只有 ``EVICTING`` 经此写入——它是 task 之外的独立生命周期事实；
        ``RUNNING`` / ``AWAITING_APPROVAL`` / ``IDLE`` 一律由 :meth:`_effective_status`
        现算，不再落字段。集中写入便于将来加迁移校验 / 日志而不散点。
        """
        cell.status = status

    def _effective_status(self, cell: ThreadCell) -> ThreadCellStatus:
        """cell 当前状态的唯一现算入口。

        优先级：``EVICTING``（粘性，唯一落字段）> ``AWAITING_APPROVAL``（看 pending
        approval）> ``RUNNING``（看 ``current_run_task``）> ``IDLE``。投影与 evict
        判定都只调本方法，保证"是否在跑/待审批"与事实真源永不漂移。
        """
        if cell.status is ThreadCellStatus.EVICTING:
            return ThreadCellStatus.EVICTING
        if self._has_pending_approval(cell.thread_id):
            return ThreadCellStatus.AWAITING_APPROVAL
        if cell.current_run_task is not None:
            return ThreadCellStatus.RUNNING
        return ThreadCellStatus.IDLE

    def _mark_cell_evicting(
        self,
        cell: ThreadCell,
        *,
        reason: EvictReason,
        drain_block_reason: str | None = None,
    ) -> None:
        """统一写入 evicting 状态和 pending input 停止原因。"""
        self._set_cell_status(cell, ThreadCellStatus.EVICTING)
        cell.pending_input_drain_block_reason = drain_block_reason or (
            "shutdown" if reason == "server_shutdown" else "evicted"
        )

    async def _finish_evicted_cell(
        self,
        cell: ThreadCell,
        *,
        reason: EvictReason,
        message: str | None = None,
        notify_ws: bool = True,
    ) -> None:
        """关闭已从活跃表移出的 cell。"""
        thread_id = cell.thread_id

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
        await self._cancel_current_run_task(cell)

        # 2b. root agent 树（agent_loop / AgentManager / registry / futures）
        # 由 HostDispatcher 持有；cell evict 是树销毁场景，直接关闭该生命周期 owner。
        await self._aclose_host_dispatcher(cell)

        # 3. 取消 manager 侧待审批，并关闭 adapter
        self._cancel_pending_approvals_for_thread(thread_id)
        with suppress(Exception):
            await cell.adapter.close()

        # 4. 关闭 runtime 主资源；pending 审批已由 Manager 在上一步取消。
        with suppress(Exception):
            await cell.runtime.aclose()

    @staticmethod
    async def _cancel_current_run_task(cell: Any) -> None:
        """取消 cell 当前 run task。

        输入为 ThreadCell 或 _EphemeralSessionCell；输出为空。current_run_task 是 cell
        明确字段，缺失代表测试替身或调用方合同错误，允许 AttributeError 暴露。
        """
        run_task = cell.current_run_task
        if run_task is not None and not run_task.done():
            run_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(run_task, timeout=5.0)
        if cell.current_run_task is run_task:
            cell.current_run_task = None

    @staticmethod
    async def _aclose_host_dispatcher(cell: Any) -> None:
        """关闭 cell 持有的 HostDispatcher 生命周期 owner。"""
        dispatcher = _host_dispatcher_for(cell)
        try:
            await dispatcher.aclose(drain=False)
        except Exception:
            logger.exception(
                "host_dispatcher aclose failed for thread=%s",
                cell.thread_id,
            )

    def _is_idle_evictable_cell(
        self,
        cell: ThreadCell,
        *,
        now: float,
        threshold: float,
    ) -> bool:
        """判断 cell 当前是否没有任何 Manager 持有的工作。"""
        if (now - cell.last_active_at) <= threshold:
            return False
        # 现算入口已折叠 evicting / pending approval / active run 三个事实，
        # 非 IDLE 即说明还有 Manager 持有的运行态工作，不可回收。
        if self._effective_status(cell) is not ThreadCellStatus.IDLE:
            return False
        if cell.pending_inputs:
            return False
        return cell.pending_input_drain_block_reason is None

    async def _evict_cell_if_idle(
        self,
        thread_id: str,
        *,
        now: float,
        threshold: float,
    ) -> bool:
        """最终复核 idle 条件，通过后移出 cell 并执行回收。"""
        async with self._lock:
            cell = self._cells.get(thread_id)
        if cell is None:
            return False

        async with cell.pending_input_lock:
            if not self._is_idle_evictable_cell(cell, now=now, threshold=threshold):
                return False
            async with self._lock:
                if self._cells.get(thread_id) is not cell:
                    return False
                self._cells.pop(thread_id, None)
                self._boot_locks.pop(thread_id, None)
            self._mark_cell_evicting(cell, reason="idle")

        await self._finish_evicted_cell(cell, reason="idle", notify_ws=True)
        return True

    async def _idle_eviction_loop(self) -> None:
        """后台 task：周期扫描 cell 列表，命中空闲阈值的执行 evict。

        策略：
        - 每 ``cfg.web.idle_check_interval_seconds`` 秒扫一次
        - 命中条件：``now - cell.last_active_at > cfg.web.idle_timeout_seconds``
          且该 thread 没有待审批、active run、pending input 或 drain 阻断状态
        - 命中即走 ``_evict_cell_if_idle`` 做最终复核和回收
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
                    ]
                for tid in candidates:
                    with suppress(Exception):
                        await self._evict_cell_if_idle(
                            tid,
                            now=now,
                            threshold=threshold,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # idle loop 永不退出（除非 cancel）；记 warning 继续
                logger.warning("idle eviction loop iteration failed: %s", exc)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

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
            pending_approval_count = self._pending_approval_count(cell.thread_id)
            effective = self._effective_status(cell)
            # 现算结果映射到 wire DTO 允许的三态；evicting cell 已立即从 dict
            # pop，正常不会出现在这里，防御性兜底成 idle。
            status: Literal["idle", "running", "awaiting_approval"]
            if effective is ThreadCellStatus.RUNNING:
                status = "running"
            elif effective is ThreadCellStatus.AWAITING_APPROVAL:
                status = "awaiting_approval"
            else:
                status = "idle"
            out.append(
                CellSummaryDTO(
                    thread_id=cell.thread_id,
                    thread_name=cell.metadata.name,
                    preset_id=cell.metadata.preset_id,
                    created_at=cell.metadata.created_at,
                    last_active_at=cell.last_active_at,
                    current_turn=None,  # v0.1.5 不暴露 current_turn 精确值
                    pending_approval_count=pending_approval_count,
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
        return await self._persist_thread_metadata(updated)

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
        return await self._persist_thread_metadata(updated)

    # ------------------------------------------------------------------
    # Cron 定向投递（v0.4）
    # ------------------------------------------------------------------

    async def append_cron_message(
        self,
        thread_id: str,
        text: str,
        *,
        task_id: str,
        run_id: str,
        session_id: str,
        task_name: str = "",
    ) -> bool:
        """Broadcast a cron delivery message to an existing thread's websocket fanout.

        v0.4 cron-thread-preset：``ThreadTargetSink`` 调此方法把 cron 结果
        投递到目标 thread 的实时 WS fanout。cron runner 已经把本次执行写入
        ``session_id`` 对应的独立 run session；这里只负责实时通知前端，
        避免依赖 Web cell runtime 的私有 session cache。

        策略：
        - cell 未 boot（idle evicted / 从未启动）→ 返回 False
        - cell 已 boot → 发送 ``cron.message.appended`` → True

        Returns:
            True if the target thread cell exists, False otherwise.
        """
        async with self._lock:
            cell = self._cells.get(thread_id)
        if cell is None:
            return False

        # 通知 WS 前端（best-effort）
        try:
            fanout = getattr(cell.adapter, "_ws", None)
            if fanout is not None and hasattr(fanout, "send_json"):
                from hosts.web.app_support.cron_delivery import make_cron_message_frame

                await fanout.send_json(
                    make_cron_message_frame(
                        thread_id=thread_id,
                        task_id=task_id,
                        run_id=run_id,
                        session_id=session_id,
                        content=text,
                        task_name=task_name,
                    )
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


if TYPE_CHECKING:
    # mypy-only：确保真实 ThreadManager 持续满足 Web 路由层依赖的 Protocol 合同。
    def _assert_thread_manager_protocol(manager: ThreadManager) -> ThreadManagerProtocol:
        return manager


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
