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
from pathlib import Path
from typing import Any, Literal

from config_loader.models import Config
from core.contracts import ApprovalAction
from core.message import Message
from web.host_adapter import WebHostAdapter
from web.protocol import CellEvictedFrame, CellSummaryDTO, EvictReason
from web.thread_cell import ThreadCell
from web.thread_metadata import (
    ThreadMetadata,
    delete_thread_metadata_dir,
    list_thread_metadata,
    read_thread_metadata,
    thread_metadata_path,
    write_thread_metadata,
)
from web.thread_status_ws import ThreadStatusEventSink
from web.ws_event_sink import UsagePersistSink, WSEventSink
from web.ws_fanout import WebSocketFanout

logger = logging.getLogger(__name__)


class ClaudeThreadAlreadyBoundError(ValueError):
    """thread.claude_thread_id 已经非空，禁止覆盖（v0.2 invariant：写入后只读）。"""


class ClaudeThreadConflictError(ValueError):
    """claude_thread_id 已被另一 thread 绑定，禁止重复绑定（v0.2 invariant：1:1 绑定）。"""


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
    ) -> None:
        self._cfg = cfg
        self._home = kongming_home
        self._runtime_factory = runtime_factory
        self._cells: dict[str, ThreadCell] = {}
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False

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
        normalized_cwd = cwd.strip()

        thread_id = _generate_thread_id()
        resolved_name = name.strip() or thread_id
        now = _now()
        meta = ThreadMetadata(
            id=thread_id,
            name=resolved_name,
            preset_id=preset_id,
            backend_kind=backend_kind,
            cwd=normalized_cwd,
            created_at=now,
            updated_at=now,
            message_count=0,
            cumulative_prompt_tokens=0,
            cumulative_completion_tokens=0,
            cumulative_total_tokens=0,
        )
        # 写盘可能阻塞，放 to_thread
        await asyncio.to_thread(write_thread_metadata, self._home, meta)
        return meta

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
            cumulative_prompt_tokens=meta.cumulative_prompt_tokens,
            cumulative_completion_tokens=meta.cumulative_completion_tokens,
            cumulative_total_tokens=meta.cumulative_total_tokens,
        )
        await asyncio.to_thread(write_thread_metadata, self._home, updated)
        async with self._lock:
            cell = self._cells.get(thread_id)
            if cell is not None:
                cell.metadata = updated
        return updated

    async def add_thread_usage(
        self,
        thread_id: str,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        cache_read_tokens: int | None = None,
        cache_creation_tokens: int | None = None,
    ) -> ThreadMetadata:
        """把一次 run 的累计 usage 回写到 thread metadata。

        用于 generic chat 路径在 ``bridge.run_once`` 结束后落盘 thread 级累计
        ``prompt / completion / total``。负数一律按 0 处理，避免上游异常值污染累计量。

        ``cache_read_tokens`` / ``cache_creation_tokens`` 为 optional；generic_chat
        的 usage event 可能不包含它们。非 None 时累加到 metadata 对应字段。
        """
        meta = await asyncio.to_thread(read_thread_metadata, self._home, thread_id)
        if meta is None:
            raise KeyError(f"thread not found: {thread_id}")

        prompt = max(0, int(prompt_tokens))
        completion = max(0, int(completion_tokens))
        total = max(0, int(total_tokens))
        update: dict[str, object] = {
            "updated_at": _now(),
            "cumulative_prompt_tokens": meta.cumulative_prompt_tokens + prompt,
            "cumulative_completion_tokens": meta.cumulative_completion_tokens + completion,
            "cumulative_total_tokens": meta.cumulative_total_tokens + total,
            "schema_version": 6,
        }
        if cache_read_tokens is not None:
            update["cumulative_cache_read_tokens"] = (
                meta.cumulative_cache_read_tokens + cache_read_tokens
            )
        if cache_creation_tokens is not None:
            update["cumulative_cache_creation_tokens"] = (
                meta.cumulative_cache_creation_tokens + cache_creation_tokens
            )
        updated = meta.model_copy(update=update)
        await asyncio.to_thread(write_thread_metadata, self._home, updated)
        async with self._lock:
            cell = self._cells.get(thread_id)
            if cell is not None:
                cell.metadata = updated
        return updated

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
        )
        ws_sink = WSEventSink(fanout)
        usage_sink = UsagePersistSink(meta.id, self.add_thread_usage)

        status_sink = ThreadStatusEventSink(meta.id)
        sinks: list[Any] = [ws_sink, usage_sink, status_sink]
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
            with suppress(Exception):
                await cell.adapter._safe_send_json(frame.model_dump())

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
        out.sort(key=lambda m: m.updated_at, reverse=True)
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
                "schema_version": 6,
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
                "schema_version": 6,
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
                        "kind": "cron.message.appended",
                        "thread_id": thread_id,
                        "content": text,
                    }
                )
        except Exception:
            pass  # WS 通知失败不影响投递结果

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
]
