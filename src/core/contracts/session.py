"""Session protocol."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from core.message import Message


@runtime_checkable
class Session(Protocol):
    """会话历史存储协议。

    - :mod:`core.session` 提供第一批 ``InMemorySession`` 默认实现。
    - ``sessions/session_store.py`` 以后会提供工程化实现，继续遵守此协议，
      不得再定义一份新 Session 接口。
    """

    session_id: str

    async def append(self, message: Message, *, usage: dict[str, Any] | None = None) -> None:
        """追加一条消息到会话末尾。

        ``usage`` 用于附着这条记录对应的 LLM token 统计。当前 runner 只在
        assistant 消息写入时传入；user / system / tool 默认留空。
        """
        ...

    async def history(self) -> list[Message]:
        """返回当前完整历史，顺序与 append 顺序一致。"""
        ...

    async def clear(self) -> None:
        """清空当前会话历史，并把该会话的 run 编号重置为初始状态。"""
        ...

    async def advance_run_index(self) -> int:
        """递增并返回新的 run 编号；递增后立即持久化（如有持久化层）。

        语义：
        - 调用一次 → +1 → 返回新值
        - 持久化后端（FileSession / SQLiteSession）必须把递增后的值落盘
        - 内存后端（InMemorySession）只更新实例字段

        非事务原子约定（v0.x 简化）：
        - 调用方通常在 ``session.append(user_msg)`` 之后立刻调用本方法，
          以把"用户消息入历史"和"run 编号递增"绑到同一时机
        - 严格讲两步应单事务原子提交，但 v0.x 阶段不做事务化：
          - append 成功 + advance 失败 → 下次启动 run_count 不变，新 run_index
            复用上次值；本 run 已崩溃未生成 run_id 记录，新一轮取此值仍唯一
            不撞，不会丢消息也不会让 run_id 重复
          - advance 成功 + append 失败（极罕见）→ jsonl 少一条，run_count 跳号，
            仍唯一不撞
        - 事务化留给 v0.2+
        """
        ...


__all__ = ["Session"]
