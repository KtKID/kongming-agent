"""SDK Message → :class:`NormalizedMessage` 翻译器（v0.1）。

参考：

- ccui ``server/modules/providers/list/claude/claude-sessions.provider.ts``
  ``normalizeMessage`` 函数
- 旧 sidecar ``src/claude_sidecar/translator.py`` 的翻译规则（输出端从 stdout
  JSONL 改成返回 ``list[NormalizedMessage]``）

翻译规则：

============================  =============================================
SDK message                   NormalizedMessage kind
----------------------------  ---------------------------------------------
``SystemMessage(init)``       ``session_created`` + ``newSessionId``
``SystemMessage(其他)``       丢弃（hook / task 噪声，[E10 决议]）
``AssistantMessage[Text]``    ``text``（role=assistant）
``AssistantMessage[Thinking]``  ``thinking``
``AssistantMessage[ToolUse]`` ``tool_use``
``UserMessage[ToolResult]``   ``tool_result``（命中 deny 集合 → 丢弃 [E8]）
``StreamEvent.message_start``       ``stream_status``（phase=responding, model）
``StreamEvent.content_block_start`` ``stream_status``（phase 按 block type 映射, blockIndex, toolName?）
``StreamEvent.text_delta``    ``stream_delta``（deltaType=text）
``StreamEvent.thinking_delta``  ``stream_delta``（deltaType=thinking）
``StreamEvent.input_json_delta``  ``stream_delta``（deltaType=input_json）
``StreamEvent.content_block_stop``  ``stream_end``
``ResultMessage``             ``complete``（exitCode / durationMs / tokenBudget）
其他类型（``RateLimitEvent`` / ``message_delta`` / ``message_stop``）  丢弃
============================  =============================================

``content_block.type`` → ``phase`` 映射：

- ``"thinking"`` / ``"redacted_thinking"`` → ``"thinking"``
- ``"text"`` → ``"responding"``
- ``"tool_use"`` / ``"server_tool_use"`` / ``"mcp_tool_use"`` → ``"tool_calling"``
- 未知 type → 不产出 ``stream_status``（保守降级）

INTERNAL_CONTENT 过滤：``TextBlock.text`` 命中预定义前缀（如 ``<system-reminder>``）
则丢弃整条 text 事件——这是 Claude Code CLI 的内部 system 注入，不应泄漏给前端。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

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

from web.claude_code._content_filter import (
    INTERNAL_CONTENT_PREFIXES,
    is_internal_content,
)
from web.llm_protocol import NormalizedMessage

# ---------------------------------------------------------------------------
# 内部 system 文本前缀已迁移到 ``web.claude_code._content_filter``
# 此处 re-export 仅为向后兼容外部 import；新代码请直接从 _content_filter import
# ---------------------------------------------------------------------------

_BLOCK_TYPE_TO_PHASE: dict[str, Literal["responding", "thinking", "tool_calling"]] = {
    "text": "responding",
    "thinking": "thinking",
    "redacted_thinking": "thinking",
    "tool_use": "tool_calling",
    "server_tool_use": "tool_calling",
    "mcp_tool_use": "tool_calling",
}
"""``content_block.type`` → ``stream_status.phase`` 映射。

未列入的 block type（未来 SDK 新增）会被保守降级为不产出 ``stream_status``，
避免给前端发送不可识别的 phase 值。
"""


def _now_iso() -> str:
    """生成 ISO8601 UTC 时间戳。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id() -> str:
    """生成消息唯一 id（UUID v4）。"""
    return str(uuid.uuid4())


def _base(session_id: str | None) -> NormalizedMessage:
    """构造 base 字段（id / sessionId / timestamp / provider）。"""
    return NormalizedMessage(
        id=_new_id(),
        sessionId=session_id,
        timestamp=_now_iso(),
        provider="claude",
    )


# ---------------------------------------------------------------------------
# 主翻译类
# ---------------------------------------------------------------------------


class ClaudeNormalizer:
    """SDK message → :class:`NormalizedMessage` 翻译器。

    内部状态：

    - ``_pending_deny``：approval deny 后等待去重的 ``tool_use_id`` 集合
    - ``_last_assistant_message_id``：最近 ``AssistantMessage.message_id``，
      给 ``StreamEvent`` 用作锚点（虽然当前协议不直接使用，但保留兼容字段）

    生命周期：每个 WebSocket 连接一个独立实例（per-connection），避免跨
    连接的 deny 集合污染。
    """

    def __init__(self) -> None:
        self._pending_deny: set[str] = set()
        self._last_assistant_message_id: str | None = None

    # ----- 公共状态接口 -----

    def add_pending_deny(self, tool_use_id: str) -> None:
        """approval deny 时由 :class:`ApprovalBridge` 调用，标记需要去重的
        ``tool_use_id``——下一条同 id 的 ``ToolResultBlock`` 会被丢弃（E8）。
        """
        self._pending_deny.add(tool_use_id)

    def reset(self) -> None:
        """跨 session 重置内部状态。"""
        self._pending_deny.clear()
        self._last_assistant_message_id = None

    # ----- 主翻译方法 -----

    def normalize(
        self,
        sdk_message: Any,
        session_id: str | None,
    ) -> list[NormalizedMessage]:
        """翻译一条 SDK message。返回 0..N 条 :class:`NormalizedMessage`。"""
        if isinstance(sdk_message, SystemMessage):
            return self._handle_system(sdk_message, session_id)
        if isinstance(sdk_message, AssistantMessage):
            return self._handle_assistant(sdk_message, session_id)
        if isinstance(sdk_message, UserMessage):
            return self._handle_user(sdk_message, session_id)
        if isinstance(sdk_message, ResultMessage):
            return self._handle_result(sdk_message, session_id)
        if isinstance(sdk_message, StreamEvent):
            return self._handle_stream(sdk_message, session_id)
        # RateLimitEvent / 未知类型：丢弃
        return []

    # ----- 各 SDK message 类型分发 -----

    def _handle_system(
        self,
        msg: SystemMessage,
        session_id: str | None,
    ) -> list[NormalizedMessage]:
        """``SystemMessage(init)`` → ``session_created``；其他 subtype 丢弃。"""
        if msg.subtype != "init":
            return []
        sid = None
        if msg.data:
            sid = msg.data.get("session_id")
        out = _base(sid or session_id)
        out["frame_type"] = "session_created"
        if sid is not None:
            out["newSessionId"] = sid
        return [out]

    def _handle_assistant(
        self,
        msg: AssistantMessage,
        session_id: str | None,
    ) -> list[NormalizedMessage]:
        """``AssistantMessage`` → 遍历 content blocks 翻译。"""
        if msg.message_id:
            self._last_assistant_message_id = msg.message_id

        results: list[NormalizedMessage] = []
        for block in msg.content or []:
            if isinstance(block, TextBlock):
                if is_internal_content(block.text):
                    # 内部 system 注入文本：整条丢弃
                    continue
                out = _base(session_id)
                out["frame_type"] = "text"
                out["role"] = "assistant"
                out["content"] = block.text
                results.append(out)
            elif isinstance(block, ThinkingBlock):
                # 空 thinking 也照实输出（[smoke §2.3]）
                out = _base(session_id)
                out["frame_type"] = "thinking"
                out["content"] = block.thinking
                results.append(out)
            elif isinstance(block, ToolUseBlock):
                out = _base(session_id)
                out["frame_type"] = "tool_use"
                out["toolId"] = block.id
                out["toolName"] = block.name
                out["toolInput"] = block.input
                results.append(out)
            # ToolResultBlock 不会出现在 AssistantMessage（SDK 把它放在 UserMessage）
        return results

    def _handle_user(
        self,
        msg: UserMessage,
        session_id: str | None,
    ) -> list[NormalizedMessage]:
        """``UserMessage[ToolResult]`` → ``tool_result``（deny 集合命中则丢弃）。"""
        content = msg.content
        if not isinstance(content, list):
            # str 形式：用户原文输入，不回吐
            return []

        results: list[NormalizedMessage] = []
        for block in content:
            if not isinstance(block, ToolResultBlock):
                continue
            tool_use_id = block.tool_use_id
            if tool_use_id in self._pending_deny:
                # E8：deny 后 SDK 自动塞的 ToolResultBlock 必须丢弃
                # discard 一次后清掉，避免后续误抑制
                self._pending_deny.discard(tool_use_id)
                continue

            out = _base(session_id)
            out["frame_type"] = "tool_result"
            out["toolId"] = tool_use_id
            if block.content is not None:
                out["content"] = block.content
            if block.is_error is not None:
                out["isError"] = block.is_error
            results.append(out)
        return results

    def _handle_result(
        self,
        msg: ResultMessage,
        session_id: str | None,
    ) -> list[NormalizedMessage]:
        """``ResultMessage`` → ``complete``。"""
        out = _base(session_id)
        out["frame_type"] = "complete"
        out["exitCode"] = 1 if msg.is_error else 0
        if msg.duration_ms is not None:
            out["durationMs"] = msg.duration_ms

        usage = self._normalize_usage(msg.usage, total_cost_usd=msg.total_cost_usd)
        if usage:
            out["tokenBudget"] = usage

        if msg.is_error:
            error_msg: str | None = None
            if msg.errors:
                error_msg = msg.errors[0] if len(msg.errors) == 1 else " / ".join(msg.errors)
            elif msg.subtype:
                error_msg = msg.subtype
            if error_msg:
                out["error"] = error_msg
        return [out]

    def _handle_stream(
        self,
        msg: StreamEvent,
        session_id: str | None,
    ) -> list[NormalizedMessage]:
        """``StreamEvent`` → ``stream_status`` / ``stream_delta`` / ``stream_end``。

        翻译规则：

        - ``message_start`` → ``stream_status(phase="responding", model=...)``——
          首个 token 到达前给前端"开始生成"信号
        - ``content_block_start`` → ``stream_status(phase=映射, blockIndex, toolName?)``——
          按 block type 映射 phase，`tool_use` 类带工具名
        - ``content_block_delta`` → ``stream_delta(deltaType=...)``——含
          ``text`` / ``thinking`` / ``input_json`` 三种 delta
        - ``content_block_stop`` → ``stream_end``
        - ``message_delta`` / ``message_stop`` / 未知类型丢弃
        """
        event = msg.event or {}
        event_type = event.get("type")

        if event_type == "message_start":
            out = _base(session_id)
            out["frame_type"] = "stream_status"
            out["phase"] = "responding"
            inner = event.get("message") or {}
            model = inner.get("model")
            if isinstance(model, str) and model:
                out["model"] = model
            return [out]

        if event_type == "content_block_start":
            block = event.get("content_block") or {}
            block_type = block.get("type")
            phase = _BLOCK_TYPE_TO_PHASE.get(block_type) if isinstance(block_type, str) else None
            if phase is None:
                # 未知 block type：保守不产出 stream_status，避免误导前端
                return []
            out = _base(session_id)
            out["frame_type"] = "stream_status"
            out["phase"] = phase
            index = event.get("index")
            if isinstance(index, int):
                out["blockIndex"] = index
            if phase == "tool_calling":
                tool_name = block.get("name")
                if isinstance(tool_name, str) and tool_name:
                    out["toolName"] = tool_name
                tool_id = block.get("id")
                if isinstance(tool_id, str) and tool_id:
                    out["toolId"] = tool_id
            return [out]

        if event_type == "content_block_delta":
            delta = event.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                out = _base(session_id)
                out["frame_type"] = "stream_delta"
                out["content"] = delta.get("text", "")
                out["deltaType"] = "text"
                return [out]
            if delta_type == "thinking_delta":
                out = _base(session_id)
                out["frame_type"] = "stream_delta"
                out["content"] = delta.get("thinking", "")
                out["deltaType"] = "thinking"
                return [out]
            if delta_type == "input_json_delta":
                out = _base(session_id)
                out["frame_type"] = "stream_delta"
                out["content"] = delta.get("partial_json", "")
                out["deltaType"] = "input_json"
                return [out]
            # signature_delta / 未知 delta 类型丢弃
            return []
        if event_type == "content_block_stop":
            out = _base(session_id)
            out["frame_type"] = "stream_end"
            return [out]
        # message_delta / message_stop / 未知 SSE 控制帧丢弃
        return []

    # ----- 工具方法 -----

    @staticmethod
    def _normalize_usage(
        usage: dict[str, Any] | None,
        *,
        total_cost_usd: float | None = None,
    ) -> dict[str, Any]:
        """SDK ``usage`` snake_case → camelCase。

        - 已知字段走 ``key_map`` 显式映射（保证向前兼容老代码）
        - 未识别字段走自动 ``snake_to_camel``——避免 SDK 新增字段时产生
          `cache_creation` 跟 `cacheCreationTokens` 同时出现的混搭
          （e2e smoke 暴露的 Bug 2）
        - 嵌套 dict / list 内部的字段名**不递归**——交给前端宽松解析（ccui 同款）
        """
        key_map: dict[str, str] = {
            "input_tokens": "inputTokens",
            "output_tokens": "outputTokens",
            "cache_read_input_tokens": "cacheReadTokens",
            "cache_read_tokens": "cacheReadTokens",
            "cache_creation_input_tokens": "cacheCreationTokens",
            "cache_creation_tokens": "cacheCreationTokens",
        }
        out: dict[str, Any] = {}
        if usage:
            for key, value in usage.items():
                mapped = key_map.get(key)
                if mapped is None:
                    mapped = _snake_to_camel(key) if "_" in key else key
                if mapped not in out:
                    out[mapped] = value
        if total_cost_usd is not None and "totalCostUsd" not in out:
            out["totalCostUsd"] = total_cost_usd
        return out


def _snake_to_camel(s: str) -> str:
    """``server_tool_use`` → ``serverToolUse``。空段安全。"""
    parts = s.split("_")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:] if p)


__all__ = ["INTERNAL_CONTENT_PREFIXES", "ClaudeNormalizer"]
