"""轨迹投影内部数据模型（track-timeline-v0.1 · task-trajectory-backend）。

本文件只放 ``hosts.web.trajectory`` 模块内部使用的值对象与累积草稿：

- :class:`ParsedTraceEvent`：一行 trace.jsonl 解析后的**不可变**值对象
  （frozen——解析结果语义稳定）。
- :class:`SessionMessageRecord` / :class:`SystemPromptSnapshot`：从 session
  真源（session jsonl / system_prompt.json）读出的**不可变**正文值对象。
  trace 事件里的消息是脱敏摘要（``content_chars`` / ``message_roles``），
  用户输入、assistant 正文、系统提示词的**全文**只存在于 session 真源。
- ``*Draft`` 系列：投影过程中的**可变**累积状态（run / llm / tool /
  user_input / corrupted）。投影器先在草稿上累积（usage 可在
  llm.response 之后才到，需要回填），EOF 后统一物化成 frozen DTO。

判断标准（对齐架构设计原则「数据模型不可变优先」）：解析结果用 frozen，
逐步回填的累积状态保持可变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ParsedTraceEvent:
    """一行 trace.jsonl 解析出的统一事件（seq = 行号，0 起）。"""

    seq: int
    kind: str
    timestamp_ms: int
    run_id: str = ""
    turn: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionMessageRecord:
    """session jsonl 中一条消息记录的正文投影（时间 = created_at 毫秒）。

    trace 的 llm.request/llm.response 事件只带脱敏摘要，不落正文；用户
    输入与 assistant 回复全文以本值对象从 session 真源进入轨迹投影。
    """

    role: str
    content: str
    created_at_ms: int
    reasoning: str | None = None


@dataclass(frozen=True)
class SystemPromptSnapshot:
    """system_prompt.json 的正文快照（thread 级，含物化时间）。"""

    content: str
    created_at_ms: int | None = None


@dataclass
class RunDraft:
    """一次运行（run.start → run.end）的累积草稿。"""

    run_index: int
    run_id: str
    started_at_ms: int | None
    status: str = "running"
    ended_at_ms: int | None = None
    duration_ms: int | None = None
    max_turn: int = 0
    tokens: dict[str, int] = field(default_factory=dict)
    #: 该 run 是否已消费一条 session user 消息（融合阶段按序消费）
    user_input_consumed: bool = False


@dataclass
class UserInputDraft:
    """一次运行的用户输入记录草稿（正文来自 session 真源）。"""

    seq: int
    run_index: int
    run_id: str
    turn: int | None
    started_at_ms: int | None
    text: str = ""


@dataclass
class SystemPromptDraft:
    """系统提示词记录草稿（thread 级一条，seq=-1 表示非 trace 来源）。"""

    seq: int = -1
    started_at_ms: int | None = None
    text: str = ""
    instruction_sources: tuple[str, ...] = ()


@dataclass
class LlmDraft:
    """一次模型调用的累积草稿（llm.request → llm.response，usage 可后到）。

    ``content`` 初值来自 trace 脱敏摘要（通常为空）；EOF 后由 session
    真源的 assistant 正文回填（见 :meth:`TrajectoryProjector.project`）。
    """

    seq: int
    run_index: int
    run_id: str
    turn: int | None
    started_at_ms: int | None
    ended_at_ms: int | None = None
    ttft_ms: int | None = None
    finish_reason: str | None = None
    content: str = ""
    content_chars: int | None = None
    reasoning: str | None = None
    tool_call_count: int = 0
    tokens: dict[str, int] = field(default_factory=dict)
    closed: bool = False


@dataclass
class ToolDraft:
    """一次工具调用的累积草稿（tool.call.start → tool.call.end 配对）。"""

    seq: int
    run_index: int
    run_id: str
    turn: int | None
    call_id: str
    tool_name: str
    started_at_ms: int | None
    arguments: dict[str, Any] = field(default_factory=dict)
    ended_at_ms: int | None = None
    ok: bool | None = None
    content: str = ""
    data: Any = None
    error_message: str | None = None
    closed: bool = False


@dataclass
class CorruptedDraft:
    """一行无法解析的 trace 行的占位草稿（保留 seq，内容不可展开）。"""

    seq: int
    started_at_ms: int | None = None
