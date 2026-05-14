"""Anthropic Messages API stream parser。

把 Anthropic Messages 流式响应的 SSE 事件序列归一化为 provider-agnostic 的
:class:`LLMStreamChunk` 序列，与 :mod:`executors.llm.openai_compat_stream_parser`
对齐输出形态（runner / sink 不感知 provider 差异）。

事件 → chunk 映射表见 ``docs/llm-provider-v0.2/streaming/04-data-and-state.md`` §5。
不与 OpenAI parser 共用代码：两套 SSE 协议事件种类完全不同，强共用会互相
污染状态机。

**设计要点**：

- ``input_json_delta`` 仅做字符串累积，**不在每个 delta 上跑 ``json.loads``**——
  避免 O(n²) 解析开销（对齐 ``other/claude-code-main/src/services/api/claude.ts``
  的"raw stream not BetaMessageStream"决策）；流结束 ``_build_message()`` 末尾
  一次性 ``json.loads``。
- 流结束守卫两条（对齐官方 claude.ts 行 2350）：
  1. 从未见 ``message_start`` → 抛 ``ProviderError("...without message_start...")``
  2. 见到 ``message_start`` 但既没 ``stop_reason`` 也没任何 tool_use block 完成
     → 抛 ``ProviderError("...without stop_reason or content blocks")``
- Stall 检测：chunk 间隔超过 ``_STALL_THRESHOLD_SECONDS`` (30s) 时
  ``logger.warning`` 一条；不主动 abort（httpx ``Timeout(read=...)`` 兜底）。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

from core.contracts import FinishReason, LLMStreamChunk
from core.errors import ProviderError
from core.message import Message, ToolCall

logger = logging.getLogger(__name__)


_BlockType = Literal["text", "thinking", "tool_use"]


@dataclass
class _AnthropicBlock:
    """Anthropic ``content_block`` 的累积状态。"""

    block_type: _BlockType
    text: str = ""
    tool_use_id: str = ""
    tool_name: str = ""
    tool_input_json: str = ""
    tool_call_started: bool = False  # 是否已 yield tool_call.start


@dataclass
class _AnthropicParserState:
    """Anthropic Messages SSE parser 累积状态。"""

    content_blocks: dict[int, _AnthropicBlock] = field(default_factory=dict)

    stop_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    model_name: str | None = None
    message_id: str | None = None
    chunk_count: int = 0


class AnthropicStreamParser:
    """消费 ``(event_name, data)`` 序列，产出 :class:`LLMStreamChunk` 流。

    单实例只处理一条流；复用同一对象处理多条流会复用状态字段，语义未定义——
    每条流构造新实例（与 :class:`OpenAICompatStreamParser` 一致约定）。
    """

    _STALL_THRESHOLD_SECONDS = 30.0

    def __init__(self) -> None:
        self._state = _AnthropicParserState()

    @property
    def chunk_count(self) -> int:
        return self._state.chunk_count

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    async def parse(
        self,
        events: AsyncIterator[tuple[str | None, dict[str, Any]]],
    ) -> AsyncIterator[LLMStreamChunk]:
        """消费 SSE 二元组流，产出 :class:`LLMStreamChunk`。

        - 每个 chunk 之间维护 ``last_event_time``，超过 30s gap 走
          ``logger.warning``（不抛错，httpx read_timeout 兜底）。
        - 错误事件（``event: error``）立即抛 :class:`ProviderError`。
        - 流结束触发 :meth:`_finalize`，包含双守卫与终态聚合。
        """
        last_event_time: float | None = None

        async for event_name, data in events:
            self._state.chunk_count += 1
            now = time.monotonic()
            if (
                last_event_time is not None
                and (now - last_event_time) > self._STALL_THRESHOLD_SECONDS
            ):
                logger.warning(
                    "anthropic stream stall detected: %.1fs gap between events",
                    now - last_event_time,
                )
            last_event_time = now

            async for chunk in self._dispatch(event_name, data):
                yield chunk

        async for chunk in self._finalize():
            yield chunk

    # ------------------------------------------------------------------
    # 单事件分派
    # ------------------------------------------------------------------

    async def _dispatch(
        self,
        event_name: str | None,
        data: dict[str, Any],
    ) -> AsyncIterator[LLMStreamChunk]:
        # SSE event 行优先；没有时退到 data.type（防御代理把 event: 行吞了）
        evt = event_name or (data.get("type") if isinstance(data.get("type"), str) else None)

        if evt == "ping":
            return
        if evt == "error":
            err = data.get("error") or {}
            err_type = err.get("type") if isinstance(err, dict) else None
            err_msg = err.get("message") if isinstance(err, dict) else None
            raise ProviderError(
                f"Anthropic stream error event: {err_type}: {err_msg}",
                details={"error": err, "message_id": self._state.message_id},
            )

        if evt == "message_start":
            self._handle_message_start(data)
            return

        if evt == "content_block_start":
            async for chunk in self._handle_content_block_start(data):
                yield chunk
            return

        if evt == "content_block_delta":
            async for chunk in self._handle_content_block_delta(data):
                yield chunk
            return

        if evt == "content_block_stop":
            async for chunk in self._handle_content_block_stop(data):
                yield chunk
            return

        if evt == "message_delta":
            self._handle_message_delta(data)
            return

        if evt == "message_stop":
            # 终态聚合统一在 _finalize 里做（流读完时触发），message_stop 自身不 yield
            return

        # 其它未知事件类型：静默忽略，不抛错（防御 Anthropic 后续新增事件类型）

    # ------------------------------------------------------------------
    # 各事件处理
    # ------------------------------------------------------------------

    def _handle_message_start(self, data: dict[str, Any]) -> None:
        message = data.get("message") or {}
        if not isinstance(message, dict):
            return

        msg_id = message.get("id")
        if isinstance(msg_id, str):
            self._state.message_id = msg_id
        model_name = message.get("model")
        if isinstance(model_name, str):
            self._state.model_name = model_name

        usage = message.get("usage") or {}
        if isinstance(usage, dict):
            input_tokens = usage.get("input_tokens")
            if isinstance(input_tokens, int):
                self._state.input_tokens = input_tokens
            cache_creation = usage.get("cache_creation_input_tokens")
            if isinstance(cache_creation, int):
                self._state.cache_creation_input_tokens = cache_creation
            cache_read = usage.get("cache_read_input_tokens")
            if isinstance(cache_read, int):
                self._state.cache_read_input_tokens = cache_read
            # message_start 也可能给 output_tokens=0，累加到 state
            output_tokens = usage.get("output_tokens")
            if isinstance(output_tokens, int):
                self._state.output_tokens = output_tokens

    async def _handle_content_block_start(
        self,
        data: dict[str, Any],
    ) -> AsyncIterator[LLMStreamChunk]:
        index = data.get("index")
        if not isinstance(index, int):
            return
        block_data = data.get("content_block") or {}
        if not isinstance(block_data, dict):
            return

        bt = block_data.get("type")
        if bt == "text":
            self._state.content_blocks[index] = _AnthropicBlock(block_type="text")
            return
        if bt == "thinking":
            self._state.content_blocks[index] = _AnthropicBlock(block_type="thinking")
            return
        if bt == "tool_use":
            tool_use_id = block_data.get("id") or ""
            tool_name = block_data.get("name") or ""
            block = _AnthropicBlock(
                block_type="tool_use",
                tool_use_id=tool_use_id if isinstance(tool_use_id, str) else "",
                tool_name=tool_name if isinstance(tool_name, str) else "",
            )
            self._state.content_blocks[index] = block
            # 立即 yield tool_call.start（满足"start 必须在 arguments.delta 之前"不变式）
            block.tool_call_started = True
            yield LLMStreamChunk(
                kind="tool_call.start",
                index=index,
                tool_call_id=block.tool_use_id or None,
                tool_name=block.tool_name or "",
            )
            return
        # 其它 block 类型（如未来 server_tool_use 等）：暂不支持，注册为空 text 占位
        self._state.content_blocks[index] = _AnthropicBlock(block_type="text")

    async def _handle_content_block_delta(
        self,
        data: dict[str, Any],
    ) -> AsyncIterator[LLMStreamChunk]:
        index = data.get("index")
        if not isinstance(index, int):
            return
        block = self._state.content_blocks.get(index)
        if block is None:
            # 防御：delta 提到了未注册的 block（一般不会，Anthropic 严格保证 start
            # 在 delta 之前）；按 text block 兜底注册，避免 KeyError
            block = _AnthropicBlock(block_type="text")
            self._state.content_blocks[index] = block

        delta = data.get("delta") or {}
        if not isinstance(delta, dict):
            return
        delta_type = delta.get("type")

        if delta_type == "text_delta":
            text_piece = delta.get("text")
            if isinstance(text_piece, str) and text_piece and block.block_type == "text":
                block.text += text_piece
                yield LLMStreamChunk(kind="content.delta", delta=text_piece, index=0)
            return

        if delta_type == "thinking_delta":
            thinking_piece = delta.get("thinking")
            if (
                isinstance(thinking_piece, str)
                and thinking_piece
                and block.block_type == "thinking"
            ):
                block.text += thinking_piece
                yield LLMStreamChunk(kind="reasoning.delta", delta=thinking_piece)
            return

        if delta_type == "input_json_delta":
            partial = delta.get("partial_json")
            if isinstance(partial, str) and partial and block.block_type == "tool_use":
                block.tool_input_json += partial
                yield LLMStreamChunk(
                    kind="tool_call.arguments.delta",
                    index=index,
                    delta=partial,
                    tool_call_id=block.tool_use_id or None,
                )
            return

        # citations_delta / signature_delta 等：V1 不消费（thinking signature 链路
        # 不在 v1-mini 范围，与非流式路径行为一致）

    async def _handle_content_block_stop(
        self,
        data: dict[str, Any],
    ) -> AsyncIterator[LLMStreamChunk]:
        index = data.get("index")
        if not isinstance(index, int):
            return
        block = self._state.content_blocks.get(index)
        if block is None:
            return
        if block.block_type == "tool_use":
            # 兜底：若 content_block_start 没成功 yield start（异常情况），先补一次
            if not block.tool_call_started:
                block.tool_call_started = True
                yield LLMStreamChunk(
                    kind="tool_call.start",
                    index=index,
                    tool_call_id=block.tool_use_id or None,
                    tool_name=block.tool_name or "",
                )
            yield LLMStreamChunk(
                kind="tool_call.end",
                index=index,
                tool_call_id=block.tool_use_id or None,
                tool_name=block.tool_name or "",
            )

    def _handle_message_delta(self, data: dict[str, Any]) -> None:
        delta = data.get("delta") or {}
        if isinstance(delta, dict):
            stop_reason = delta.get("stop_reason")
            if isinstance(stop_reason, str):
                self._state.stop_reason = stop_reason

        usage = data.get("usage") or {}
        if isinstance(usage, dict):
            output_tokens = usage.get("output_tokens")
            if isinstance(output_tokens, int):
                # message_delta 给的是当前流总输出 tokens（不是增量），覆盖即可
                self._state.output_tokens = output_tokens

    # ------------------------------------------------------------------
    # 终态聚合
    # ------------------------------------------------------------------

    async def _finalize(self) -> AsyncIterator[LLMStreamChunk]:
        # 守卫 1：从未见 message_start
        if self._state.message_id is None:
            raise ProviderError(
                "Anthropic stream ended without receiving message_start event",
                details={"chunk_count": self._state.chunk_count},
            )

        # 守卫 2：见到 message_start 但既没 stop_reason 也没任何 tool_use block 完成
        # （tool_use block 的 content_block_stop 已经触发就算正常推进）
        has_completed_tool_use = any(
            b.block_type == "tool_use" and b.tool_call_started
            for b in self._state.content_blocks.values()
        )
        if self._state.stop_reason is None and not has_completed_tool_use:
            raise ProviderError(
                "Anthropic stream ended without stop_reason or completed content blocks",
                details={
                    "chunk_count": self._state.chunk_count,
                    "message_id": self._state.message_id,
                },
            )

        message = self._build_message()
        finish_reason = self._normalize_stop_reason(
            self._state.stop_reason or "end_turn",
            message_has_tool_calls=bool(message.tool_calls),
        )
        usage = self._build_usage()
        provider_metadata = self._build_provider_metadata()

        yield LLMStreamChunk(
            kind="message.done",
            message=message,
            finish_reason=finish_reason,
            usage=usage,
            provider_metadata=provider_metadata,
        )

    def _build_message(self) -> Message:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        # 按 block index 升序拼装（与 Anthropic 原顺序一致）
        for idx in sorted(self._state.content_blocks.keys()):
            block = self._state.content_blocks[idx]
            if block.block_type == "text":
                if block.text:
                    text_parts.append(block.text)
                continue
            if block.block_type == "tool_use":
                args_raw = block.tool_input_json
                arguments: dict[str, Any]
                if args_raw and args_raw.strip():
                    try:
                        loaded = json.loads(args_raw)
                        arguments = loaded if isinstance(loaded, dict) else {"__raw__": loaded}
                    except json.JSONDecodeError:
                        # 与既有非流式路径一致：解析失败时保留原始字符串
                        arguments = {"__raw__": args_raw}
                else:
                    arguments = {}
                tool_calls.append(
                    ToolCall(
                        call_id=block.tool_use_id or f"call_{uuid.uuid4().hex[:10]}",
                        tool_name=block.tool_name or "",
                        arguments=arguments,
                    )
                )
                continue
            # thinking block：累计在 provider_metadata.reasoning_content；
            # 不进 Message.content（与非流式 anthropic_messages._parse_response 一致）

        content_text = "".join(text_parts) if text_parts else None
        return Message.assistant(
            content=content_text,
            tool_calls=tool_calls or None,
        )

    def _build_usage(self) -> dict[str, Any]:
        # task#3.1：透传 SDK 原生字段 + provider_kind，让 web.usage_token 按 channel 解析
        cache_read = self._state.cache_read_input_tokens or 0
        cache_creation = self._state.cache_creation_input_tokens or 0
        # 派生 prompt_tokens = input + cache_read + cache_creation（**修 lossy bug**：
        # 不含 cache 两项的旧实现会让 web 显示的输入 token 比真实小 N 万）
        prompt_tokens = self._state.input_tokens + cache_read + cache_creation
        out: dict[str, Any] = {
            "provider_kind": "anthropic",
            # Anthropic 原生字段
            "input_tokens": self._state.input_tokens,
            "output_tokens": self._state.output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
            # 兼容老消费者
            "prompt_tokens": prompt_tokens,
            "completion_tokens": self._state.output_tokens,
            "total_tokens": prompt_tokens + self._state.output_tokens,
        }
        return out

    def _build_provider_metadata(self) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        if self._state.message_id is not None:
            meta["id"] = self._state.message_id
        if self._state.model_name is not None:
            meta["model"] = self._state.model_name
        if self._state.cache_creation_input_tokens is not None:
            meta["cache_creation_input_tokens"] = self._state.cache_creation_input_tokens
        if self._state.cache_read_input_tokens is not None:
            meta["cache_read_input_tokens"] = self._state.cache_read_input_tokens

        # thinking 累积内容写到 reasoning_content（截断 500，对齐非流式 / OpenAI parser）
        thinking_parts: list[str] = []
        for idx in sorted(self._state.content_blocks.keys()):
            block = self._state.content_blocks[idx]
            if block.block_type == "thinking" and block.text:
                thinking_parts.append(block.text)
        if thinking_parts:
            full = "".join(thinking_parts)
            meta["reasoning_content"] = full[:500]
            meta["reasoning_content_length"] = len(full)

        return meta

    @staticmethod
    def _normalize_stop_reason(
        raw: str,
        *,
        message_has_tool_calls: bool,
    ) -> FinishReason:
        """与既有非流式 ``AnthropicMessagesProvider._normalize_stop_reason`` 保持一致。"""
        if message_has_tool_calls or raw == "tool_use":
            return "tool_calls"
        if raw in ("end_turn", "stop_sequence"):
            return "stop"
        if raw == "max_tokens":
            return "length"
        if raw == "refusal":
            return "error"
        return "other"


__all__ = ["AnthropicStreamParser"]
