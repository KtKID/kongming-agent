"""Anthropic Messages API provider。

走 ``{base_url}/v1/messages`` 端点（原生 Anthropic Messages API）。

与 OpenAI Chat Completions 的主要差异：

- 系统消息提取到请求顶层的 ``system`` 字段，不混入 ``messages`` 数组。
- assistant tool_calls 转成 ``{"type": "tool_use", ...}`` content block。
- tool result 作为 user 角色的 ``{"type": "tool_result", ...}`` content block。
- Tools schema 不带 ``function`` 包装层，使用 ``input_schema``。
- 响应用 ``stop_reason`` 而非 ``finish_reason``；usage 字段名也不同。
- 鉴权头：``x-api-key``；必须携带 ``anthropic-version: 2023-06-01``。

**base_url 约定**：``base_url`` 由调用方设置（默认 ``https://api.anthropic.com``），
provider 内部拼 ``/v1/messages``——与 OpenAI provider 约定一致，版本段在 base_url 里。
但 Anthropic 官方 base_url 本身不含 ``/v1``，所以 ``_ENDPOINT_PATH = "/v1/messages"``。

**api_key 要求**：Anthropic 是远端服务，``is_local`` 恒 False，pydantic 校验已确保
``api_key`` 非空，这里无需单独判断。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

import httpx

from config_loader.models import ModelConfig
from core.contracts import LLMRequest, LLMResponse
from core.errors import ProviderError
from core.message import Message, ToolCall
from executors.llm.base import BaseLLMProvider
from executors.llm.reasoning import ReasoningConfig, resolve_reasoning_plan


class AnthropicMessagesProvider(BaseLLMProvider):
    """原生 Anthropic Messages API provider。

    走 ``{base_url}/v1/messages`` 端点。
    """

    _ENDPOINT_PATH = "/v1/messages"
    _ANTHROPIC_VERSION = "2023-06-01"

    def __init__(
        self,
        *,
        model_config: ModelConfig,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
    ) -> None:
        super().__init__(
            model_config=model_config,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
        )

    async def _do_complete(self, request: LLMRequest) -> LLMResponse:
        url = self._model_config.base_url.rstrip("/") + self._ENDPOINT_PATH
        payload = self._build_payload(request)
        headers = self._build_headers()

        per_call_timeout = (
            request.timeout_seconds
            if request.timeout_seconds is not None
            else self._model_config.timeout
        )

        client = self._ensure_client()
        try:
            response = await client.post(
                url, json=payload, headers=headers, timeout=per_call_timeout
            )
        except httpx.TimeoutException as exc:
            raise TimeoutError(f"httpx timeout calling {url}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ConnectionError(f"httpx error calling {url}: {exc}") from exc

        if response.status_code >= 400:
            detail_text = response.text[:2000] if response.text else ""
            raise ProviderError(
                f"LLM provider HTTP {response.status_code}: {detail_text}",
                details={
                    "status_code": response.status_code,
                    "url": url,
                    "model": self._model_config.name,
                },
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                f"LLM provider returned non-JSON body: {response.text[:500]!r}",
                details={"url": url},
            ) from exc

        return self._parse_response(data)

    # ------------------------------------------------------------------
    # 请求构造
    # ------------------------------------------------------------------

    def _build_payload(self, request: LLMRequest) -> dict[str, Any]:
        """组装 Messages API 请求体。

        Anthropic 格式要求：
        - ``system`` 字段在顶层（不作为 messages 条目）
        - ``messages`` 只含 user / assistant 角色
        - tool_calls → ``tool_use`` content blocks
        - tool results → user 角色的 ``tool_result`` content blocks
        """
        system_text, messages = self._split_system_and_convert(list(request.messages))

        payload: dict[str, Any] = {
            "model": request.model or self._model_config.name,
            "messages": messages,
            "max_tokens": (
                request.max_tokens
                if request.max_tokens is not None
                else self._model_config.max_tokens
            ),
        }

        if system_text:
            payload["system"] = system_text

        temperature = (
            request.temperature
            if request.temperature is not None
            else self._model_config.temperature
        )
        payload["temperature"] = temperature

        if request.tools:
            payload["tools"] = self._tools_to_anthropic_format(list(request.tools))

        # reasoning_effort：从 model_config 读取，按最终模型名匹配 profile。
        # 注意：与 OpenAI provider 不同，Anthropic provider 没有硬编码回退路径。
        # 原因：Anthropic 历史上没有内置的 reasoning 参数映射；V1 只通过配置驱动
        # 的 reasoning_profiles 支持 MiniMax 等兼容层模型。若 profiles 为空，
        # 则 reasoning_effort 配置静默不生效——这是有意设计，不是遗漏。
        effort = self._model_config.reasoning_effort
        profiles = self._model_config.reasoning_profiles
        if effort is not None and profiles:
            config = ReasoningConfig(enabled=True, effort=effort)
            plan = resolve_reasoning_plan(payload["model"], config, profiles)
            if plan.send_reasoning:
                payload.update(plan.payload_patch)

        extra = request.metadata.get("provider_extra") if request.metadata else None
        if isinstance(extra, dict):
            for k, v in extra.items():
                if k not in payload:
                    payload[k] = v

        return payload

    def _build_headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._model_config.api_key,
            "anthropic-version": self._ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    # ------------------------------------------------------------------
    # 消息格式转换
    # ------------------------------------------------------------------

    @staticmethod
    def _split_system_and_convert(
        messages: list[Message],
    ) -> tuple[str, list[dict[str, Any]]]:
        """提取 system 消息，转换其余消息为 Anthropic 格式。

        Returns:
            (system_text, anthropic_messages) — system 合并文本和消息列表。
            system_text 为空字符串时调用方不传 ``system`` 字段。
        """
        system_parts: list[str] = []
        out: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == "system":
                if msg.content:
                    system_parts.append(msg.content)
                continue

            if msg.role == "user":
                out.append({"role": "user", "content": msg.content or ""})
                continue

            if msg.role == "tool":
                # tool result → user 角色的 tool_result block
                call_id = msg.tool_call_id or f"missing_{uuid.uuid4().hex[:8]}"
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": msg.content or "",
                }
                # 尝试合并到上一条 user 消息（Anthropic 要求 tool_result 在 user 角色内）
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                elif out and out[-1]["role"] == "user":
                    # 把上一条 user string content 升格为 list
                    prev_content = out[-1]["content"]
                    out[-1]["content"] = (
                        [{"type": "text", "text": prev_content}] if prev_content else []
                    )
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
                continue

            if msg.role == "assistant":
                content: list[dict[str, Any]] = []
                if msg.content:
                    content.append({"type": "text", "text": msg.content})
                if msg.tool_calls:
                    for call in msg.tool_calls:
                        content.append(
                            {
                                "type": "tool_use",
                                "id": call.call_id,
                                "name": call.tool_name,
                                "input": call.arguments,
                            }
                        )
                out.append({"role": "assistant", "content": content or ""})
                continue

        system_text = "\n\n".join(system_parts)
        return system_text, out

    @staticmethod
    def _tools_to_anthropic_format(tools: list[Any]) -> list[dict[str, Any]]:
        """把 Tool 列表转成 Anthropic tools schema。"""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in tools
        ]

    # ------------------------------------------------------------------
    # 响应解析
    # ------------------------------------------------------------------

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        """把 Messages API JSON 转成 :class:`LLMResponse`。"""
        content_blocks = data.get("content") or []
        if not content_blocks:
            raise ProviderError(
                "LLM provider returned empty content list",
                details={"model": self._model_config.name, "raw_keys": list(data.keys())},
            )

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(block.get("text") or "")
            elif block_type == "tool_use":
                raw_input = block.get("input") or {}
                # Anthropic 的 input 是 dict；与 OpenAI arguments 字符串不同
                if isinstance(raw_input, str):
                    try:
                        raw_input = json.loads(raw_input)
                    except json.JSONDecodeError:
                        raw_input = {"__raw__": raw_input}
                tool_calls.append(
                    ToolCall(
                        call_id=block.get("id") or f"call_{uuid.uuid4().hex[:10]}",
                        tool_name=block.get("name") or "",
                        arguments=raw_input if isinstance(raw_input, dict) else {},
                    )
                )

        content_text: str | None = "\n".join(text_parts) if text_parts else None
        message = Message.assistant(
            content=content_text,
            tool_calls=tool_calls or None,
        )

        stop_reason_raw = data.get("stop_reason") or "end_turn"
        finish_reason = self._normalize_stop_reason(
            stop_reason_raw, message_has_tool_calls=bool(tool_calls)
        )

        usage_raw = data.get("usage") or {}
        normalized_usage: dict[str, Any] = {}
        # Anthropic: input_tokens / output_tokens → 映射成通用键
        if "input_tokens" in usage_raw:
            normalized_usage["prompt_tokens"] = usage_raw["input_tokens"]
        if "output_tokens" in usage_raw:
            normalized_usage["completion_tokens"] = usage_raw["output_tokens"]
        if "prompt_tokens" in normalized_usage and "completion_tokens" in normalized_usage:
            normalized_usage["total_tokens"] = (
                normalized_usage["prompt_tokens"] + normalized_usage["completion_tokens"]
            )

        # 厂商扩展字段：保留原始 Anthropic 字段名。
        provider_metadata: dict[str, Any] = {}

        # cache token 细分（Anthropic 的 prompt caching）
        for cache_key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
            if cache_key in usage_raw:
                provider_metadata[cache_key] = usage_raw[cache_key]

        # response 级标识
        for field_name in ("id", "model", "stop_sequence"):
            if field_name in data:
                provider_metadata[field_name] = data[field_name]

        return LLMResponse(
            message=message,
            finish_reason=finish_reason,
            usage=normalized_usage,
            provider_metadata=provider_metadata,
            raw=data,
        )

    @staticmethod
    def _normalize_stop_reason(
        raw: str, *, message_has_tool_calls: bool
    ) -> Literal["stop", "tool_calls", "length", "error", "other"]:
        if message_has_tool_calls or raw == "tool_use":
            return "tool_calls"
        if raw in ("end_turn", "stop_sequence"):
            return "stop"
        if raw == "max_tokens":
            return "length"
        return "other"


__all__ = ["AnthropicMessagesProvider"]
