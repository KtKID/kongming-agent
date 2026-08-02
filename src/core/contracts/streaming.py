"""Streaming protocol helpers for LLM providers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from core.contracts.llm_provider import FinishReason, LLMRequest
from core.contracts.provider_usage import ProviderUsageSnapshot
from core.message import Message

StreamChunkKind = Literal[
    "reasoning.delta",
    "content.delta",
    "tool_call.start",
    "tool_call.arguments.delta",
    "tool_call.end",
    "message.done",
]


@dataclass(frozen=True)
class LLMStreamChunk:
    """provider-agnostic 流式增量事件。

    一次完整的流结束后，必须有且只有一个 ``kind="message.done"`` 的 chunk
    作为终态。runner 把该 chunk 的 ``message`` / ``finish_reason`` / ``usage``
    / ``provider_metadata`` 当作等价 :class:`LLMResponse` 使用，后续 tool_call
    / approval / session.append 链路与非流式完全一致。

    字段必填矩阵详见 ``docs/llm-provider-v0.2/streaming/04-data-and-state.md`` §1.1。
    核心约束（parser 必须保证）：

    - ``message.done`` 必须是流的最后一个 chunk
    - 同一 ``index`` 下 ``tool_call.start`` 必须在所有 ``tool_call.arguments.delta``
      之前；``tool_call.end`` 必须在该 index 的所有 delta 之后
    - ``tool_call.end`` 只承载定位信息（index / tool_call_id / tool_name），
      不包含 arguments 完整字符串或 extra_content；完整 tool_call（含合法 JSON
      化的 arguments 与 extra_content）只能通过 ``message.done.message.tool_calls``
      获取
    - 任何 ``*.delta`` kind 的 chunk 必须携带非空 ``delta``

    Attributes:
        kind: chunk 类型。见上方 :data:`StreamChunkKind`。
        delta: ``*.delta`` kind 的增量字符串；其他 kind 忽略。
        index: ``content.delta`` / ``tool_call.*`` 的槽位。``content.delta`` V1
            始终为 0（单正文 block）。
        tool_call_id: ``tool_call.start`` / ``tool_call.end`` 必填；
            ``tool_call.arguments.delta`` 可选。
        tool_name: ``tool_call.start`` / ``tool_call.end`` 必填。
        message: 仅 ``message.done`` 必填，其他 kind 忽略；等价非流式
            :attr:`LLMResponse.message`。
        finish_reason: 仅 ``message.done`` 必填。
        usage: 仅 ``message.done`` 使用；provider 没有 usage 时为 None。
        provider_metadata: 仅 ``message.done`` 必填（可为空 dict），其他 kind 忽略。
    """

    kind: StreamChunkKind
    delta: str = ""
    index: int = 0
    tool_call_id: str | None = None
    tool_name: str | None = None
    message: Message | None = None
    finish_reason: FinishReason | None = None
    usage: ProviderUsageSnapshot | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SupportsLLMStream(Protocol):
    """流式能力 Protocol，与 :class:`LLMProvider` 正交。

    具备该能力的 provider 必须同时满足 :class:`LLMProvider`；runner 用
    ``isinstance(llm, SupportsLLMStream)`` 做能力探测，避免用
    :class:`NotImplementedError` 作控制流。

    调用约定：

    - runner 不 ``await stream()`` 本身，直接拿 :class:`AsyncIterator`
    - provider 在 iterator 内部处理 HTTP / SSE / 累积
    - runner 消费完所有 chunk（直到见到 ``kind="message.done"`` 的终态）
    - 显式工具调用合同要求返回的 iterator 提供 async ``aclose()``；Runner 在消费
      任何 chunk 前检查该能力，缺失时以 ProviderError 失败，避免不可释放的响应流
    - 非 ``message.done`` 结束 = 流中断 = 由 provider 抛
      :class:`core.errors.ProviderError`；runner 不靠 iterator 耗尽来推断终态

    现有不具备流式能力的 provider / stub 不需要改：结构上没有 ``stream()``
    方法，``isinstance(..., SupportsLLMStream)`` 自动为 False。
    """

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        """对一次请求启动流式响应。返回可迭代的 :class:`LLMStreamChunk` 流。"""
        ...


# ---------------------------------------------------------------------------
__all__ = [
    "LLMStreamChunk",
    "StreamChunkKind",
    "SupportsLLMStream",
]
