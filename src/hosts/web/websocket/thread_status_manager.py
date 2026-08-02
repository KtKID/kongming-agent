"""Web thread 状态与单连接出站队列的唯一 Manager。

本模块维护 active status、全局 sequence、每 thread 单调 run generation，以及
每个 ``/ws/thread-status`` 连接的有界队列和唯一 writer。所有状态 snapshot 与
增量在同一把锁内排队，保证 attach、发布和终态收口具有确定顺序。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from hosts.web.protocol import (
    ThreadStatusFrame,
    ThreadStatusPhase,
    ThreadStatusSnapshotFrame,
)
from network.network_log import log_network_exception

_ACTIVE_PHASES: frozenset[str] = frozenset(
    {"responding", "thinking", "tool_calling", "waiting_approval"}
)
_SLOW_CLIENT_CLOSE_CODE = 1013
_SLOW_CLIENT_CLOSE_REASON = "thread status outbound queue overflow"


@dataclass(frozen=True, slots=True)
class ThreadStatusRunLease:
    """一次 thread run 的稳定身份，由 Manager 单调分配。"""

    thread_id: str
    run_id: str
    generation: int


@dataclass(slots=True)
class _ConnectionState:
    """单个 WebSocket 的出站队列与唯一 writer task。"""

    websocket: WebSocket
    queue: asyncio.Queue[dict[str, Any] | None]
    writer_task: asyncio.Task[None] | None = None
    closing: bool = False
    close_event: asyncio.Event = field(default_factory=asyncio.Event)
    started_event: asyncio.Event = field(default_factory=asyncio.Event)


class ThreadStatusManager:
    """管理 thread active 状态、run lease 和全局 WS 单 writer。"""

    def __init__(self, *, queue_size: int = 128) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be >= 1")
        self._queue_size = queue_size
        self._active: dict[str, ThreadStatusFrame] = {}
        self._run_generations: dict[str, int] = {}
        self._current_leases: dict[str, ThreadStatusRunLease] = {}
        self._connections: dict[WebSocket, _ConnectionState] = {}
        self._sequence = 0
        self._lock = asyncio.Lock()

    @property
    def connection_count(self) -> int:
        """返回当前由 Manager 管理的连接数。"""
        return len(self._connections)

    @property
    def sequence(self) -> int:
        """返回最近一次已接受状态变更的全局 sequence。"""
        return self._sequence

    @property
    def active_statuses(self) -> dict[str, ThreadStatusFrame]:
        """返回 active map 的深拷贝快照，供监控和测试只读使用。"""
        return {thread_id: frame.model_copy(deep=True) for thread_id, frame in self._active.items()}

    async def begin_run(self, thread_id: str, run_id: str) -> ThreadStatusRunLease:
        """为 run 分配单调 generation，并将其设为 thread 当前 lease。"""
        if not thread_id:
            raise ValueError("thread_id must not be empty")
        if not run_id:
            raise ValueError("run_id must not be empty")
        async with self._lock:
            generation = self._run_generations.get(thread_id, 0) + 1
            self._run_generations[thread_id] = generation
            lease = ThreadStatusRunLease(
                thread_id=thread_id,
                run_id=run_id,
                generation=generation,
            )
            self._current_leases[thread_id] = lease
            return lease

    async def attach(self, websocket: WebSocket) -> None:
        """登记连接，并把 active snapshot 作为该连接首个出站帧排队。"""
        state: _ConnectionState | None = None
        async with self._lock:
            if websocket in self._connections:
                return
            queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=self._queue_size)
            snapshot = ThreadStatusSnapshotFrame(
                watermark=self._sequence,
                items=[frame.model_copy(deep=True) for _, frame in sorted(self._active.items())],
            ).model_dump(exclude_none=True)
            queue.put_nowait(snapshot)
            state = _ConnectionState(
                websocket=websocket,
                queue=queue,
            )
            writer_task = asyncio.create_task(
                self._writer_loop(state),
                name=f"thread-status-writer-{id(websocket)}",
            )
            state.writer_task = writer_task
            self._connections[websocket] = state
        await state.started_event.wait()

    async def detach(self, websocket: WebSocket) -> None:
        """移除连接并回收 writer；重复调用保持幂等。"""
        async with self._lock:
            state = self._connections.pop(websocket, None)
        if state is None:
            return
        await self._stop_writer(state)

    async def publish_status(
        self,
        lease: ThreadStatusRunLease,
        *,
        phase: ThreadStatusPhase,
        tool_name: str | None = None,
        run_end_reason: int | None = None,
    ) -> bool:
        """接受当前 lease 的状态并广播；stale lease 返回 ``False``。"""
        overflowed: list[_ConnectionState] = []
        async with self._lock:
            if self._current_leases.get(lease.thread_id) != lease:
                return False
            self._sequence += 1
            frame = ThreadStatusFrame(
                threadId=lease.thread_id,
                phase=phase,
                sequence=self._sequence,
                runId=lease.run_id,
                runGeneration=lease.generation,
                toolName=tool_name,
                run_end_reason=run_end_reason,
            )
            if phase in _ACTIVE_PHASES:
                self._active[lease.thread_id] = frame
            else:
                self._active.pop(lease.thread_id, None)
            overflowed = self._enqueue_all_locked(frame.model_dump(exclude_none=True))
        await self._close_overflowed(overflowed)
        return True

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """经各连接唯一 writer 广播同通道的非状态协议帧。"""
        async with self._lock:
            overflowed = self._enqueue_all_locked(dict(payload))
        await self._close_overflowed(overflowed)

    async def send_to(self, websocket: WebSocket, payload: dict[str, Any]) -> bool:
        """经指定连接唯一 writer 排队发送，连接不存在时返回 ``False``。"""
        overflowed: list[_ConnectionState] = []
        async with self._lock:
            state = self._connections.get(websocket)
            if state is None or state.closing:
                return False
            try:
                state.queue.put_nowait(dict(payload))
            except asyncio.QueueFull:
                self._mark_overflow_locked(state)
                overflowed.append(state)
        await self._close_overflowed(overflowed)
        return not overflowed

    def _enqueue_all_locked(
        self,
        payload: dict[str, Any],
    ) -> list[_ConnectionState]:
        """在 Manager lock 内向所有连接排队，返回需要关闭的慢连接。"""
        overflowed: list[_ConnectionState] = []
        for _websocket, state in list(self._connections.items()):
            if state.closing:
                continue
            try:
                state.queue.put_nowait(dict(payload))
            except asyncio.QueueFull:
                self._mark_overflow_locked(state)
                overflowed.append(state)
        return overflowed

    def _mark_overflow_locked(self, state: _ConnectionState) -> None:
        """丢弃慢连接积压帧并把 1013 关闭指令交给唯一 writer。"""
        state.closing = True
        while True:
            try:
                state.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                state.queue.task_done()
        state.close_event.set()

    async def _writer_loop(self, state: _ConnectionState) -> None:
        """串行消费单连接队列，网络失败时移除自身。"""
        try:
            while True:
                payload = await self._next_payload_or_close(state)
                if payload is None:
                    state.started_event.set()
                    return
                state.started_event.set()
                try:
                    sent = await self._send_payload_or_close(state, payload)
                    if not sent:
                        return
                finally:
                    state.queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_network_exception(
                "hosts.web.websocket.thread_status_manager",
                "writer_send_failed",
                exc,
                websocket_id=id(state.websocket),
            )
        finally:
            async with self._lock:
                if self._connections.get(state.websocket) is state:
                    self._connections.pop(state.websocket, None)

    async def _next_payload_or_close(
        self,
        state: _ConnectionState,
    ) -> dict[str, Any] | None:
        """等待下一帧；overflow 优先让唯一 writer 发出 1013 close。"""
        if state.close_event.is_set():
            await state.websocket.close(
                code=_SLOW_CLIENT_CLOSE_CODE,
                reason=_SLOW_CLIENT_CLOSE_REASON,
            )
            return None
        payload = await state.queue.get()
        if state.close_event.is_set():
            state.queue.task_done()
            await state.websocket.close(
                code=_SLOW_CLIENT_CLOSE_CODE,
                reason=_SLOW_CLIENT_CLOSE_REASON,
            )
            return None
        return payload

    async def _send_payload_or_close(
        self,
        state: _ConnectionState,
        payload: dict[str, Any],
    ) -> bool:
        """发送一帧；overflow 会先取消阻塞发送，再由同一 writer 关闭连接。"""
        send_task = asyncio.create_task(
            state.websocket.send_json(payload),
            name=f"thread-status-send-{id(state.websocket)}",
        )
        close_task = asyncio.create_task(
            state.close_event.wait(),
            name=f"thread-status-close-wait-{id(state.websocket)}",
        )
        try:
            done, _ = await asyncio.wait(
                {send_task, close_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if close_task in done:
                send_task.cancel()
                with suppress(asyncio.CancelledError):
                    await send_task
                await state.websocket.close(
                    code=_SLOW_CLIENT_CLOSE_CODE,
                    reason=_SLOW_CLIENT_CLOSE_REASON,
                )
                return False
            await send_task
            return True
        finally:
            for task in (send_task, close_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(send_task, close_task, return_exceptions=True)

    async def _stop_writer(self, state: _ConnectionState) -> None:
        """取消并等待指定 writer，避免连接生命周期泄漏 task。"""
        writer_task = state.writer_task
        if writer_task is None or writer_task is asyncio.current_task():
            return
        writer_task.cancel()
        with suppress(asyncio.CancelledError):
            await writer_task

    async def _close_overflowed(
        self,
        states: list[_ConnectionState],
    ) -> None:
        """让队列溢出的连接 writer 获得调度并自行发送 1013 close。"""
        if states:
            await asyncio.sleep(0)


__all__ = [
    "ThreadStatusManager",
    "ThreadStatusRunLease",
]
