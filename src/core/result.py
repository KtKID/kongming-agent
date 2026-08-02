"""Run 结束统一结果对象。

一次 runner.run() 调用无论以什么方式收口，都会产出一个 :class:`Result`。
host / cli 只读 Result，不直接看 RunState。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntFlag
from typing import Literal

from core.errors import AgentError, MaxTurnsExceededError
from core.message import Message

ResultStatus = Literal["completed", "failed", "cancelled"]


class RunEndReason(IntFlag):
    """run 结束原因 bitmask 枚举（错误分类器单一真源）。

    设计要点：
    - **自然因（三选一互斥）**：COMPLETE / MAX_TURNS / ERROR。对应 turn 循环同一
      次迭代的三条互斥出口（正常返回 / 预算耗尽抛错 / 真失败），不可能同时出现。
    - **外部因（可叠加在任意自然因上）**：INTERRUPT / EVICTED。典型叠加场景：
      ``COMPLETE | INTERRUPT``（模型刚给出终态，用户同时点了停止）、
      ``MAX_TURNS | INTERRUPT``（max_turns 抛出后收口清理期间 cancel 落地）。
      旧实现（runner except 子句有顺序）会吞掉叠加信息——bitmask 让审计层诚实记录。

    消费侧约定：
    - **停止按钮复位**：``reason != 0``（任何结束帧）→ 复位，不关心为什么结束。
    - **drain 决策**：仅 ``reason == COMPLETE``（纯自然完成）才自动 drain 队列下一条；
      任何外部因或 MAX_TURNS / ERROR 在场 → 不 drain（把控制权交还用户）。
    - **UI 显示**：``INTERRUPT`` 位 set 时显示"已停止"（尊重用户介入），即便
      ``COMPLETE`` 也在场。
    """

    NONE = 0
    COMPLETE = 1
    MAX_TURNS = 2
    ERROR = 4
    INTERRUPT = 8
    EVICTED = 16


def compute_run_end_reason(result: Result) -> RunEndReason:
    """从 :class:`Result` 推导结束原因 bitmask（纯函数）。

    这是 runner 之外唯一的原因推导入口；web thread-status / 队列 drain / 前端按钮
    都消费此函数的输出，不再各自 ``isinstance(result.error, ...)``。

    推导规则：
    - ``status == "completed"`` → ``COMPLETE``（即便 error 是 MaxTurnsExceededError，
      completed 分支走的是 mark_completed 路径，语义为正常完成）。
    - ``status == "failed"`` + ``MaxTurnsExceededError`` → ``MAX_TURNS``（预算耗尽，
      不是真错误）；其余 failed → ``ERROR``。
    - ``status == "cancelled"`` → ``INTERRUPT``（cancel 原因典型 = 用户点停止）。
    """
    status = result.status
    if status == "completed":
        return RunEndReason.COMPLETE
    if status == "cancelled":
        return RunEndReason.INTERRUPT
    # failed 分支：区分 max_turns（预算耗尽）与真 error
    if isinstance(result.error, MaxTurnsExceededError):
        return RunEndReason.MAX_TURNS
    return RunEndReason.ERROR


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
              ``InterruptFrame`` → ``ThreadManager.interrupt_agent_tree()`` →
              ``HostDispatcher.interrupt()``。

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


__all__ = ["Result", "ResultStatus", "RunEndReason", "compute_run_end_reason"]
