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

**流式**（v0.2，2026-04-25 接入）：除 :meth:`_do_complete` 非流式外，
:meth:`stream` 实现 :class:`core.contracts.SupportsLLMStream` Protocol。
runner 通过 ``isinstance(llm, SupportsLLMStream)`` 能力探测分派。

**httpx 客户端生命周期**：
- :class:`BaseLLMProvider._ensure_client` 懒加载一个共享 ``httpx.AsyncClient``，
  此后 :meth:`_do_complete` 复用同一个 client 命中 TLS keepalive，省掉
  每次握手的 ~100-300ms 延迟。
- timeout 不绑在 client 级，而是每次 ``.post(..., timeout=...)`` 按 per-request
  覆盖，保证不同 ``LLMRequest.timeout_seconds`` 都生效。
- 关闭由调用方（``NativeRuntime.aclose`` / CLI finally 分支）触发
  :meth:`BaseLLMProvider.aclose` 释放连接池。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, cast

import httpx

from config_loader.models import ModelConfig
from core.contracts import LLMRequest, LLMResponse, LLMStreamChunk
from core.errors import ProviderError
from executors.llm.base import BaseLLMProvider
from executors.llm.openai_compat_stream_parser import OpenAICompatStreamParser
from executors.llm.raw_dump import dump_raw_llm_interaction
from executors.llm.reasoning import EffortLevel, ReasoningConfig, resolve_reasoning_plan
from executors.llm.sse_reader import iter_sse_events
from network.network_log import log_network_exception

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
        stream_read_timeout: float = 120.0,
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
        # 流式 read 超时（秒）。装配层从 cfg.stream.read_timeout 传入；
        # 本地 endpoint（model.is_local=True）会被 stream() 内部自动上调到
        # max(stream_read_timeout, model.timeout)，因为本地推理 token 间隔可能更长。
        self._stream_read_timeout = stream_read_timeout

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

        client = self._ensure_client()
        try:
            response = await client.post(
                url, json=payload, headers=headers, timeout=per_call_timeout
            )
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
    # 流式请求（满足 SupportsLLMStream Protocol）
    # ------------------------------------------------------------------

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        """OpenAI-compatible 流式响应。

        实现 :class:`core.contracts.SupportsLLMStream` Protocol。链路：
        ``httpx.AsyncClient.stream("POST", ...)`` → :func:`iter_sse_events`
        → :class:`OpenAICompatStreamParser` → yield :class:`LLMStreamChunk`。

        - 在 :meth:`_build_payload` 基础上覆盖 ``stream=True`` +
          ``stream_options.include_usage=true``（确保 GLM 等在最后 chunk 送 usage）
        - timeout 分离：connect 30s / read 由 ``stream_read_timeout`` 控制
          （本地 endpoint 自动上调到 ``max(stream_read_timeout, model.timeout)``，
          因为本地推理 token 间隔可能更长）/ write 跟 ``model.timeout``
        - HTTP / SSE 错误抛 :class:`ProviderError`（runner 不靠 iterator 耗尽推断终态）
        - raw_dump 累积所有原始 chunk dict，流结束（不论成功失败）一次性落盘，
          保留 ``self._enable_raw_dump`` opt-in 语义
        """
        url = self._model_config.base_url.rstrip("/") + self._ENDPOINT_PATH
        payload = self._build_payload(request)
        # 覆盖 _build_payload 默认的 stream=False，加 include_usage 让最后 chunk 带 usage
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        headers = self._build_headers()

        # B#4 timeout 分离：本地 endpoint read 超时上调，避免本地推理 token 间隔被截断
        is_local = self._model_config.is_local
        effective_read_timeout = (
            max(self._stream_read_timeout, self._model_config.timeout)
            if is_local
            else self._stream_read_timeout
        )
        timeout = httpx.Timeout(
            connect=30.0,
            read=effective_read_timeout,
            write=self._model_config.timeout,
            pool=30.0,
        )

        client = self._ensure_client()
        parser = OpenAICompatStreamParser()

        # B#3 raw_dump 累积：流结束（成功或失败）一次性落盘
        chunks_buffer: list[dict[str, Any]] = []
        dump_status = 0
        dump_error: str | None = None

        try:
            async with client.stream(
                "POST", url, json=payload, headers=headers, timeout=timeout
            ) as response:
                dump_status = response.status_code
                if response.status_code >= 400:
                    body_bytes = await response.aread()
                    detail_text = body_bytes.decode("utf-8", errors="replace")[:2000]
                    dump_error = f"HTTP {response.status_code}"
                    raise ProviderError(
                        f"LLM provider stream HTTP {response.status_code}: {detail_text}",
                        details={
                            "status_code": response.status_code,
                            "url": url,
                            "model": self._model_config.name,
                        },
                    )

                async def _wrapped_events() -> AsyncIterator[tuple[str | None, dict[str, Any]]]:
                    async for evt_name, evt_data in iter_sse_events(response):
                        chunks_buffer.append(evt_data)
                        yield evt_name, evt_data

                async for chunk in parser.parse(_wrapped_events()):
                    yield chunk

        except ProviderError:
            # 已包装，直接透传（dump 在 finally 落地）
            raise
        except httpx.TimeoutException as exc:
            dump_error = f"timeout: {exc}"
            raise ProviderError(
                f"LLM provider stream timeout calling {url}: {exc}",
                details={"url": url, "model": self._model_config.name},
            ) from exc
        except httpx.HTTPError as exc:
            dump_error = f"http_error: {exc}"
            raise ProviderError(
                f"LLM provider stream HTTP error calling {url}: {exc}",
                details={"url": url, "model": self._model_config.name},
            ) from exc
        except Exception as exc:
            # SSE / parser 异常都视为 provider 侧失败
            dump_error = f"{type(exc).__name__}: {exc}"
            raise ProviderError(
                f"LLM provider stream failed: {exc!r}",
                details={"url": url, "exception_type": type(exc).__name__},
            ) from exc
        finally:
            # 流结束（成功或异常）一次性 dump，opt-in。
            # 用 contextlib.suppress 兜底：dump 是观测层旁路，磁盘满 / 序列化
            # 失败等副作用绝不能覆盖原始 ProviderError（finally 中抛异常会丢失主异常）。
            try:
                dump_raw_llm_interaction(
                    enabled=self._enable_raw_dump,
                    provider="openai_responses_stream",
                    url=url,
                    request_payload=payload,
                    request_headers=headers,
                    response_status=dump_status,
                    response_headers={},
                    response_body={"chunks": chunks_buffer},
                    error=dump_error,
                )
            except Exception as exc:
                log_network_exception(
                    "executors.llm.openai_responses",
                    "raw_dump_failed",
                    exc,
                    url=url,
                    model=self._model_config.name,
                )

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

        # reasoning_effort：从 model_config 读取，按 model name 映射到厂商格式。
        effort = self._model_config.reasoning_effort
        if effort is not None:
            self._apply_reasoning_effort(payload, effort)

        # 允许调用方通过 metadata 追加 provider 特定字段（v1-mini 先不展开解释
        # 每个字段；透传便于调试）。
        extra = request.metadata.get("provider_extra") if request.metadata else None
        if isinstance(extra, dict):
            for k, v in extra.items():
                # 不允许覆盖核心字段。
                if k not in payload:
                    payload[k] = v

        return payload

    def _apply_reasoning_effort(self, payload: dict[str, Any], effort: str) -> None:
        """按 reasoning_profiles 匹配最终模型名，注入厂商特定 reasoning 参数。

        profiles 为空时静默跳过（reasoning_effort 配置不生效）。
        """
        profiles = self._model_config.reasoning_profiles
        if not profiles:
            return
        model_name = payload.get("model", self._model_config.name)
        config = ReasoningConfig(enabled=True, effort=cast(EffortLevel, effort))
        plan = resolve_reasoning_plan(model_name, config, profiles)
        if plan.send_reasoning:
            payload.update(plan.payload_patch)

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
        # task#3.1：OpenAI usage 字段透传 SDK 原生字段 + provider_kind 标识，
        # 用于 web.usage_token.UsageTokenManager 按 channel 解析。
        normalized_usage: dict[str, Any] = {
            "provider_kind": "openai_compatible",
        }
        # 标准 3 字段
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if key in usage:
                normalized_usage[key] = usage[key]
        # 派生 channel-specific 字段（OpenAI 系命名跟 usage_token._channel_openai 对齐）
        # prompt_tokens 视为 input_tokens（OpenAI 语义：含 cached_input 子集）
        # completion_tokens 视为 output_tokens（含 reasoning_output 子集）
        if "prompt_tokens" in usage:
            normalized_usage["input_tokens"] = usage["prompt_tokens"]
        if "completion_tokens" in usage:
            normalized_usage["output_tokens"] = usage["completion_tokens"]
        # cached_tokens / reasoning_tokens：从 details 子结构提到 usage 主体
        completion_details = usage.get("completion_tokens_details") or {}
        if "reasoning_tokens" in completion_details:
            normalized_usage["reasoning_output_tokens"] = completion_details["reasoning_tokens"]
        prompt_details = usage.get("prompt_tokens_details") or {}
        if "cached_tokens" in prompt_details:
            normalized_usage["cached_input_tokens"] = prompt_details["cached_tokens"]

        # 厂商扩展字段：按原始字段名收集，不归一化（observability 老消费者用）
        provider_metadata: dict[str, Any] = {}
        if "reasoning_tokens" in completion_details:
            provider_metadata["reasoning_tokens"] = completion_details["reasoning_tokens"]
        if "cached_tokens" in prompt_details:
            provider_metadata["cached_tokens"] = prompt_details["cached_tokens"]

        # request/response 标识
        for field_name in ("id", "request_id", "model", "system_fingerprint"):
            if field_name in data:
                provider_metadata[field_name] = data[field_name]

        # reasoning_content（可能很长，截断至 500 字符；完整内容在 raw_dump）
        # 多字段 fallback：reasoning → reasoning_content → reasoning_details[*]
        msg_data = choice.get("message") or {}
        reasoning_raw = self._extract_reasoning(msg_data)
        if reasoning_raw is not None:
            provider_metadata["reasoning_content"] = reasoning_raw[:500]
            provider_metadata["reasoning_content_length"] = len(reasoning_raw)

        return LLMResponse(
            message=message,
            finish_reason=finish_reason,
            usage=normalized_usage,
            provider_metadata=provider_metadata,
            raw=data,
        )

    @staticmethod
    def _extract_reasoning(message_data: dict[str, Any]) -> str | None:
        """从 OpenAI-compat message dict 中抽 reasoning 文本，多字段 fallback。

        按优先级尝试以下字段（命中即返回，不再回退）：

        1. ``message.reasoning`` —— DeepSeek / 部分 Anthropic-compat 兼容层
        2. ``message.reasoning_content`` —— GLM / Qwen / 本地 reasoning 模型
        3. ``message.reasoning_details[*]`` —— OpenAI Responses-style，每项内
           按 ``summary`` / ``thinking`` / ``content`` / ``text`` 顺序取首个非空字段

        对齐 Hermes ``_extract_reasoning``（``other/hermes-agent/run_agent.py``）。
        返回完整文本（不截断；调用方按需截断）。三层都缺时返回 ``None``。
        """
        # 1. reasoning（顶层字符串）
        raw = message_data.get("reasoning")
        if isinstance(raw, str) and raw:
            return raw

        # 2. reasoning_content（顶层字符串）
        raw = message_data.get("reasoning_content")
        if isinstance(raw, str) and raw:
            return raw

        # 3. reasoning_details[*]：每项取首个非空候选字段
        details = message_data.get("reasoning_details")
        if isinstance(details, list):
            parts: list[str] = []
            for item in details:
                if not isinstance(item, dict):
                    continue
                for key in ("summary", "thinking", "content", "text"):
                    val = item.get(key)
                    if isinstance(val, str) and val:
                        parts.append(val)
                        break  # 同一项只取一个字段，避免重复累积
            if parts:
                return "\n".join(parts)

        return None

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
