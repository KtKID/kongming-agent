"""运行态对象。

:class:`RunState` 描述"这次 run 发生了什么、现在走到哪了"。
它故意设计成可序列化：所有字段都是基本类型、dataclass 或 Message。
后续要接中断 / 恢复 / 持久化（例如 SQLite session），只需要把它序列化进去。

这里**不放**任何主循环推进逻辑。turn 推进是 :mod:`core.runner` 的唯一职责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from core.errors import AgentError
from core.message import Message

RunStatus = Literal[
    "pending",
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "cancelled",
]


@dataclass
class RunState:
    """一次 run 的运行态。

    Attributes:
        run_id: 本次 run 的唯一 id。
        session_id: 所属 session，多次 run 可以共享同一个 session。
        status: 当前生命周期状态。runner 会在合法状态之间迁移。
        turn: 已经进入第几个 turn，从 0 开始；首次 LLM 调用前为 0，之后自增。
        messages: 本次 run 观察到的消息序列（通常是 session.history 的快照副本），
            供 observability / recovery 使用，不代替 session 自身历史。
        last_error: 最近一次抛出的错误；status=failed 时必然非空。
        metadata: 自由字段，装配层可以写入 trace_id 之类的附加信息。
    """

    run_id: str
    session_id: str
    status: RunStatus = "pending"
    turn: int = 0
    messages: list[Message] = field(default_factory=list)
    last_error: AgentError | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def mark_running(self) -> None:
        self.status = "running"

    def mark_waiting_approval(self) -> None:
        self.status = "waiting_approval"

    def mark_completed(self) -> None:
        self.status = "completed"

    def mark_failed(self, error: AgentError) -> None:
        self.status = "failed"
        self.last_error = error

    def mark_cancelled(self) -> None:
        """标记 run 被 cancel（典型 = 用户主动 interrupt）。

        interrupt-run-v0.1 起，runner 顶层 ``except asyncio.CancelledError``
        会调本方法。``"cancelled"`` 与 ``"failed"`` 的区别：
        ``failed`` 由代码异常触发并伴随 ``last_error``；``cancelled`` 是
        协作式取消（外部 task.cancel()），可能不带 error。
        """
        self.status = "cancelled"

    def advance_turn(self) -> int:
        """进入下一个 turn，返回新 turn 号（1-based 语义）。"""
        self.turn += 1
        return self.turn

    def record(self, message: Message) -> None:
        """记录一条可观察的消息快照。"""
        self.messages.append(message)


__all__ = ["RunState", "RunStatus"]
