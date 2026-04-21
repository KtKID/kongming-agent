"""OpenAI-compatible provider adapter。

**关于文件名**：按 ``docs/kongming-agent-v1-minimal/11-v1-file-layout.md`` 与
``plan.md``，本文件命名为 ``openai_responses.py``，名字源自"OpenAI Responses
API adapter"的规划标签。但：

- 当前 v1-mini 第一版实际走 **Chat Completions** 端点（``{base_url}/chat/completions``）。
- 原因：LM Studio / Ollama / vLLM 等本地 OpenAI-compatible 服务对 chat
  completions 的兼容度远高于 Responses API；v1-mini 本地基线模型
  ``gemma-4-e4b-it`` 也走 chat completions。
- 未来若切换到 Responses API 端点，``core.contracts.LLMProvider`` Protocol
  形状不需要改；只在本文件内部切换 ``_endpoint`` 与请求/响应映射即可。
- 文件名保留为 ``openai_responses.py`` 是为了和已落地的规划文档保持一致，
  避免双份文件布局引用。

**不做流式**：v1-mini 第一版只走非流式。留给 :class:`BaseLLMProvider` 的扩展
点承接。流式落地交给后续批次（批次 7 observability 之后，或独立批次）。

**httpx 客户端生命周期**：
- 每次 :meth:`_do_complete` 开启一个 ``httpx.AsyncClient`` 上下文；
  本地 / 远端模型单次请求都在同一上下文里完成，退出时自动关闭连接。
- 不做单例、不做连接池持有——v1-mini 第一版请求并发度非常低（CLI 单用户
  串行），把连接池复用的工程化改造放到后续批次，配合真正的并发压测再决定。
"""

from __future__ import annotations

from typing import Any, Literal

import httpx

from config_loader.models import ModelConfig
from core.contracts import LLMRequest, LLMResponse
from core.errors import ProviderError
from executors.llm.base import BaseLLMProvider
from executors.llm.raw_dump import dump_raw_llm_interaction

FinishReasonStr = Literal["stop", "tool_calls", "length", "error", "other"]


class OpenAIResponsesProvider(BaseLLMProvider):
    """OpenAI-compatible provider。

    走 ``{base_url}/chat/completions`` 端点。本地服务允许 ``api_key``
    为空——此时不带 ``Authorization`` 头，兼容 LM Studio / Ollama 等。
    """

    # 非流式 endpoint。版本段（/v1、/v4 等）由调用方通过 base_url 携带，
    # 这里只拼路径末段，避免双重版本拼接。
    _ENDPOINT_PATH = "/chat/completions"

    def __init__(
        self,
        *,
        model_config: ModelConfig,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        enable_raw_dump: bool = False,
    ) -> None:
        super().__init__(
            model_config=model_config,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
        )
        # 配置驱动的 raw dump 开关；装配层从 cfg.trace.raw_llm 传入。
        # env KONGMING_TRACE_RAW_LLM=1 通过 config_loader 的 env 覆盖链路
        # 会自动把 cfg.trace.raw_llm 变成 True，这里只读最终结果。
        self._enable_raw_dump = enable_raw_dump

    async def _do_complete(self, request: LLMRequest) -> LLMResponse:
        """实现一次真实 chat completion 请求。

        Raises:
            ProviderError: HTTP 错误、鉴权错误、响应结构不合法等。
        """
        url = self._model_config.base_url.rstrip("/") + self._ENDPOINT_PATH
        payload = self._build_payload(request)
        headers = self._build_headers()

        # 请求级超时：使用 LLMRequest.timeout_seconds（若给）或 model.timeout。
        per_call_timeout = (
            request.timeout_seconds
            if request.timeout_seconds is not None
            else self._model_config.timeout
        )

        try:
            async with httpx.AsyncClient(timeout=per_call_timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            # 让 base.complete 按 TimeoutError 心智处理（触发重试）。
            raise TimeoutError(f"httpx timeout calling {url}: {exc}") from exc
        except httpx.HTTPError as exc:
            # 其它连接层错误走通用重试路径。
            raise ConnectionError(f"httpx error calling {url}: {exc}") from exc

        # 宽松解析响应 body：先尝试 JSON，失败则保留原文占位。这样 raw_dump 在
        # 4xx/5xx 或 non-JSON 场景下都能把内容 dump 到磁盘供排查。
        parse_error: str | None = None
        body_for_dump: Any
        try:
            body_for_dump = response.json()
        except ValueError:
            body_for_dump = {"__raw_text__": response.text}
            parse_error = "non-json-body"

        # opt-in dump：由 `cfg.trace.raw_llm`（或 env KONGMING_TRACE_RAW_LLM=1
        # 自动覆盖到此配置）控制；默认关。
        dump_raw_llm_interaction(
            enabled=self._enable_raw_dump,
            provider="openai_responses",
            url=url,
            request_payload=payload,
            request_headers=headers,
            response_status=response.status_code,
            response_headers=dict(response.headers),
            response_body=body_for_dump,
            error=(f"HTTP {response.status_code}" if response.status_code >= 400 else parse_error),
        )

        if response.status_code >= 400:
            # 4xx / 5xx 都直接落为 ProviderError（不经重试）；如果是 5xx 想重试，
            # 未来可以覆盖 _is_retryable。v1-mini 先保守：让失败立即可见。
            detail_text = response.text[:2000] if response.text else ""
            raise ProviderError(
                f"LLM provider HTTP {response.status_code}: {detail_text}",
                details={
                    "status_code": response.status_code,
                    "url": url,
                    "model": self._model_config.name,
                },
            )

        if parse_error is not None:
            raise ProviderError(
                f"LLM provider returned non-JSON body: {response.text[:500]!r}",
                details={"url": url},
            )

        return self._parse_response(body_for_dump)

    # ------------------------------------------------------------------
    # 请求构造
    # ------------------------------------------------------------------

    def _build_payload(self, request: LLMRequest) -> dict[str, Any]:
        """组装 chat completions 请求体。"""
        messages = self.messages_to_openai_format(request.messages)
        payload: dict[str, Any] = {
            "model": request.model or self._model_config.name,
            "messages": messages,
            "stream": False,
        }

        # 采样参数：request 层覆盖 model 配置层。
        temperature = (
            request.temperature
            if request.temperature is not None
            else self._model_config.temperature
        )
        max_tokens = (
            request.max_tokens if request.max_tokens is not None else self._model_config.max_tokens
        )
        payload["temperature"] = temperature
        payload["max_tokens"] = max_tokens

        if request.tools:
            payload["tools"] = self.tools_to_openai_format(list(request.tools))
            # tool_choice 留给 provider 默认值（通常是 ``auto``）；未来要暴露成
            # 可配参数时，从 LLMRequest.metadata 读。
            payload["tool_choice"] = "auto"

        # 允许调用方通过 metadata 追加 provider 特定字段（v1-mini 先不展开解释
        # 每个字段；透传便于调试）。
        extra = request.metadata.get("provider_extra") if request.metadata else None
        if isinstance(extra, dict):
            for k, v in extra.items():
                # 不允许覆盖核心字段。
                if k not in payload:
                    payload[k] = v

        return payload

    def _build_headers(self) -> dict[str, str]:
        """按 api_key 是否为空决定要不要带 Authorization 头。

        本地模型（api_key 为空）：无鉴权头，兼容 LM Studio / Ollama 等。
        远端模型：标准 Bearer token。
        """
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._model_config.api_key:
            headers["Authorization"] = f"Bearer {self._model_config.api_key}"
        return headers

    # ------------------------------------------------------------------
    # 响应解析
    # ------------------------------------------------------------------

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        """把 chat completions JSON 转成 :class:`LLMResponse`。"""
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(
                "LLM provider returned empty choices list",
                details={"model": self._model_config.name, "raw_keys": list(data.keys())},
            )

        choice = choices[0]
        message = self.openai_choice_to_message(choice)
        finish_reason_raw = choice.get("finish_reason") or "stop"
        finish_reason = self._normalize_finish_reason(
            finish_reason_raw, message_has_tool_calls=bool(message.tool_calls)
        )

        usage = data.get("usage") or {}
        # 只保留平铺后的 int 字段；复杂嵌套结构留给未来 observability 消化。
        normalized_usage: dict[str, Any] = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if key in usage:
                normalized_usage[key] = usage[key]

        return LLMResponse(
            message=message,
            finish_reason=finish_reason,
            usage=normalized_usage,
            raw=data,
        )

    @staticmethod
    def _normalize_finish_reason(raw: str, *, message_has_tool_calls: bool) -> FinishReasonStr:
        """把 provider 的 finish_reason 映射到 :data:`core.contracts.FinishReason`。

        - ``tool_calls`` → ``tool_calls``
        - ``stop`` / ``end_turn`` → ``stop``
        - ``length`` / ``max_tokens`` → ``length``
        - ``content_filter`` / ``error`` → ``error``
        - 其它 → ``other``

        另外：如果消息里带 tool_calls，但 provider 仍然给 ``stop``（有些
        兼容实现会这样），统一按 ``tool_calls`` 处理，避免 runner 错误终止。
        """
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
        return "other"


__all__ = ["OpenAIResponsesProvider"]
