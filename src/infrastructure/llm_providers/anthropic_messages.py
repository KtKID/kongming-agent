"""Anthropic Messages API provider。

走 ``{base_url}/v1/messages`` 端点（原生 Anthropic Messages API）。

与 OpenAI Chat Completions 的主要差异：

- 系统消息提取到请求顶层的 ``system`` 字段，不混入 ``messages`` 数组。
- assistant tool_calls 转成 ``{"type": "tool_use", ...}`` content block。
- tool result 作为 user 角色的 ``{"type": "tool_result", ...}`` content block。
- Tools schema 不带 ``function`` 包装层，使用 ``input_schema``。
- 响应用 ``stop_reason`` 而非 ``finish_reason``；usage 字段名也不同。
- 鉴权头：API key 默认写入 ``x-api-key``；兼容端点可通过配置
  ``api_key_header: authorization-bearer`` 改为 ``Authorization: Bearer``。
  必须携带 ``anthropic-version: 2023-06-01``。

**base_url 约定**：``base_url`` 由调用方设置（默认 ``https://api.anthropic.com``），
provider 内部拼 ``/v1/messages``——与 OpenAI provider 约定一致，版本段在 base_url 里。
但 Anthropic 官方 base_url 本身不含 ``/v1``，所以 ``_ENDPOINT_PATH = "/v1/messages"``。

**api_key 要求**：Anthropic 是远端服务，``is_local`` 恒 False，pydantic 校验已确保
``api_key`` 非空，这里无需单独判断。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal

import httpx

from core.contracts import (
    AssetBytesReader,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    ProviderUsageCompleteness,
    ProviderUsageFamily,
    collect_media_parts_from_messages,
)
from core.errors import ProviderError
from core.message import Message, ToolCall
from infrastructure.config.api_key_headers import build_api_key_headers
from infrastructure.config.model_provider_catalog import (
    ResolvedModelConfig,
    ResolvedModelCredential,
)
from infrastructure.llm_providers.anthropic_stream_parser import AnthropicStreamParser
from infrastructure.llm_providers.base import BaseLLMProvider
from infrastructure.llm_providers.media_adapter import (
    AnthropicMediaAdapter,
    MediaAdapter,
)
from infrastructure.llm_providers.raw_dump import dump_raw_llm_interaction
from infrastructure.llm_providers.reasoning import (
    ReasoningConfig,
    ResolvedReasoningPlan,
    resolve_reasoning_plan,
)
from infrastructure.llm_providers.sse_reader import iter_sse_events
from infrastructure.llm_providers.usage import ProviderUsageManager
from network.network_log import log_network_exception

_LOGGER = logging.getLogger(__name__)


class AnthropicMessagesProvider(BaseLLMProvider):
    """原生 Anthropic Messages API provider。

    走 ``{base_url}/v1/messages`` 端点。
    """

    _ENDPOINT_PATH = "/v1/messages"
    _ANTHROPIC_VERSION = "2023-06-01"

    def __init__(
        self,
        *,
        model_config: ResolvedModelConfig,
        credential: ResolvedModelCredential,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        enable_raw_dump: bool = False,
        stream_read_timeout: float = 120.0,
        media_adapter: MediaAdapter | None = None,
        asset_reader: AssetBytesReader | None = None,
    ) -> None:
        super().__init__(
            model_config=model_config,
            credential=credential,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
        )
        # 与 OpenAIResponsesProvider 对齐:raw_dump 由 cfg.trace.raw_llm 驱动;
        # stream_read_timeout 由 cfg.stream.read_timeout 驱动。
        # Anthropic 是远端服务(is_local 恒 False),read_timeout 不像 OpenAI 那样
        # 按 is_local 自动上调。
        self._enable_raw_dump = enable_raw_dump
        self._stream_read_timeout = stream_read_timeout
        self._usage_manager = ProviderUsageManager()

        # 多模态 user message 装配：provider 只消费宿主无关的
        # ``AssetBytesReader`` 协议。Web 上传存储由 Web 装配层显式注入；
        # CLI / cron 等纯文本路径保持 ``None``。
        self._media_adapter: MediaAdapter = media_adapter or AnthropicMediaAdapter()
        self._asset_reader = asset_reader

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
    # 流式请求（满足 SupportsLLMStream Protocol）
    # ------------------------------------------------------------------

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        """Anthropic Messages API 流式响应。

        实现 :class:`core.contracts.SupportsLLMStream` Protocol。链路：
        ``httpx.AsyncClient.stream("POST", ...)`` → :func:`iter_sse_events`
        → :class:`AnthropicStreamParser` → yield :class:`LLMStreamChunk`。

        与 :meth:`OpenAIResponsesProvider.stream` 结构一致，差异：

        - payload 加 ``"stream": True``，**不**加 ``stream_options``（Anthropic
          的 usage 天然由 ``message_start`` + ``message_delta`` 携带，无需主动开）
        - timeout 不按 ``is_local`` 上调（Anthropic 远端恒定）
        - raw_dump chunk 元素带回 ``event_name`` 便于排查
        - 异常映射、HTTP 4xx/5xx 处理、finally 段 dump 全部对齐
        """
        url = self._model_config.base_url.rstrip("/") + self._ENDPOINT_PATH
        payload = {**self._build_payload(request), "stream": True}
        headers = self._build_headers()

        timeout = httpx.Timeout(
            connect=30.0,
            read=self._stream_read_timeout,
            write=self._model_config.timeout,
            pool=30.0,
        )

        client = self._ensure_client()
        parser = AnthropicStreamParser(usage_manager=self._usage_manager)

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
                        chunks_buffer.append({"event": evt_name, "data": evt_data})
                        yield evt_name, evt_data

                async for chunk in parser.parse(_wrapped_events()):
                    yield chunk

        except ProviderError:
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
            dump_error = f"{type(exc).__name__}: {exc}"
            raise ProviderError(
                f"LLM provider stream failed: {exc!r}",
                details={"url": url, "exception_type": type(exc).__name__},
            ) from exc
        finally:
            try:
                dump_raw_llm_interaction(
                    enabled=self._enable_raw_dump,
                    provider="anthropic_messages_stream",
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
                    "infrastructure.llm_providers.anthropic_messages",
                    "raw_dump_failed",
                    exc,
                    url=url,
                    model=self._model_config.name,
                )

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
        # 从 ``request.metadata["thread_id"]`` 读 thread_id，由 runner 在装配
        # LLMRequest 时注入(``session.session_id`` 真值)。纯文本路径不读取资产。
        thread_id = ""
        if request.metadata:
            raw_tid = request.metadata.get("thread_id")
            if isinstance(raw_tid, str):
                thread_id = raw_tid

        # P1 #1 (R2 boundary fix)：load_bytes / to_blocks 失败时静默退化纯文本,
        # 收集被丢的 asset_ids 写到 ``request.metadata["dropped_attachments"]``,
        # 作为后端 audit trail（前端 UI 提示留 Phase 2）。
        dropped: list[str] = []

        def _convert_user(msg: Message) -> dict[str, Any]:
            return self._convert_user_message(msg, thread_id=thread_id, dropped=dropped)

        system_text, messages = self._split_system_and_convert(
            list(request.messages), user_converter=_convert_user
        )

        # 把 dropped asset_ids 透传给上层(audit / 观测消费方)。
        # request.metadata 是可变 dict(LLMRequest frozen 但 dict 内容可改),
        # 这里只写一次(本函数只跑一遍 _build_payload)。
        if dropped and request.metadata is not None:
            request.metadata["dropped_attachments"] = list(dropped)

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

        # reasoning_effort：请求级值覆盖 provider 配置默认值，按最终模型名匹配 profile。
        # 注意：与 OpenAI provider 不同，Anthropic provider 没有硬编码回退路径。
        # 原因：Anthropic 历史上没有内置的 reasoning 参数映射；V1 只通过配置驱动
        # 的 reasoning_profiles 支持 MiniMax 等兼容层模型。若 profiles 为空，
        # 则 reasoning_effort 配置静默不生效，这是有意的配置保护。
        effort = (
            request.reasoning_effort
            if request.reasoning_effort is not None
            else self._default_reasoning_effort()
        )
        model_name = _payload_model_name(payload, self._model_config.name)
        capability = self._reasoning_capability(model_name)
        if effort is None:
            self._log_reasoning_decision(
                model_name=model_name,
                requested_effort=None,
                plan=None,
                capability_configured=capability is not None,
            )
        elif capability is not None:
            config = ReasoningConfig(enabled=True, effort=effort)
            plan = resolve_reasoning_plan(model_name, config, capability)
            self._log_reasoning_decision(
                model_name=model_name,
                requested_effort=effort,
                plan=plan,
                capability_configured=True,
            )
            if plan.send_reasoning:
                payload.update(plan.payload_patch)
        else:
            self._log_reasoning_decision(
                model_name=model_name,
                requested_effort=effort,
                plan=None,
                capability_configured=False,
            )
            config = ReasoningConfig(enabled=True, effort=effort)
            resolve_reasoning_plan(model_name, config, capability)

        extra = request.metadata.get("provider_extra") if request.metadata else None
        if isinstance(extra, dict):
            for k, v in extra.items():
                if k not in payload:
                    payload[k] = v

        return payload

    def _log_reasoning_decision(
        self,
        *,
        model_name: str,
        requested_effort: str | None,
        plan: ResolvedReasoningPlan | None,
        capability_configured: bool,
    ) -> None:
        """记录每次请求的 reasoning 开关、档位和实际 payload 注入决策。"""
        switch = _reasoning_switch(requested_effort)
        _LOGGER.info(
            "llm.reasoning provider=%s model=%s switch=%s requested_effort=%s "
            "normalized_effort=%s adapter=%s send_reasoning=%s payload_keys=%s "
            "catalog_source=%s catalog_provider=%s preset_id=%s capability_configured=%s",
            "anthropic",
            model_name,
            switch,
            requested_effort,
            plan.normalized_effort if plan is not None else None,
            plan.adapter_name if plan is not None else "none",
            plan.send_reasoning if plan is not None else False,
            sorted(plan.payload_patch.keys()) if plan is not None else [],
            *self._reasoning_catalog_context(),
            capability_configured,
        )

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "anthropic-version": self._ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        headers.update(
            build_api_key_headers(
                api_key=self._credential.value,
                api_key_header=self._credential.header
                or self._model_config.effective_api_key_header,
            )
        )
        return headers

    # ------------------------------------------------------------------
    # 消息格式转换
    # ------------------------------------------------------------------

    @staticmethod
    def _split_system_and_convert(
        messages: list[Message],
        *,
        user_converter: Callable[[Message], dict[str, Any]] | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """提取 system 消息，转换其余消息为 Anthropic 格式。

        Args:
            messages: 待转换的消息列表。
            user_converter: 可选的 user message 转换器，由 provider 实例传入
                以支持 multi-block 附件（claude-image-paste-e2e §5）。未传时
                走原 ``{"role": "user", "content": msg.content or ""}`` 路径
                （向后兼容既有 248+ 纯文本测试）。

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
                if user_converter is not None:
                    out.append(user_converter(msg))
                else:
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

    def _convert_user_message(
        self,
        msg: Message,
        *,
        thread_id: str,
        dropped: list[str] | None = None,
    ) -> dict[str, Any]:
        """把单条 user :class:`Message` 转成 Anthropic 格式。

        - 无 ``metadata["attachments"]`` → 保持原 ``content: str`` 形态
          (向后兼容既有纯文本测试)。
        - 有 ``metadata["attachments"]`` → 通过注入的 ``AssetBytesReader`` 还原
          成 ``MediaPart``，再由 :class:`AnthropicMediaAdapter` 转成 image blocks。
          文本为空时省略 text block(Anthropic API 接受 image-only content)。

        :func:`collect_media_parts_from_messages` 内部对 unrebuildable / 未知 kind
        都 warning log + 跳过,不抛错;若所有 attachments 都跳过,结果等价于纯文本路径。

        Args:
            msg: 待转换的 user 消息。
            thread_id: 当前 thread id,用于宿主层资产读取器解析资产坐标。
            dropped: 可选 list,被退化丢弃的 attachment asset_id 会 append 进去,
                由调用方(``_build_payload``)写到 ``request.metadata["dropped_attachments"]``
                作为后端 audit trail(P1 #1 R2 boundary fix)。None 时不收集。
        """
        text = msg.content or ""
        metadata = msg.metadata or {}
        refs = metadata.get("attachments") if isinstance(metadata, dict) else None
        if not isinstance(refs, list) or not refs:
            return {"role": "user", "content": text}

        # 收集 ref 里声明的 asset_ids,用于退化分支的 audit 上报
        declared_asset_ids: list[str] = []
        for ref in refs:
            if isinstance(ref, dict):
                aid = ref.get("asset_id")
                if isinstance(aid, str) and aid:
                    declared_asset_ids.append(aid)

        if self._asset_reader is None:
            if dropped is not None:
                _LOGGER.warning(
                    "drop attachments because no asset reader is configured for thread %s: "
                    "asset_ids=%s",
                    thread_id,
                    declared_asset_ids,
                )
                dropped.extend(declared_asset_ids)
            return {"role": "user", "content": text}

        parts = collect_media_parts_from_messages(
            [msg], reader=self._asset_reader, thread_id=thread_id
        )
        if not parts:
            # 所有 attachments 均无法还原 → 退化到纯文本(已 warning log)
            # P1 #1 R2 boundary fix:把这些 asset_id 也算作 dropped。
            if dropped is not None:
                _LOGGER.warning(
                    "drop attachments due to collect_media_parts returning empty for "
                    "thread %s: asset_ids=%s",
                    thread_id,
                    declared_asset_ids,
                )
                dropped.extend(declared_asset_ids)
            return {"role": "user", "content": text}

        # P0-2 (R2 boundary fix)：``MediaAdapter.to_blocks`` 内部按 part 调
        # ``load_bytes()`` 读盘；任意一个资产文件 missing / 读失败 / mime 不
        # 合法都会让本轮 turn 整体崩溃，违反 ``media_adapter.py`` docstring
        # "图片有问题 → 退化纯文本" 承诺。这里把 IO/格式异常统一兜底成
        # warning log + 退化纯文本路径（与 ``not parts`` 同 return 分支）。
        try:
            image_blocks = self._media_adapter.to_blocks(parts)
        except (FileNotFoundError, OSError, ValueError) as exc:
            # P1 #1 R2 boundary fix:to_blocks 失败 → 收集 parts 里所有 asset_id
            # 到 dropped(asset 已通过 collect 还原,part.asset_id 一定是合法字符串)。
            failed_asset_ids = [p.asset_id for p in parts]
            _LOGGER.warning(
                "drop attachments due to media adapter to_blocks failure for thread %s: "
                "asset_ids=%s err=%s",
                thread_id,
                failed_asset_ids,
                exc,
            )
            if dropped is not None:
                dropped.extend(failed_asset_ids)
            return {"role": "user", "content": text}

        content_blocks: list[dict[str, Any]] = list(image_blocks)
        if text:
            content_blocks.append({"type": "text", "text": text})
        return {"role": "user", "content": content_blocks}

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

        usage_value = data.get("usage")
        usage_raw = usage_value if isinstance(usage_value, dict) else {}
        response_id = data.get("id")
        normalized_usage = self._usage_manager.normalize(
            family=ProviderUsageFamily.ANTHROPIC_MESSAGES,
            raw_usage=usage_raw,
            completeness=(
                ProviderUsageCompleteness.COMPLETE
                if isinstance(usage_value, dict)
                else ProviderUsageCompleteness.INCOMPLETE
            ),
            provider_response_id=response_id if isinstance(response_id, str) else None,
        )

        # 厂商扩展字段：保留原始 Anthropic 字段名。
        provider_metadata: dict[str, Any] = {}

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


def _reasoning_switch(effort: str | None) -> str:
    """把请求 effort 投影成日志里的开关状态。"""
    if effort is None:
        return "unspecified"
    if effort.lower() == "none":
        return "disabled"
    return "enabled"


def _payload_model_name(payload: dict[str, Any], fallback: str) -> str:
    """从 provider payload 中读取最终模型名。"""
    model_name = payload.get("model")
    if isinstance(model_name, str):
        return model_name
    return fallback


__all__ = ["AnthropicMessagesProvider"]
