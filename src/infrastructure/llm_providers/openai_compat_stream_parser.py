"""OpenAI-compatible Chat Completions stream parser。

把 OpenAI / GLM / DeepSeek / Kimi / Qwen / LM Studio / Ollama / vLLM 等
OpenAI-compatible chat completions 流式响应的 chunk 序列归一化为
provider-agnostic 的 :class:`LLMStreamChunk` 序列。

状态机对齐 ``other/hermes-agent/run_agent.py:4744-4852``：

- ``delta.reasoning_content or delta.reasoning`` 双字段探测（只取其一不双倍）
- ``delta.content`` 累积 + yield
- ``delta.tool_calls`` 按 ``index`` 聚合，含 Ollama / LM Studio 对并行 tool_call
  重用同一 ``index=0`` 的防御（见 Hermes 4803-4816）
- 流结束阶段对每个 tool_call 的 arguments 跑 ``json.loads`` 校验，任一 fail
  → ``finish_reason="length"``（对齐 Hermes 4863-4867）
- 终态 yield ``kind="message.done"``，message 等价非流式 :class:`Message`

详细字段映射 / 状态字段说明见
``docs/llm-provider-v0.2/streaming/04-data-and-state.md`` §2.1 / §4。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from core.contracts import (
    FinishReason,
    LLMStreamChunk,
    ProviderUsageFamily,
    ProviderUsageSnapshot,
)
from core.message import Message, ToolCall
from infrastructure.llm_providers.usage import ProviderUsageManager


@dataclass
class _PartialCall:
    """单个 tool_call 的累积状态，对应 Hermes ``run_agent.py:4819-4824`` 的 dict。

    字符串字段使用 ``+=`` 累积（OpenAI 约定 ``function.arguments`` 是 JSON 字符串流）。
    """

    id: str = ""
    name: str = ""
    arguments: str = ""
    extra_content: Any = None

    @property
    def arguments_is_valid_json(self) -> bool:
        if not self.arguments or not self.arguments.strip():
            # 空 arguments 不算截断
            return True
        try:
            json.loads(self.arguments)
            return True
        except json.JSONDecodeError:
            return False


class OpenAICompatStreamParser:
    """把 OpenAI-compatible chunk 序列归一化为 :class:`LLMStreamChunk` 序列。

    单次实例只处理一条流；复用同一个 parser 对象处理多条流会复用状态字段，
    语义未定义——对每条流构造新实例。
    """

    def __init__(self, *, usage_manager: ProviderUsageManager | None = None) -> None:
        """初始化单流 parser，输入为 usage 门户，输出为空。"""
        # 内容累积
        self._content_parts: list[str] = []
        self._reasoning_parts: list[str] = []

        # tool_calls 累积：key 为 slot（Ollama 防御后的最终槽位）
        self._tool_calls_acc: dict[int, _PartialCall] = {}

        # Ollama / LM Studio 并行 tool_call 防御
        self._last_id_at_idx: dict[int, str] = {}
        self._active_slot_by_idx: dict[int, int] = {}

        # tool_call.start 节流（同一 slot 只发一次）
        self._tool_gen_notified: set[int] = set()

        # 流级元数据
        self._finish_reason: str | None = None
        manager = usage_manager or ProviderUsageManager()
        self._usage_stream = manager.start_stream(ProviderUsageFamily.OPENAI_CHAT_COMPLETIONS)
        self._model_name: str | None = None
        self._response_id: str | None = None
        self._system_fingerprint: str | None = None

        # 计数（供 llm.stream.end payload 填充，也方便上层 infrastructure.tracing）
        self.chunk_count: int = 0

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    async def parse(
        self,
        events: AsyncIterator[tuple[str | None, dict[str, Any]]],
    ) -> AsyncIterator[LLMStreamChunk]:
        """消费 ``(event_name, chunk_dict)`` 序列，产出 :class:`LLMStreamChunk` 流。

        `event_name` 由 OpenAI-compatible provider 忽略（OpenAI chat completions
        流只有 `data:` 行，`event_name` 始终为 ``None``）。
        """

        async for _, chunk in events:
            self.chunk_count += 1
            async for produced in self._consume_chunk(chunk):
                yield produced

        # 流读完 → 构造终态
        async for produced in self._finalize():
            yield produced

    # ------------------------------------------------------------------
    # chunk 处理
    # ------------------------------------------------------------------

    async def _consume_chunk(self, chunk: dict[str, Any]) -> AsyncIterator[LLMStreamChunk]:
        # 顶层元数据（每个 chunk 都尝试抽）
        if "id" in chunk and isinstance(chunk.get("id"), str):
            self._response_id = chunk["id"]
        if "model" in chunk and isinstance(chunk.get("model"), str):
            self._model_name = chunk["model"]
        if "system_fingerprint" in chunk and isinstance(chunk.get("system_fingerprint"), str):
            self._system_fingerprint = chunk["system_fingerprint"]
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self._usage_stream.ingest(usage, terminal=True)

        choices = chunk.get("choices") or []
        if not choices:
            # 空 choices（心跳 / usage-only chunk）—— 只抽顶层元数据，不再处理 delta
            return

        # OpenAI chat completions 约定 choices[0] 即可；多 choice 不在 V1 支持
        choice = choices[0]
        if not isinstance(choice, dict):
            return

        finish_reason_raw = choice.get("finish_reason")
        if isinstance(finish_reason_raw, str) and finish_reason_raw:
            self._finish_reason = finish_reason_raw

        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            return

        # --- reasoning：双字段探测，只取其一（Hermes run_agent.py:4766）
        reasoning_text = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning_text, str) and reasoning_text:
            self._reasoning_parts.append(reasoning_text)
            yield LLMStreamChunk(kind="reasoning.delta", delta=reasoning_text)

        # --- content
        content_text = delta.get("content")
        if isinstance(content_text, str) and content_text:
            self._content_parts.append(content_text)
            yield LLMStreamChunk(kind="content.delta", delta=content_text, index=0)

        # --- tool_calls
        tool_calls_delta = delta.get("tool_calls")
        if isinstance(tool_calls_delta, list) and tool_calls_delta:
            async for produced in self._apply_tool_calls_delta(tool_calls_delta):
                yield produced

    async def _apply_tool_calls_delta(self, tc_deltas: list[Any]) -> AsyncIterator[LLMStreamChunk]:
        for tc_delta in tc_deltas:
            if not isinstance(tc_delta, dict):
                continue

            # --- Ollama 防御（对齐 Hermes run_agent.py:4732-4737 + 4803-4816）
            raw_idx_val = tc_delta.get("index")
            raw_idx = raw_idx_val if isinstance(raw_idx_val, int) else 0

            delta_id_val = tc_delta.get("id")
            delta_id = delta_id_val if isinstance(delta_id_val, str) else ""

            # 若本 slot 首次出现：用 raw_idx 作为初始 slot
            if raw_idx not in self._active_slot_by_idx:
                self._active_slot_by_idx[raw_idx] = raw_idx

            # "同一 raw_idx 但出现了新 id" → 分新 slot（Ollama / LM Studio 并行 tool_call）
            if (
                delta_id
                and raw_idx in self._last_id_at_idx
                and delta_id != self._last_id_at_idx[raw_idx]
            ):
                new_slot = max(self._tool_calls_acc.keys(), default=-1) + 1
                # 避免与已占用的 raw_idx 冲突：如果 new_slot 已经是 active 里的某个值，
                # 正常情况下不会（_tool_calls_acc 里已有 new_slot-1 之前的）。
                self._active_slot_by_idx[raw_idx] = new_slot

            if delta_id:
                self._last_id_at_idx[raw_idx] = delta_id

            slot = self._active_slot_by_idx[raw_idx]
            part = self._tool_calls_acc.get(slot)
            if part is None:
                part = _PartialCall()
                self._tool_calls_acc[slot] = part

            # 累加 id / function.name / function.arguments
            if delta_id and not part.id:
                part.id = delta_id

            func = tc_delta.get("function")
            if isinstance(func, dict):
                name_piece = func.get("name")
                if isinstance(name_piece, str) and name_piece:
                    part.name += name_piece
                args_piece = func.get("arguments")
                if isinstance(args_piece, str) and args_piece:
                    part.arguments += args_piece

            # extra_content：优先 tc_delta.extra_content，fallback model_extra["extra_content"]
            # 对齐 Hermes run_agent.py:4833-4839
            ec_primary = tc_delta.get("extra_content")
            if ec_primary is not None:
                part.extra_content = ec_primary
            else:
                model_extra = tc_delta.get("model_extra")
                if isinstance(model_extra, dict):
                    ec_fallback = model_extra.get("extra_content")
                    if ec_fallback is not None:
                        part.extra_content = ec_fallback

            # --- 节流：拿到完整 name 且未发过 tool_call.start 时首发
            if slot not in self._tool_gen_notified and part.name:
                self._tool_gen_notified.add(slot)
                yield LLMStreamChunk(
                    kind="tool_call.start",
                    index=slot,
                    tool_call_id=part.id or None,
                    tool_name=part.name,
                )

            # --- arguments 增量：拿到非空 arguments 片段就 yield（但只在已发过
            # tool_call.start 之后；否则先累积 name，等 name 来了再触发 start
            # 并继续 yield 已经累积的 args？——V1 决定：按 chunk 到达顺序 yield
            # 即便此时还没见到 name。因为 OpenAI 会话里 name 一般在首 chunk 就给。
            if isinstance(func, dict):
                args_piece = func.get("arguments")
                if isinstance(args_piece, str) and args_piece:
                    yield LLMStreamChunk(
                        kind="tool_call.arguments.delta",
                        index=slot,
                        delta=args_piece,
                        tool_call_id=part.id or None,
                    )

    # ------------------------------------------------------------------
    # 流结束聚合
    # ------------------------------------------------------------------

    async def _finalize(self) -> AsyncIterator[LLMStreamChunk]:
        # 1. 对每个 tool_call 的 arguments 跑 json.loads 校验
        effective_finish_reason = self._finish_reason or "stop"
        truncated_args = False
        for part in self._tool_calls_acc.values():
            if not part.arguments_is_valid_json:
                effective_finish_reason = "length"
                truncated_args = True

        # 2. 按 slot 升序 yield tool_call.end
        for slot in sorted(self._tool_calls_acc.keys()):
            part = self._tool_calls_acc[slot]
            if slot not in self._tool_gen_notified:
                # 防御：某些 provider 可能在 name 从未出现的情况下已累积 arguments；
                # 补 yield 一次 start 以保持不变式（tool_call.start 必在 end 之前）
                self._tool_gen_notified.add(slot)
                yield LLMStreamChunk(
                    kind="tool_call.start",
                    index=slot,
                    tool_call_id=part.id or None,
                    tool_name=part.name or "",
                )
            yield LLMStreamChunk(
                kind="tool_call.end",
                index=slot,
                tool_call_id=part.id or None,
                tool_name=part.name or "",
            )

        # 3. 拼 Message
        content_text = "".join(self._content_parts) or None
        tool_calls = self._build_tool_calls()

        message = Message.assistant(content=content_text, tool_calls=tool_calls)

        # 4. 归一化 finish_reason
        normalized = self._normalize_finish_reason(
            effective_finish_reason,
            message_has_tool_calls=bool(tool_calls),
            truncated_args=truncated_args,
        )

        # 5. 构造 canonical usage，再从其 raw_usage 提取 provider metadata
        normalized_usage = self._build_usage()
        provider_metadata = self._build_provider_metadata(normalized_usage.raw_usage)

        yield LLMStreamChunk(
            kind="message.done",
            message=message,
            finish_reason=normalized,
            usage=normalized_usage,
            provider_metadata=provider_metadata,
        )

    def _build_tool_calls(self) -> list[ToolCall] | None:
        if not self._tool_calls_acc:
            return None
        out: list[ToolCall] = []
        for slot in sorted(self._tool_calls_acc.keys()):
            part = self._tool_calls_acc[slot]
            call_id = part.id or f"call_{uuid.uuid4().hex[:10]}"
            tool_name = part.name
            args_raw = part.arguments
            arguments: dict[str, Any]
            if args_raw and args_raw.strip():
                try:
                    loaded = json.loads(args_raw)
                    arguments = loaded if isinstance(loaded, dict) else {"__raw__": loaded}
                except json.JSONDecodeError:
                    # 与 base.py:266 既有非流式路径一致：截断时保留原始字符串
                    arguments = {"__raw__": args_raw}
            else:
                arguments = {}
            out.append(ToolCall(call_id=call_id, tool_name=tool_name, arguments=arguments))
        return out

    def _build_provider_metadata(self, usage: dict[str, Any]) -> dict[str, Any]:
        """构造 provider metadata，输入为 canonical raw usage，输出为扩展字段。"""
        provider_metadata: dict[str, Any] = {}

        # reasoning_content（累积拼接，截断到 500 字符；对齐现有非流式路径）
        if self._reasoning_parts:
            full = "".join(self._reasoning_parts)
            provider_metadata["reasoning_content"] = full[:500]
            provider_metadata["reasoning_content_length"] = len(full)

        if self._model_name is not None:
            provider_metadata["model"] = self._model_name
        if self._response_id is not None:
            # 既有非流式路径同时写 id / request_id；流式下 provider 一般只送 id
            provider_metadata["id"] = self._response_id
            provider_metadata["request_id"] = self._response_id
        if self._system_fingerprint is not None:
            provider_metadata["system_fingerprint"] = self._system_fingerprint

        completion_details = usage.get("completion_tokens_details") or {}
        if isinstance(completion_details, dict) and "reasoning_tokens" in completion_details:
            provider_metadata["reasoning_tokens"] = completion_details["reasoning_tokens"]
        prompt_details = usage.get("prompt_tokens_details") or {}
        if isinstance(prompt_details, dict) and "cached_tokens" in prompt_details:
            provider_metadata["cached_tokens"] = prompt_details["cached_tokens"]

        return provider_metadata

    def _build_usage(self) -> ProviderUsageSnapshot:
        """构造终态 usage，输入为已聚合 chunk，输出为 canonical snapshot。"""
        return self._usage_stream.finalize(provider_response_id=self._response_id)

    @staticmethod
    def _normalize_finish_reason(
        raw: str,
        *,
        message_has_tool_calls: bool,
        truncated_args: bool,
    ) -> FinishReason:
        """把 OpenAI-compatible finish_reason 归一化为 :data:`FinishReason`。

        规则与 ``openai_responses.OpenAIResponsesProvider._normalize_finish_reason``
        保持一致；额外：当 ``truncated_args=True`` 时强制降级为 ``length``
        （对齐 Hermes ``_effective_finish_reason``）。
        """
        if truncated_args:
            return "length"
        if message_has_tool_calls:
            return "tool_calls"
        if raw == "tool_calls":
            return "tool_calls"
        if raw in ("stop", "end_turn"):
            return "stop"
        if raw in ("length", "max_tokens"):
            return "length"
        if raw in ("content_filter", "error"):
            return "error"
        # 未具体映射 / 未知原始值：按 other 兜底
        return "other"


__all__ = ["OpenAICompatStreamParser"]
