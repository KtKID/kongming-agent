"""共享 LLM 协议定义（v0.1）。

跨 ``src/web/claude_code/`` 与未来的 ``src/web/codex/`` / ``src/web/gemini/``
模块共享的 wire 协议——前端 → 后端 4 类入站命令 + 后端 → 前端
:class:`NormalizedMessage` 字典。

设计要点：

- 协议宽松（dict + ``frame_type`` 字段）；TypedDict 仅用作 mypy 静态校验，运行时
  不强制 schema，方便 SDK 升级时新增字段
- ``provider`` 字段做多 backend 区分（``claude`` / ``codex`` / ``gemini`` /
  ``cursor``），v0.1 只实现 claude
- ``frame_type`` 列举 15 种已知值，**SDK 升级新增 frame_type 时直接扩 Literal**——
  消费方不应假设穷尽性
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

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

LLMProvider = Literal["claude", "codex", "gemini", "cursor"]
"""出站消息的 ``provider`` 字段取值集合。v0.1 仅实现 ``claude``。"""


# ---------------------------------------------------------------------------
# 出站消息（后端 → 前端）
# ---------------------------------------------------------------------------


class NormalizedMessage(TypedDict, total=False):
    """归一化的后端 → 前端消息字典。

    `total=False` 让所有字段都可选；运行期消费方按 ``frame_type`` 决定读哪些字段。

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
    frame_type: MessageKind

    # text / thinking / stream_delta
    role: NotRequired[Literal["user", "assistant"]]
    content: NotRequired[Any]

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

    # session_created
    newSessionId: NotRequired[str]

    # complete
    exitCode: NotRequired[int]
    aborted: NotRequired[bool]
    tokenBudget: NotRequired[dict[str, Any]]
    durationMs: NotRequired[int]

    # error
    error: NotRequired[str]


# ---------------------------------------------------------------------------
# 入站命令（前端 → 后端）
# ---------------------------------------------------------------------------


class ClaudeCommand(TypedDict, total=False):
    """前端发起的"调起 Claude run"命令。"""

    frame_type: Literal["claude-command"]
    command: str
    options: NotRequired[dict[str, Any]]


class CodexCommand(TypedDict, total=False):
    """前端发起的"调起 Codex run"命令（frame_type 字段区分 provider）。"""

    frame_type: Literal["codex-command"]
    command: str
    options: NotRequired[dict[str, Any]]


class ClaudePermissionResponse(TypedDict, total=False):
    """前端响应 ``permission_request`` 的决策。"""

    frame_type: Literal["claude-permission-response"]
    requestId: str
    allow: bool
    updatedInput: NotRequired[Any]
    message: NotRequired[str]
    rememberEntry: NotRequired[str]


class AbortSession(TypedDict, total=False):
    """前端要求中止指定 session 的当前 run。"""

    frame_type: Literal["abort-session"]
    sessionId: str
    provider: NotRequired[LLMProvider]


class CheckSessionStatus(TypedDict, total=False):
    """前端查询某 session 是否仍在跑（重连场景）。"""

    frame_type: Literal["check-session-status"]
    sessionId: str
    provider: NotRequired[LLMProvider]


__all__ = [
    "AbortSession",
    "CheckSessionStatus",
    "ClaudeCommand",
    "ClaudePermissionResponse",
    "CodexCommand",
    "LLMProvider",
    "MessageKind",
    "NormalizedMessage",
]
