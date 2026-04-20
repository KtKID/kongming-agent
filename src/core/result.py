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
        final_message: 最终产出的 assistant 消息；``status != "completed"`` 时可能为 None。
        turn_count: 实际推进的 turn 数。
        error: ``status`` 为 failed / cancelled 时承载错误信息。
        metadata: 附加信息，例如 token usage 汇总、timing、provider 信息。
    """

    run_id: str
    session_id: str
    status: ResultStatus
    final_message: Message | None
    turn_count: int
    error: AgentError | None = None
    metadata: dict[str, object] = field(default_factory=dict)


__all__ = ["Result", "ResultStatus"]
