"""共享 LLM 协议定义（v0.1）。

跨 Claude/Codex 归一化器共享的内部 ``NormalizedMessage`` 字典。Web 入站与
出站帧真源统一位于 ``hosts.web.protocol``。

设计要点：

- 协议宽松（dict + ``frame_type`` 字段）；TypedDict 仅用作 mypy 静态校验，运行时
  不强制 schema，方便 SDK 升级时新增字段
- ``provider`` 字段做多 backend 区分（``claude`` / ``codex`` / ``gemini`` /
  ``cursor``），v0.1 只实现 claude
- ``frame_type`` 列举 15 种已知值，**SDK 升级新增 frame_type 时直接扩 Literal**——
  消费方不应假设穷尽性
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, Required

from typing_extensions import TypedDict

# ---------------------------------------------------------------------------
# 字面量类型
# ---------------------------------------------------------------------------

MessageKind = Literal[
    "text",
    "tool_use",
    "tool_result",
    "thinking",
    "stream_delta",
    "stream_end",
    "stream_status",
    "session_created",
    "permission_request",
    "permission_cancelled",
    "complete",
    "error",
    "status",
    "interactive_prompt",
    "task_notification",
]
"""出站消息的 ``frame_type`` 字段取值集合（15 种）。

v0.1 实际产生约 10 种（``text`` / ``thinking`` / ``tool_use`` / ``tool_result`` /
``stream_delta`` / ``stream_end`` / ``stream_status`` / ``permission_request`` /
``session_created`` / ``complete``）；其余 5 种留作占位，未来扩展。

``stream_status`` 由 ``StreamEvent`` 的 ``message_start`` / ``content_block_start``
控制帧翻译而来，用于让前端在第一个 token 到达前就知道 agent 当前阶段
（思考中 / 生成中 / 调用工具）。
"""

LLMProvider = Literal["claude", "codex", "gemini", "cursor", "generic_chat"]
"""出站消息的 ``provider`` 字段取值集合。"""


# ---------------------------------------------------------------------------
# 出站消息（后端 → 前端）
# ---------------------------------------------------------------------------


class NormalizedMessage(TypedDict, total=False):
    """归一化的后端 → 前端消息字典。

    ``frame_type`` 是必填判别字段，其余字段可选；运行期消费方按该字段决定读取内容。

    Base 字段（所有 ``frame_type`` 都应该带）：

    - ``id``：消息唯一 id（UUID v4）
    - ``sessionId``：当前 SDK session id（``None`` 表示 session 未建立）
    - ``timestamp``：ISO8601 UTC 时间戳
    - ``provider``：::data:`LLMProvider`
    - ``frame_type``：::data:`MessageKind`
    """

    # base
    id: str
    sessionId: str | None
    timestamp: str
    provider: LLMProvider
    frame_type: Required[MessageKind]

    # text / thinking / stream_delta
    role: NotRequired[Literal["user", "assistant"]]
    content: NotRequired[Any]
    metadata: NotRequired[dict[str, Any]]
    historyIndex: NotRequired[int]

    # stream_status / stream_delta（流式进度元信息）
    # phase: 当前阶段（responding=生成文本 / thinking=思考 / tool_calling=调用工具）
    # blockIndex: SDK content_block 的 index（同一 turn 内自增）
    # deltaType: stream_delta 的子类型（text / thinking / input_json）
    # model: stream_status(message_start) 携带的 model 名
    phase: NotRequired[Literal["responding", "thinking", "tool_calling"]]
    blockIndex: NotRequired[int]
    deltaType: NotRequired[Literal["text", "thinking", "input_json"]]
    model: NotRequired[str]

    # tool_use / tool_result
    toolName: NotRequired[str]
    toolInput: NotRequired[Any]
    toolId: NotRequired[str]
    isError: NotRequired[bool]

    # permission_request / permission_cancelled
    requestId: NotRequired[str]
    input: NotRequired[Any]
    reason: NotRequired[str]
    autoApproveAtMs: NotRequired[int | None]
    autoRejectAtMs: NotRequired[int | None]
    blockedByRule: NotRequired[str | None]
    channel: NotRequired[str]

    # session_created
    newSessionId: NotRequired[str]

    # complete
    exitCode: NotRequired[int]
    aborted: NotRequired[bool]
    tokenBudget: NotRequired[dict[str, Any]]
    durationMs: NotRequired[int]

    # error
    error: NotRequired[str]


__all__ = [
    "LLMProvider",
    "MessageKind",
    "NormalizedMessage",
]
