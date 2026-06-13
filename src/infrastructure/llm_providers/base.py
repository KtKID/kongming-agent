"""LLM provider 实现侧基类与共用助手。

这里**不是** :class:`core.contracts.LLMProvider` Protocol 的重定义，而是
provider 实现方（``openai_responses.py`` 以及未来的 ``anthropic_messages.py``
等）可以继承的便利基类。承担：

- 重试（带指数退避）
- 顶层超时兜底（每个子类 ``_do_complete`` 内部也可以用 ``model_config.timeout``
  做更细粒度的请求级超时；基类再包一层总时长保险）
- 错误归一化为 :class:`core.errors.ProviderError`
- 流式响应处理留出扩展点（v1-mini 第一版只走非流式；接口形状保留，不在调用
  方引入流式分支）
- 把 :class:`core.message.Message` 列表转成 OpenAI chat 消息格式（大多数
  OpenAI-compatible 厂商共用）；把 OpenAI choice 对象转回 :class:`Message`
  （包含 tool_calls 解析）

依赖方向：只依赖 ``core`` 协议与 ``infrastructure.config.models.ModelConfig``，
不依赖任何 sibling 模块。
"""

from __future__ import annotations

import asyncio
import json
import random
import uuid
from typing import Any

import httpx

from core.contracts import LLMRequest, LLMResponse
from core.errors import ProviderError
from core.message import Message, ToolCall
from infrastructure.config.models import ModelConfig


class BaseLLMProvider:
    """provider 实现侧基类。

    子类只需实现 :meth:`_do_complete`；重试 / 超时 / 错误归一化由基类提供。

    Attributes:
        model_config: 从统一配置入口拿到的模型配置（超时 / key / base_url 等
            运行时可调参数的唯一来源）。
        max_retries: 出现可重试错误时的最大重试次数，不含首次。
        retry_backoff: 指数退避基准秒数；第 n 次重试等待
            ``retry_backoff * 2**n * jitter`` 秒。
    """

    def __init__(
        self,
        *,
        model_config: ModelConfig,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if retry_backoff <= 0:
            raise ValueError("retry_backoff must be > 0")
        self._model_config = model_config
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        # 懒加载的 httpx.AsyncClient：首次 :meth:`_ensure_client` 触发构造，
        # 之后跨多次 ``_do_complete`` 复用以命中 TLS keepalive。
        # 不在 client 级绑 timeout——每次 `.post()` 按 request.timeout_seconds
        # 覆盖，保证不同 request 的 timeout 自适应。
        self._client: httpx.AsyncClient | None = None

    @property
    def model_config(self) -> ModelConfig:
        return self._model_config

    # ------------------------------------------------------------------
    # httpx client 生命周期
    # ------------------------------------------------------------------

    def _ensure_client(self) -> httpx.AsyncClient:
        """懒加载共享 ``httpx.AsyncClient``。

        参数选择参考 Hermes（``other/hermes-agent/run_agent.py``）：
        ``max_keepalive_connections=5`` / ``keepalive_expiry=30.0`` 对 kongming
        单用户 CLI 足够。不在此设置 ``timeout``——per-request 传给 ``.post()``。
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                limits=httpx.Limits(
                    max_keepalive_connections=5,
                    keepalive_expiry=30.0,
                ),
            )
        return self._client

    async def aclose(self) -> None:
        """关闭持有的 ``httpx.AsyncClient`` 并释放连接池。

        幂等：未曾创建或已关闭时，连续调用不抛错。调用方（``NativeRuntime.aclose``
        / CLI finally 分支）负责在生命周期结束时调用。
        """
        client = self._client
        if client is None:
            return
        self._client = None
        await client.aclose()

    # ------------------------------------------------------------------
    # 公共入口（Protocol 语义：满足 core.contracts.LLMProvider）
    # ------------------------------------------------------------------

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """带重试与错误归一化的 provider 调用。

        重试判定由 :meth:`_is_retryable` 决定。默认实现把 ``asyncio.TimeoutError``
        和非 :class:`ProviderError` 的普通 :class:`Exception` 视为可重试，
        :class:`ProviderError` 不重试（由子类自己决定是否已经包装为可重试）。

        顶层超时：若请求里没指定 ``timeout_seconds``，使用
        ``model_config.timeout`` 作为总时长上限。
        """
        effective_timeout = (
            request.timeout_seconds
            if request.timeout_seconds is not None
            else self._model_config.timeout
        )
        attempt = 0
        last_exc: Exception | None = None
        while True:
            try:
                return await asyncio.wait_for(self._do_complete(request), timeout=effective_timeout)
            except ProviderError as exc:
                # provider 自己已经包装过了——默认视为不可重试，直接透传。
                raise exc
            except TimeoutError as exc:
                last_exc = exc
                if attempt >= self._max_retries:
                    raise ProviderError(
                        f"LLM provider call timed out after {effective_timeout}s "
                        f"(retries exhausted: {attempt})",
                        details={
                            "attempts": attempt + 1,
                            "timeout_seconds": effective_timeout,
                        },
                    ) from exc
            except Exception as exc:
                last_exc = exc
                if not self._is_retryable(exc) or attempt >= self._max_retries:
                    raise ProviderError(
                        f"LLM provider call failed: {exc!r}",
                        details={
                            "attempts": attempt + 1,
                            "exception_type": type(exc).__name__,
                        },
                    ) from exc

            # 还有机会重试——走指数退避。
            attempt += 1
            delay = self._retry_backoff * (2 ** (attempt - 1))
            # 加一点抖动，避免多副本一起冲突时的同相重试。
            delay *= 0.5 + random.random()
            await asyncio.sleep(delay)
            # 下一轮继续 while True。
            _ = last_exc

    # ------------------------------------------------------------------
    # 子类覆写点
    # ------------------------------------------------------------------

    async def _do_complete(self, request: LLMRequest) -> LLMResponse:
        """真正发请求。子类必须实现。"""
        raise NotImplementedError

    def _is_retryable(self, exc: Exception) -> bool:
        """判定某个底层异常是否可重试。

        默认策略：只要不是 :class:`ProviderError`（即没被子类显式标记成"终态失败"），
        就视为瞬时问题，可重试。子类可以覆盖以加细规则（如根据 HTTP status code
        判断）。
        """
        return not isinstance(exc, ProviderError)

    # ------------------------------------------------------------------
    # OpenAI chat 消息格式转换助手
    # ------------------------------------------------------------------

    @staticmethod
    def messages_to_openai_format(
        messages: list[Message] | tuple[Message, ...],
    ) -> list[dict[str, Any]]:
        """把内部 :class:`Message` 列表转成 OpenAI Chat Completions 消息格式。

        - ``user`` / ``system`` → ``{"role": "...", "content": str}``
        - ``assistant`` → 可能带 ``tool_calls``；``content`` 可能为 ``None``
        - ``tool`` → ``{"role": "tool", "tool_call_id": "...", "content": "..."}``

        这份转换足以覆盖 LM Studio / Ollama / vLLM / OpenAI 官方端点的 chat
        completions 口径。
        """
        out: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.role
            if role == "user":
                out.append({"role": "user", "content": msg.content or ""})
            elif role == "system":
                out.append({"role": "system", "content": msg.content or ""})
            elif role == "assistant":
                item: dict[str, Any] = {"role": "assistant"}
                # OpenAI 允许 assistant content 为 null 当且仅当带 tool_calls。
                item["content"] = msg.content if msg.content is not None else None
                if msg.tool_calls:
                    item["tool_calls"] = [
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {
                                "name": call.tool_name,
                                # OpenAI 规范里 arguments 是一串 JSON 字符串。
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in msg.tool_calls
                    ]
                out.append(item)
            elif role == "tool":
                if msg.tool_call_id is None:
                    # 结构性保护：tool 消息必须带 tool_call_id，否则 OpenAI 会拒。
                    # 这里不抛，而是生成占位，防止 runner 因历史脏数据整个挂掉。
                    call_id = f"missing_{uuid.uuid4().hex[:8]}"
                else:
                    call_id = msg.tool_call_id
                tool_entry: dict[str, Any] = {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": msg.content or "",
                }
                if msg.name:
                    tool_entry["name"] = msg.name
                out.append(tool_entry)
            else:
                # 未识别 role：跳过而不是抛，避免阻塞主链路；上游 runner / host
                # 不应该产生非法 role 的消息。
                continue
        return out

    @staticmethod
    def tools_to_openai_format(tools: tuple[Any, ...] | list[Any]) -> list[dict[str, Any]]:
        """把 :class:`core.contracts.Tool` 列表转成 OpenAI tools schema。"""
        result: list[dict[str, Any]] = []
        for tool in tools:
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
            )
        return result

    @staticmethod
    def openai_choice_to_message(choice: dict[str, Any]) -> Message:
        """把 OpenAI chat completion 返回的一个 choice 转成内部 :class:`Message`。

        Args:
            choice: 形如 ``{"message": {"role": "assistant", "content": "...",
                "tool_calls": [...]}, "finish_reason": "..."}`` 的 choice dict。

        Returns:
            :class:`Message` 实例。只返回 message，不带 finish_reason——
            那是 :class:`LLMResponse` 的字段。
        """
        message_raw = choice.get("message") or {}
        content = message_raw.get("content")
        if content is not None and not isinstance(content, str):
            # 有些 provider 返回 content 是 list-of-parts；这里做个简单 flatten
            # （保留 text 段），避免上游拿到不规范类型。
            try:
                content = "".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") in ("text", "output_text")
                )
            except Exception:
                content = str(content)

        tool_calls_raw = message_raw.get("tool_calls") or []
        parsed_calls: list[ToolCall] = []
        for tc in tool_calls_raw:
            if not isinstance(tc, dict):
                continue
            call_id = tc.get("id") or f"call_{uuid.uuid4().hex[:10]}"
            func = tc.get("function") or {}
            tool_name = func.get("name") or ""
            args_raw = func.get("arguments")
            arguments: dict[str, Any]
            if isinstance(args_raw, dict):
                arguments = args_raw
            elif isinstance(args_raw, str) and args_raw:
                try:
                    loaded = json.loads(args_raw)
                    arguments = loaded if isinstance(loaded, dict) else {"__raw__": loaded}
                except json.JSONDecodeError:
                    # 模型偶尔会产出不合法 JSON；保留原始串，让工具方自己处理。
                    arguments = {"__raw__": args_raw}
            else:
                arguments = {}
            parsed_calls.append(
                ToolCall(
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                )
            )

        return Message.assistant(
            content=content if isinstance(content, str) or content is None else str(content),
            tool_calls=parsed_calls or None,
        )


__all__ = ["BaseLLMProvider"]
