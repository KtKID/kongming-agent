"""Run 结束统一结果对象。

一次 runner.run() 调用无论以什么方式收口，都会产出一个 :class:`Result`。
host / cli 只读 Result，不直接看 RunState。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from core.errors import AgentError
from core.message import Message

ResultStatus = Literal["completed", "failed", "cancelled"]


@dataclass(frozen=True)
class Result:
    """Run 结束的统一结果。

    Attributes:
        run_id / session_id: 运行坐标。
        status: 结束状态。只有三种；中途态（running / waiting_approval）不会出现在 Result 里。

            - ``completed``：runner 走完全部 turn，正常返回最终 assistant 消息
            - ``failed``：捕获到 :class:`AgentError` 或意外异常（error 字段非空）
            - ``cancelled``：runner 顶层捕获到 :class:`asyncio.CancelledError`
              （interrupt-run-v0.1 起启用）。来源典型 = 用户在 web 端发
              ``InterruptFrame`` → ``cell.current_run_task.cancel()``。

        final_message: 最终产出的 assistant 消息；``status != "completed"`` 时可能为 None。
        turn_count: 实际推进的 turn 数。
        error: ``status`` 为 failed / cancelled 时承载错误信息。
        metadata: 附加信息，例如 token usage 汇总、timing、provider 信息。

            **interrupt-run-v0.1 起约定字段**（``status == "cancelled"`` 时填充，
            非强类型，runner 写入 / 上层按需消费）：

            - ``cancelled_at_turn: int`` —— 被 cancel 时正处在第几个 turn
            - ``cancelled_tool_call_id: str | None`` —— 被 cancel 时正在跑
              的 tool_call_id；如果 cancel 时不在 tool 执行阶段（如 LLM stream
              中）则为 None
            - ``cancel_reason: str`` —— 触发来源标签（如 ``"user_interrupt"``
              / ``"cell_evict"``）；空字符串表示未标注
    """

    run_id: str
    session_id: str
    status: ResultStatus
    final_message: Message | None
    turn_count: int
    error: AgentError | None = None
    metadata: dict[str, object] = field(default_factory=dict)


__all__ = ["Result", "ResultStatus"]
