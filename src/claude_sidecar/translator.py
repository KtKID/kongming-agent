"""SDK Message → SidecarEvent 翻译器。

实现 :class:`claude_sidecar.runtime.MessageTranslator` Protocol。

职责（详见 :doc:`/docs/claude-sidecar-v0.1/02-module-breakdown` §模块 4）：

- **类型分发**：按 SDK message 类型 + content block 类型走不同翻译路径
- **状态身份提取**：``responseId`` / ``toolCallId`` / ``runtimeSessionId``
- **过滤**：``SystemMessage`` 仅消费 ``subtype='init'``，其他丢弃（E10 决议）
- **去重**：deny 后 SDK 自动产生的 ``ToolResultBlock`` 必须丢弃（E8 决议）
- **终态分类**：``ResultMessage`` → finished / failed / interrupted

设计要点：

- :class:`SessionState` 是 translator 自持的运行期对象。``SessionState.last_session_id``
  跨 run 保留（reconnect 后用），``current_run_id`` / ``pending_deny_request_ids`` /
  ``current_streaming_message_id`` 由 :meth:`reset_for_new_run` 在每次 ``start`` 时清空
- ``StreamEvent`` 没有自己的 parent message_id 字段——translator 在每次看到
  ``AssistantMessage(message_id=...)`` 时把 id 记进 ``current_streaming_message_id``，
  下一批 ``StreamEvent`` 用这个 id 当 ``responseId``。这是契约 §7.4 "同 turn 内
  delta/final 共享 responseId" 的实现路径
- 所有 run 级事件经 :meth:`_emit` 走，自动塞 ``threadId`` / ``runId`` —— 契约
  ``SidecarEventBase`` 要求；``protocolVersion`` / ``ts`` 由 :class:`OutputWriter` 自注
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from claude_sidecar.output import OutputWriter
from claude_sidecar.runtime import RunContext


@dataclass
class SessionState:
    """translator 在 sidecar 进程生命周期内的运行期状态。

    字段分两组：

    - **跨 run 保留**：``last_session_id``（reconnect 用）
    - **run 级**：``current_run_id`` / ``pending_deny_request_ids`` /
      ``current_streaming_message_id`` —— ``reset_for_new_run`` 时清空
    """

    last_session_id: str | None = None
    """最近一次 ``SystemMessage(subtype='init').data['session_id']``。
    reconnect 时校验或回填用，跨 run 保留。"""

    current_run_id: str | None = None
    """当前 run 的 ``RunContext.run_id``，``reset_for_new_run`` 写入。"""

    pending_deny_request_ids: set[str] = field(default_factory=set)
    """approval deny 后等待去重的 ``tool_use_id`` 集合。

    #4 approval task 在决议 deny 时调 :meth:`ClaudeMessageTranslator.add_pending_deny`
    把 ``request_id`` 加进来；translator 在 :meth:`_handle_user` 看到匹配
    ``tool_use_id`` 的 ``ToolResultBlock`` 时 ``discard`` 它，不发 ``claude_tool_finished``。
    """

    current_streaming_message_id: str | None = None
    """最近一次看到的 ``AssistantMessage.message_id``。

    SDK ``StreamEvent`` 没有 parent message_id 字段——本字段是把同一 turn 的
    delta/final 联起来的纽带，作为 ``claude_assistant_delta.responseId`` 取值。
    """

    def reset_for_new_run(self, run_id: str) -> None:
        """新 run 开始时调用：清空 run 级字段，保留跨 run 字段。"""
        self.current_run_id = run_id
        self.pending_deny_request_ids.clear()
        self.current_streaming_message_id = None
        # last_session_id 不动


class ClaudeMessageTranslator:
    """SDK Message → SidecarEvent 翻译器。

    实现 :class:`claude_sidecar.runtime.MessageTranslator` Protocol。
    """

    def __init__(
        self,
        output: OutputWriter,
        state: SessionState | None = None,
    ) -> None:
        self._output = output
        self._state = state if state is not None else SessionState()

    # ----- 公共接口（MessageTranslator Protocol） -----

    async def handle(self, sdk_message: Any, context: RunContext) -> None:
        """处理一条 SDK message，分发到对应私有方法。

        ``isinstance`` 顺序匹配——``TaskStartedMessage`` / ``TaskProgressMessage``
        等是 ``SystemMessage`` 的 dataclass 子类，会命中 ``SystemMessage`` 分支
        然后在 :meth:`_handle_system` 内按 subtype 过滤掉。
        """
        # 注意：isinstance 顺序对子类敏感——SystemMessage 子类（TaskStartedMessage 等）
        # 必须放在 SystemMessage 之前，否则 task_* 永远走不到对应处理。
        # 当前设计是 task_* 也走 _handle_system 然后被 subtype 过滤丢弃，所以
        # 顺序无关；但 ResultMessage 等不是 SystemMessage 子类，独立分支即可。
        if isinstance(sdk_message, AssistantMessage):
            await self._handle_assistant(sdk_message, context)
        elif isinstance(sdk_message, UserMessage):
            await self._handle_user(sdk_message, context)
        elif isinstance(sdk_message, SystemMessage):
            await self._handle_system(sdk_message, context)
        elif isinstance(sdk_message, ResultMessage):
            await self._handle_result(sdk_message, context)
        elif isinstance(sdk_message, StreamEvent):
            await self._handle_stream_event(sdk_message, context)
        # 其他类型（RateLimitEvent / ...）静默忽略——契约层面无对应事件

    def add_pending_deny(self, request_id: str) -> None:
        """approval deny 后由 #4 task 调用，把 ``request_id`` 加进去重集合。"""
        self._state.pending_deny_request_ids.add(request_id)

    def reset_for_new_run(self, context: RunContext) -> None:
        """新 run 开始时调用：清空 run 级状态，保留 ``last_session_id``。"""
        self._state.reset_for_new_run(context.run_id)

    # ----- 内部状态访问（仅用于测试） -----

    @property
    def state(self) -> SessionState:
        """暴露内部状态对象——单测断言用，业务方不应依赖。"""
        return self._state

    # ----- 公共写事件辅助 -----

    async def _emit(
        self,
        event_type: str,
        context: RunContext,
        **fields: Any,
    ) -> None:
        """写 run 级事件。自动塞 ``threadId`` / ``runId``（``SidecarEventBase`` 要求）。

        ``protocolVersion`` / ``ts`` 由 :class:`OutputWriter` 自注，本层不重复。
        ``None`` 字段值不剔除——交给调用方用 ``**dict``-展开方式控制（见
        ``_handle_result`` 等）。
        """
        payload: dict[str, Any] = {
            "type": event_type,
            "threadId": context.thread_id,
            "runId": context.run_id,
        }
        payload.update(fields)
        await self._output.emit(payload)

    # ----- 各类 SDK message 翻译方法 -----

    async def _handle_system(
        self,
        msg: SystemMessage,
        context: RunContext,
    ) -> None:
        """SystemMessage：仅消费 ``subtype='init'``，其他丢弃。

        ``init`` 时回填 ``SessionState.last_session_id`` 并发
        ``claude_session_created``。

        E10 决议：``hook_started`` / ``hook_response`` / ``task_started`` /
        ``task_progress`` / ``task_notification`` 等全部丢弃。
        """
        if msg.subtype != "init":
            return
        sid = msg.data.get("session_id") if msg.data else None
        if sid is not None:
            self._state.last_session_id = sid
        # runtimeSessionId 必填（契约 §7.3），即使 SDK 没给也照实输出 None——
        # 这是 SDK 异常，不应静默吞掉
        await self._emit(
            "claude_session_created",
            context,
            runtimeSessionId=sid,
        )

    async def _handle_assistant(
        self,
        msg: AssistantMessage,
        context: RunContext,
    ) -> None:
        """AssistantMessage：按 content block 类型分发到 3 类事件。

        - ``TextBlock`` → ``claude_assistant_final``
        - ``ThinkingBlock`` → ``claude_assistant_thinking_final``（空字符串照实输出）
        - ``ToolUseBlock`` → ``claude_tool_started``

        每次进入此方法都更新 ``current_streaming_message_id``，给后续 ``StreamEvent``
        用作 ``responseId`` 锚点。
        """
        # 锚点：StreamEvent 用此 id 当 responseId
        if msg.message_id:
            self._state.current_streaming_message_id = msg.message_id

        response_id = msg.message_id or self._state.current_streaming_message_id

        # 防御性遍历：smoke 显示通常一条 AssistantMessage 只含一类 block，
        # 但 SDK 类型 list[ContentBlock] 允许多 block 数组，按规则全部翻译
        for block in msg.content or []:
            if isinstance(block, TextBlock):
                await self._emit(
                    "claude_assistant_final",
                    context,
                    responseId=response_id,
                    content=block.text,
                )
            elif isinstance(block, ThinkingBlock):
                # 空 thinking 照实输出：smoke §2.3 实测模型可能产生空 ThinkingBlock，
                # 契约 §7.7 没禁止 content 为空字符串
                await self._emit(
                    "claude_assistant_thinking_final",
                    context,
                    responseId=response_id,
                    content=block.thinking,
                )
            elif isinstance(block, ToolUseBlock):
                await self._emit(
                    "claude_tool_started",
                    context,
                    toolCallId=block.id,
                    toolName=block.name,
                    input=block.input,
                )
            # ToolResultBlock 不会出现在 AssistantMessage（SDK 把它放在 UserMessage）

    async def _handle_user(
        self,
        msg: UserMessage,
        context: RunContext,
    ) -> None:
        """UserMessage：仅处理含 ``ToolResultBlock`` 的 list 形式。

        命中 deny 集合的 ``tool_use_id`` → ``discard`` 并丢弃事件（E8 决议）。
        未命中 → 发 ``claude_tool_finished``（``content`` / ``isError`` 都是可选）。

        ``UserMessage.content`` 也可能是 ``str``（用户原文输入），sidecar 不回吐。
        """
        content = msg.content
        if not isinstance(content, list):
            # str 形式：用户输入文本，sidecar 不回吐
            return

        for block in content:
            if not isinstance(block, ToolResultBlock):
                # UserMessage 含其他 block 类型理论上不会发生，但防御性跳过
                continue

            tool_use_id = block.tool_use_id

            if tool_use_id in self._state.pending_deny_request_ids:
                # E8 决议：deny 后 SDK 自动塞的 ToolResultBlock 必须丢弃
                # discard 一次后清掉，避免后续误抑制（虽然 SDK 不会重发同 id）
                self._state.pending_deny_request_ids.discard(tool_use_id)
                continue

            payload: dict[str, Any] = {"toolCallId": tool_use_id}
            # content / isError 是契约 §7.9 的可选字段，缺省时不写
            if block.content is not None:
                payload["content"] = block.content
            if block.is_error is not None:
                payload["isError"] = block.is_error

            await self._emit("claude_tool_finished", context, **payload)

    async def _handle_result(
        self,
        msg: ResultMessage,
        context: RunContext,
    ) -> None:
        """ResultMessage：3 类终态分类。

        优先级：``is_error=True`` → ``run_failed``；
        ``stop_reason='interrupted'`` → ``run_interrupted``；
        其他 → ``run_finished``（携带可选 ``usage`` / ``durationMs`` / ``durationApiMs``）。
        """
        if msg.is_error:
            await self._emit_failed(msg, context)
        elif msg.stop_reason == "interrupted":
            await self._emit_interrupted(msg, context)
        else:
            await self._emit_finished(msg, context)

    async def _emit_finished(
        self,
        msg: ResultMessage,
        context: RunContext,
    ) -> None:
        """``run_finished`` + 可选 usage / durationMs / durationApiMs（N2a 决议）。"""
        payload: dict[str, Any] = {}

        usage = _normalize_usage(msg.usage, total_cost_usd=msg.total_cost_usd)
        if usage:
            payload["usage"] = usage

        # ResultMessage.duration_ms / duration_api_ms 是必填 int（dataclass 默认 0）；
        # 但 0 也是合法值（极快 run），照实输出
        if msg.duration_ms is not None:
            payload["durationMs"] = msg.duration_ms
        if msg.duration_api_ms is not None:
            payload["durationApiMs"] = msg.duration_api_ms

        await self._emit("claude_run_finished", context, **payload)

    async def _emit_failed(
        self,
        msg: ResultMessage,
        context: RunContext,
    ) -> None:
        """``run_failed``：``message`` 取 errors[0] 或 errors 拼接 或 subtype。

        Q7 留待 #6 error-handling task 细化分类规则；当前是粗粒度兜底。
        """
        if msg.errors and len(msg.errors) >= 1:
            message = msg.errors[0] if len(msg.errors) == 1 else " / ".join(msg.errors)
        elif msg.subtype:
            message = msg.subtype
        else:
            message = "unknown error"
        await self._emit("claude_run_failed", context, message=message)

    async def _emit_interrupted(
        self,
        msg: ResultMessage,
        context: RunContext,
    ) -> None:
        """``run_interrupted``：``reason`` 取 subtype（可选字段）。"""
        payload: dict[str, Any] = {}
        if msg.subtype:
            payload["reason"] = msg.subtype
        await self._emit("claude_run_interrupted", context, **payload)

    async def _handle_stream_event(
        self,
        msg: StreamEvent,
        context: RunContext,
    ) -> None:
        """StreamEvent：第一阶段只翻译 ``content_block_delta``。

        - ``delta.type='text_delta'`` → ``claude_assistant_delta``
        - ``delta.type='thinking_delta'`` → ``claude_assistant_thinking_delta``
        - ``signature_delta`` 等其他 delta 类型丢弃
        - ``message_start`` / ``content_block_start`` / ``content_block_stop`` /
          ``message_delta`` / ``message_stop`` 等 SSE 控制帧全部丢弃

        ``responseId`` 取自 ``current_streaming_message_id``——理论上 SDK 一定先发
        partial AssistantMessage 再发 StreamEvent，但若顺序未保证，降级为
        ``'unknown'`` 不丢失事件。
        """
        event = msg.event or {}
        if event.get("type") != "content_block_delta":
            return

        delta = event.get("delta") or {}
        delta_type = delta.get("type")

        # 降级：理论上 current_streaming_message_id 一定有值，但若 SDK 顺序异常
        # 不应丢失 delta 事件，用 'unknown' 兜底
        response_id = self._state.current_streaming_message_id or "unknown"

        if delta_type == "text_delta":
            await self._emit(
                "claude_assistant_delta",
                context,
                responseId=response_id,
                delta=delta.get("text", ""),
            )
        elif delta_type == "thinking_delta":
            await self._emit(
                "claude_assistant_thinking_delta",
                context,
                responseId=response_id,
                delta=delta.get("thinking", ""),
            )
        # signature_delta / 未知 delta 类型丢弃


# ----- 工具函数 -----

# SDK ResultMessage.usage 字段名 → 契约 usage 字段名映射
# smoke §2.1 / SDK types.py 没枚举 usage 内部字段，但 Anthropic API 标准是 snake_case
# （input_tokens / output_tokens / cache_read_input_tokens / cache_creation_input_tokens）
# 契约 §7.13 用 camelCase。本表覆盖常见两种写法（带不带 "_input_" 中段）
_USAGE_KEY_MAP: dict[str, str] = {
    "input_tokens": "inputTokens",
    "output_tokens": "outputTokens",
    "cache_read_input_tokens": "cacheReadTokens",
    "cache_read_tokens": "cacheReadTokens",
    "cache_creation_input_tokens": "cacheCreationTokens",
    "cache_creation_tokens": "cacheCreationTokens",
    # 契约 §7.13 把 totalCostUsd 放在 usage 内部，但 SDK 把它放在
    # ResultMessage.total_cost_usd（独立字段）—— _emit_finished 通过 total_cost_usd
    # 参数注入，下面的 map 只处理 usage dict 内部已有的字段
}


def _normalize_usage(
    usage: dict[str, Any] | None,
    *,
    total_cost_usd: float | None = None,
) -> dict[str, Any]:
    """把 SDK ``ResultMessage.usage`` 字典字段名 snake→camel 转换。

    - 已是 camelCase 的字段（如 ``inputTokens``）直接透传
    - 未知字段透传不丢（保持向前兼容，SDK 升级新增字段不破）
    - 同 key 已存在时不覆盖（已是 camel 优先）

    ``total_cost_usd`` 由调用方从 ``ResultMessage.total_cost_usd`` 注入，
    合成到 ``usage.totalCostUsd``（契约 §7.13）。
    """
    out: dict[str, Any] = {}
    if usage:
        for key, value in usage.items():
            mapped = _USAGE_KEY_MAP.get(key, key)
            # 已存在则不覆盖：camelCase 原始字段优先于 snake_case 转出来的
            if mapped not in out:
                out[mapped] = value
    if total_cost_usd is not None and "totalCostUsd" not in out:
        out["totalCostUsd"] = total_cost_usd
    return out
