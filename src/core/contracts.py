"""跨模块共享协议真源。

这是 v1-mini 唯一可以被其他模块 ``import`` 的协议定义文件。
如果一个接口会被 ``core / tools / context / executors / safety / observability / host / cli``
中任意两个模块同时消费，它就必须收口到这里。

当前收进来的协议：

- :class:`Session`
- :class:`Tool`
- :class:`ApprovalProvider`
- :class:`LLMProvider`
- :class:`EventSink`

以及一组支撑数据结构（:class:`ToolContext` / :class:`ToolResult` /
:class:`ApprovalRequest` / :class:`ApprovalDecision` / :class:`LLMRequest` /
:class:`LLMResponse` / :class:`Event`）。

:class:`ToolLookup` 是 runner 对外拿工具的抽象面：runner 不需要知道具体的
``tools/registry.py`` 类，只要拿到一个能按名查工具的对象即可。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from core.message import Message

# ---------------------------------------------------------------------------
# Tool 相关支撑类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolContext:
    """工具执行上下文。

    Attributes:
        run_id: 当前 run 的 id，便于工具在日志 / trace 里标识来源。
        session_id: 当前 session id。
        turn: 工具被调用所在的 turn，从 1 开始计数。
        call_id: 对应的 :class:`core.message.ToolCall.call_id`。
        metadata: 装配层注入的额外上下文（例如 cwd、env 快照），core 不解释内容。
    """

    run_id: str
    session_id: str
    turn: int
    call_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果。

    ``ok=False`` 表示工具自己判定失败但已有结构化信息。
    工具实现抛出的原始异常由 runner 在外层包成
    :class:`core.errors.ToolError`，不走这个字段。
    """

    ok: bool
    content: str
    data: dict[str, Any] | None = None
    error_message: str | None = None


@runtime_checkable
class Tool(Protocol):
    """统一工具协议。

    实现方通常在 ``tools/`` 下面。core 本身不提供 Tool 实现。

    Attributes:
        name: 唯一工具名，模型通过它在 tool_call 里指向具体工具。
        description: 自然语言描述，会出现在 LLM 的 tools 列表里。
        input_schema: 参数 JSON schema；provider 适配层负责转换成目标格式。
    """

    name: str
    description: str
    input_schema: dict[str, Any]

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行一次工具调用。必须是 async。"""
        ...


@runtime_checkable
class ToolLookup(Protocol):
    """工具查找面。

    runner 只依赖这一层，不依赖具体 ``tools/registry.py``。
    ``Mapping[str, Tool]`` 天然满足此 Protocol（因为 ``__getitem__`` 和
    ``__contains__`` 都在），所以测试里可以直接传 dict。
    """

    def __contains__(self, name: object) -> bool: ...
    def __getitem__(self, name: str) -> Tool: ...


# ---------------------------------------------------------------------------
# Approval 相关支撑类型
# ---------------------------------------------------------------------------


ApprovalOutcome = Literal["approved", "rejected", "cancelled"]


@dataclass(frozen=True)
class ApprovalRequest:
    """一次审批请求。

    Attributes:
        run_id / session_id / turn / call_id: 定位该次调用的运行坐标。
        tool_name: 待审批的工具名。
        arguments: 模型发起的参数。审批端可以据此展示给人看。
        reason: 装配层给出的补充理由（例如"命中 permission=ask 规则"）。
        metadata: 额外信息，例如涉及文件路径、执行命令等摘要。
    """

    run_id: str
    session_id: str
    turn: int
    call_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovalDecision:
    """一次审批结果。

    Attributes:
        outcome: approved / rejected / cancelled 之一。
        reason: 人工或策略给出的理由文本。
        metadata: 附加信息，比如操作者身份、来源 UI 等。
    """

    outcome: ApprovalOutcome
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        """便捷判断：只有 ``outcome == "approved"`` 才视为通过。"""
        return self.outcome == "approved"


@runtime_checkable
class ApprovalProvider(Protocol):
    """审批入口协议。

    第一批默认实现是 ``tools/approval.py`` 里的 ``InteractiveApproval``；
    后续 safety 模块会基于策略返回决定。核心约束：
    **不改变协议形状，只新增实现**。
    """

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """对一次工具调用做出审批决定。"""
        ...


# ---------------------------------------------------------------------------
# LLM Provider 相关支撑类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMRequest:
    """runner 发给 provider 的统一请求。

    Attributes:
        model: 模型名。核心协议不收模型厂商信息，只收字符串。
        messages: 对话历史片段，包含 system / user / assistant / tool。
        tools: 可用的工具协议列表；provider 负责转成自己的 schema。
        temperature / max_tokens / timeout_seconds: 采样参数；``None`` 表示由 provider
            默认或由统一配置层补齐。
        metadata: 预留给装配层附加 provider 特定字段，不进核心协议。
    """

    model: str
    messages: tuple[Message, ...]
    tools: tuple[Tool, ...] = ()
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


FinishReason = Literal["stop", "tool_calls", "length", "error", "other"]


@dataclass(frozen=True)
class LLMResponse:
    """provider 返回的统一响应。

    Attributes:
        message: 模型这一轮的 assistant 消息。允许只带 tool_calls，不带 content。
        finish_reason: 结束原因；tool_calls 表示模型要求继续执行工具。
        usage: token 使用量等运行时统计（透传给 observability）。
        raw: 原始 provider 响应的精简引用，仅用于调试，core 不读它。
    """

    message: Message
    finish_reason: FinishReason = "stop"
    usage: dict[str, Any] = field(default_factory=dict)
    raw: Any | None = None


@runtime_checkable
class LLMProvider(Protocol):
    """LLM provider 统一协议。

    具体适配在 ``executors/llm/*.py``。
    """

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """发起一次模型调用并拿回统一响应。必须 async。"""
        ...


# ---------------------------------------------------------------------------
# Session 协议
# ---------------------------------------------------------------------------


@runtime_checkable
class Session(Protocol):
    """会话历史存储协议。

    - :mod:`core.session` 提供第一批 ``InMemorySession`` 默认实现。
    - ``context/session_store.py`` 以后会提供工程化实现，继续遵守此协议，
      不得再定义一份新 Session 接口。
    """

    session_id: str

    async def append(self, message: Message) -> None:
        """追加一条消息到会话末尾。"""
        ...

    async def history(self) -> list[Message]:
        """返回当前完整历史，顺序与 append 顺序一致。"""
        ...

    async def clear(self) -> None:
        """清空当前会话历史。"""
        ...


# ---------------------------------------------------------------------------
# EventSink / Event
# ---------------------------------------------------------------------------


EventKind = Literal[
    "run.start",
    "run.end",
    "turn.start",
    "turn.end",
    "llm.request",
    "llm.response",
    "tool.call.start",
    "tool.call.end",
    "approval.request",
    "approval.decision",
    "error",
]


@dataclass(frozen=True)
class Event:
    """统一事件结构。

    runner 在关键节点构造 Event，fan-out 到所有注册的 :class:`EventSink`。
    v1-mini 只有一个 sink：``observability/trace_sink.py`` 的 ``JsonlTraceSink``。
    v0.2+ 追加 usage / audit sink 时仍然走同一个协议，不新增事件协议。
    """

    kind: str
    run_id: str
    turn: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))


@runtime_checkable
class EventSink(Protocol):
    """事件落地协议。

    实现方保证 ``emit`` 是幂等写入或至少不会抛异常污染主链路。
    fan-out 职责在 runner，不是 sink 自己的事。
    """

    async def emit(self, event: Event) -> None:
        """处理一个事件。"""
        ...


# ---------------------------------------------------------------------------
# MessageCompactor
# ---------------------------------------------------------------------------


@runtime_checkable
class MessageCompactor(Protocol):
    """runner 在每个 turn 把 history 送给 LLM 之前的加工钩子。

    实现类在 ``context.history_compactor.HistoryCompactor``；core 只定义接口，
    不持有实现。命名刻意和实现类错开（Protocol = ``MessageCompactor``，实现 =
    ``HistoryCompactor``），避免 ``from core.contracts import HistoryCompactor``
    和 ``from context import HistoryCompactor`` 同名歧义。

    典型实现：压缩超长 history（裁剪空白消息、截断长 tool_result）。未来如果要做
    敏感字段 redact / few-shot 注入，也走同一个 Protocol，不新增协议。
    """

    async def compact(self, history: Sequence[Message]) -> list[Message]:
        """给定原始 history，返回加工后的 messages 列表。

        约定：
        - 永远返回**新** list，不就地修改入参
        - 空输入 → 空 list
        - 不涉及阈值时可以直接返回 ``list(history)``（原样拷贝）
        """
        ...


__all__ = [
    # Tool
    "ApprovalDecision",
    "ApprovalOutcome",
    "ApprovalProvider",
    "ApprovalRequest",
    "Event",
    "EventKind",
    "EventSink",
    "FinishReason",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "MessageCompactor",
    "Session",
    "Tool",
    "ToolContext",
    "ToolLookup",
    "ToolResult",
]
