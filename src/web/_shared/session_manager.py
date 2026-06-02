"""活跃 session 表（v0.1）。

维护 ``sessionId → SessionRecord`` 映射，支持：

- ``register`` / ``unregister`` / ``get`` / ``is_active``
- ``replace_writer``：页面刷新重连——把活跃 session 的 writer 换成新连接
- ``request_abort``：让 service 主循环能优雅退出

设计要点：

- 进程级单例：通过 FastAPI ``app.state.claude_session_manager`` 暴露
- 内部 dict 由 ``asyncio.Lock`` 保护——多 ws 连接并发 register 时避免竞态
- 不持久化：进程崩溃 = 所有活跃 session 丢失（ccui 同款简化）
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionRecord:
    """一个活跃 Claude session 的运行期状态。

    Attributes:
        session_id: SDK 自分配的 session id（来自 ``SystemMessage(init).session_id``）
        writer: 当前绑定的 WebSocket（重连时被 :meth:`SessionManager.replace_writer`
            替换为新连接）
        started_at: 注册时间（``time.monotonic()``，仅供调试 / 列表展示）
        abort_event: ``request_abort`` 后被 set，service 主循环检查后退出
        query_task: 当前 SDK ``async for`` 主循环 task（``None`` 表示无主循环
            正在跑，比如 register 但 query 还没开始；abort 时调
            ``query_task.cancel()`` 强制终止）
    """

    session_id: str
    writer: Any
    started_at: float = field(default_factory=time.monotonic)
    abort_event: asyncio.Event = field(default_factory=asyncio.Event)
    query_task: asyncio.Task[Any] | None = None


class SessionManager:
    """进程级 session 表，asyncio.Lock 保护。"""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionRecord] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        session_id: str,
        writer: Any,
        *,
        query_task: asyncio.Task[Any] | None = None,
    ) -> SessionRecord:
        """注册新 session（已存在则覆盖 writer / query_task）。"""
        async with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                # 复用记录但刷新 writer / query_task / abort_event
                existing.writer = writer
                if query_task is not None:
                    existing.query_task = query_task
                existing.abort_event.clear()
                return existing
            record = SessionRecord(
                session_id=session_id,
                writer=writer,
                query_task=query_task,
            )
            self._sessions[session_id] = record
            return record

    async def unregister(self, session_id: str) -> SessionRecord | None:
        """从表中移除 session。返回原记录（无则 None）。"""
        async with self._lock:
            return self._sessions.pop(session_id, None)

    def get(self, session_id: str) -> SessionRecord | None:
        """读取 session 记录。**未加锁**——只读快照足够。"""
        return self._sessions.get(session_id)

    def is_active(self, session_id: str) -> bool:
        """session 是否仍在表中。"""
        return session_id in self._sessions

    async def replace_writer(self, session_id: str, new_writer: Any) -> bool:
        """把活跃 session 的 writer 换成新连接（重连入口）。

        Returns:
            ``True`` 替换成功；``False`` session 不存在
        """
        async with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return False
            attach = getattr(type(record.writer), "attach_ws", None)
            if callable(attach):
                record.writer.attach_ws(new_writer)
            else:
                record.writer = new_writer
            return True

    async def request_abort(self, session_id: str) -> bool:
        """请求中止指定 session 的当前 run。

        - set ``abort_event`` 让 service 主循环看见
        - cancel ``query_task``（如有）让 ``async for`` 立即抛 CancelledError

        Returns:
            ``True`` session 存在且已发出 abort；``False`` session 不存在
        """
        async with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return False
            record.abort_event.set()
            task = record.query_task
        # cancel 在 lock 外执行——避免 task done callback 反向调本类方法死锁
        if task is not None and not task.done():
            task.cancel()
        return True

    async def rename(self, old_id: str, new_id: str) -> bool:
        """把活跃 session 改名（用于 service 收到 SystemMessage(init) 后把
        placeholder ``pending-XXX`` 替换成真实 SDK session_id）。

        - 如果 ``old_id`` 不在表里 → 返回 False
        - 如果 ``new_id == old_id`` → no-op，返回 True
        - 如果 ``new_id`` 已存在（罕见，理论上不会发生）→ 返回 False，避免覆盖

        Returns:
            ``True`` 改名成功（含 no-op）；``False`` old_id 不存在 / new_id 冲突
        """
        async with self._lock:
            record = self._sessions.get(old_id)
            if record is None:
                return False
            if new_id == old_id:
                return True
            if new_id in self._sessions:
                return False
            record.session_id = new_id
            self._sessions[new_id] = record
            del self._sessions[old_id]
            return True

    def list_active(self) -> list[str]:
        """列出所有活跃 session_id。"""
        return list(self._sessions.keys())


__all__ = ["SessionManager", "SessionRecord"]
