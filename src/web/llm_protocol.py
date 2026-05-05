"""共享 LLM 协议定义（v0.1）。

跨 ``src/web/claude_code/`` 与未来的 ``src/web/codex/`` / ``src/web/gemini/``
模块共享的 wire 协议——前端 → 后端 4 类入站命令 + 后端 → 前端
:class:`NormalizedMessage` 字典。

设计要点：

- 协议宽松（dict + ``kind`` 字段）；TypedDict 仅用作 mypy 静态校验，运行时
  不强制 schema，方便 SDK 升级时新增字段
- ``provider`` 字段做多 backend 区分（``claude`` / ``codex`` / ``gemini`` /
  ``cursor``），v0.1 只实现 claude
- ``kind`` 列举 14 种已知值，**SDK 升级新增 kind 时直接扩 Literal**——
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
    "session_created",
    "permission_request",
    "permission_cancelled",
    "complete",
    "error",
    "status",
    "interactive_prompt",
    "task_notification",
]
"""出站消息的 ``kind`` 字段取值集合（14 种）。

v0.1 实际产生约 9 种（``text`` / ``thinking`` / ``tool_use`` / ``tool_result`` /
``stream_delta`` / ``stream_end`` / ``permission_request`` / ``session_created`` /
``complete``）；其余 5 种留作占位，未来扩展。
"""

LLMProvider = Literal["claude", "codex", "gemini", "cursor"]
"""出站消息的 ``provider`` 字段取值集合。v0.1 仅实现 ``claude``。"""


# ---------------------------------------------------------------------------
# 出站消息（后端 → 前端）
# ---------------------------------------------------------------------------


class NormalizedMessage(TypedDict, total=False):
    """归一化的后端 → 前端消息字典。

    `total=False` 让所有字段都可选；运行期消费方按 ``kind`` 决定读哪些字段。

    Base 字段（所有 ``kind`` 都应该带）：

    - ``id``：消息唯一 id（UUID v4）
    - ``sessionId``：当前 SDK session id（``None`` 表示 session 未建立）
    - ``timestamp``：ISO8601 UTC 时间戳
    - ``provider``：::data:`LLMProvider`
    - ``kind``：::data:`MessageKind`
    """

    # base
    id: str
    sessionId: str | None
    timestamp: str
    provider: LLMProvider
    kind: MessageKind

    # text / thinking / stream_delta
    role: NotRequired[Literal["user", "assistant"]]
    content: NotRequired[Any]

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

    type: Literal["claude-command"]
    command: str
    options: NotRequired[dict[str, Any]]


class CodexCommand(TypedDict, total=False):
    """前端发起的"调起 Codex run"命令（type 字段区分 provider）。"""

    type: Literal["codex-command"]
    command: str
    options: NotRequired[dict[str, Any]]


class ClaudePermissionResponse(TypedDict, total=False):
    """前端响应 ``permission_request`` 的决策。"""

    type: Literal["claude-permission-response"]
    requestId: str
    allow: bool
    updatedInput: NotRequired[Any]
    message: NotRequired[str]
    rememberEntry: NotRequired[str]


class AbortSession(TypedDict, total=False):
    """前端要求中止指定 session 的当前 run。"""

    type: Literal["abort-session"]
    sessionId: str
    provider: NotRequired[LLMProvider]


class CheckSessionStatus(TypedDict, total=False):
    """前端查询某 session 是否仍在跑（重连场景）。"""

    type: Literal["check-session-status"]
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
