"""默认 in-memory session 实现。

只解决"最小闭环能跑、多轮不丢历史"。持久化、恢复、压缩都由
``context/session_store.py`` 在后续批次升级时承担，遵守同一份
:class:`core.contracts.Session` 协议。

v1-mini 单协程执行，不加锁；后续如果出现多任务共享 session 的场景，
再在 InMemorySession 上或另写一层加 asyncio.Lock。
"""

from __future__ import annotations

import uuid
from typing import Any

from core.message import Message


class InMemorySession:
    """纯内存实现的 Session。

    满足 :class:`core.contracts.Session` Protocol 的结构性约束：
    同时提供 ``session_id`` 属性和 ``append / history / clear`` 三个 async 方法。
    """

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id: str = session_id or f"sess_{uuid.uuid4().hex[:12]}"
        self._messages: list[Message] = []
        self._run_count: int = 0

    async def append(self, message: Message, *, usage: dict[str, Any] | None = None) -> None:
        self._messages.append(message)

    async def history(self) -> list[Message]:
        # 返回副本，避免调用方就地修改污染 session 内部状态。
        return list(self._messages)

    async def clear(self) -> None:
        self._messages.clear()

    async def advance_run_index(self) -> int:
        # 内存后端无持久化；进程重启后从 0 重数（与 history 同语义）。
        self._run_count += 1
        return self._run_count

    def __len__(self) -> int:
        return len(self._messages)

    def __repr__(self) -> str:  # pragma: no cover - 调试友好
        return f"InMemorySession(session_id={self.session_id!r}, size={len(self._messages)})"


__all__ = ["InMemorySession"]
