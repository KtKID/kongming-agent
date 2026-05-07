"""WS 帧 Pydantic v2 模型定义（v0.1.5 web 宿主壳协议层）。

本文件集中定义全部 18 个 WebSocket 帧的数据模型——3 个 C2S（浏览器 → 后端）
+ 15 个 S2C（后端 → 浏览器）。每个帧类都带 ``kind: Literal[...]`` 字段，
供 Pydantic v2 的 discriminated union 在外层 union（``WSFrameC2S`` /
``WSFrameS2C``，由后续任务 #8 在 ``ws_frames`` 模块的 union 文件中聚合）做
``Field(discriminator='kind')`` 自动分派。

本文件**只**定义具体帧类，不定义 union 类型。union 类型由主流程在后续任务
（#8）单独引入；这里保持帧类纯粹，使添加新帧 / 调整字段时不会牵动 union
拼装逻辑。

公共枚举（``ErrorCode`` / ``EvictReason`` / ``ApprovalOutcome``）和帧基类
（``_C2SFrameBase`` / ``_S2CFrameBase``）均从 :mod:`web.protocol._base`
import，不在本文件重复定义。``ThreadHistoryFrame`` 引用的
``HistoryMessageDTO`` 由 :mod:`web.protocol.rest_models` 提供（REST 与 WS
共享同一份历史消息 DTO，避免漂移）。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter

from web.protocol._base import (
    ApprovalOutcome,
    ErrorCode,
    EvictReason,
    _C2SFrameBase,
    _S2CFrameBase,
)
from web.protocol.rest_models import HistoryMessageDTO

# ---------------------------------------------------------------------------
# C2S 帧（浏览器 → 后端，3 个）
# ---------------------------------------------------------------------------


class ApprovalAckFrame(_C2SFrameBase):
    """浏览器对 ``approval.request`` 的应答（三态）。

    ``action`` 字面值与 :class:`core.contracts.ApprovalAction` 对齐，零翻译层：

    - ``accept_once``：仅本次放行
    - ``accept_for_session``：本次放行 + 写入 session GrantStore，本 thread
      后续同 capability 静默放行
    - ``reject``：拒绝；超时 / ESC 也走这条

    v0.1.6 升级：原 ``approved: bool`` 字段废弃，开发期不留兼容 shim
    （CLAUDE.md 第 1 条约束）。
    """

    kind: Literal["approval.ack"] = "approval.ack"
    call_id: str
    action: Literal["accept_once", "accept_for_session", "reject"]


class PingFrame(_C2SFrameBase):
    """浏览器侧 keep-alive 心跳；后端以 ``pong`` 回应。"""

    kind: Literal["ping"] = "ping"


class UserInputFrame(_C2SFrameBase):
    """浏览器提交一轮用户输入；后端按 ``request_id`` 关联回执。"""

    kind: Literal["user.input"] = "user.input"
    text: str
    request_id: str
    reasoning_effort: Literal["low", "medium", "high"] | None = None


# ---------------------------------------------------------------------------
# S2C 帧（后端 → 浏览器，15 个）
# ---------------------------------------------------------------------------


class AssistantFinalFrame(_S2CFrameBase):
    """一轮 assistant 输出收尾的最终内容（非流式或流式累计完成态）。"""

    kind: Literal["assistant.final"] = "assistant.final"
    content: str
    turn: int
    run_id: str = ""


class ApprovalDecisionFrame(_S2CFrameBase):
    """审批结局通知（approved / rejected / cancelled）。"""

    kind: Literal["approval.decision"] = "approval.decision"
    call_id: str
    outcome: ApprovalOutcome
    turn: int


class ApprovalRequestFrame(_S2CFrameBase):
    """工具执行前向用户请求审批，浏览器需回 ``approval.ack``。"""

    kind: Literal["approval.request"] = "approval.request"
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    reason: str | None = None
    turn: int


class CellEvictedFrame(_S2CFrameBase):
    """thread cell 被回收（idle / 手动停止 / shutdown / 错误）。"""

    kind: Literal["cell.evicted"] = "cell.evicted"
    thread_id: str
    reason: EvictReason
    message: str | None = None


class ContentDeltaFrame(_S2CFrameBase):
    """assistant 文本流式增量（按 ``seq`` 重排）。"""

    kind: Literal["content.delta"] = "content.delta"
    delta: str
    turn: int
    seq: int
    run_id: str = ""


class ErrorFrame(_S2CFrameBase):
    """错误事件（network / llm_error / tool_error / approval_timeout / internal）。"""

    kind: Literal["error"] = "error"
    error_code: ErrorCode
    message: str
    turn: int | None = None


class PongFrame(_S2CFrameBase):
    """对 ``ping`` 的应答；仅含 ``timestamp_ms``。"""

    kind: Literal["pong"] = "pong"


class SystemNoticeFrame(_S2CFrameBase):
    """系统级 notice。

    v0.1.9 首个来源是 self-evolution review 状态通知；字段保持通用，
    后续其它后端模块也可复用同一帧种类。
    """

    kind: Literal["system.notice"] = "system.notice"
    notice_key: str
    source: str
    status: str
    title: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    icon: str
    run_id: str = ""


class ReasoningDeltaFrame(_S2CFrameBase):
    """assistant reasoning 流式增量（按 ``seq`` 重排）。"""

    kind: Literal["reasoning.delta"] = "reasoning.delta"
    delta: str
    turn: int
    seq: int
    run_id: str = ""


class ThreadHistoryFrame(_S2CFrameBase):
    """连接建立 / resume 后下发的历史消息列表。"""

    kind: Literal["thread.history"] = "thread.history"
    messages: list[HistoryMessageDTO]


class ToolCallEndFrame(_S2CFrameBase):
    """单次工具执行结束（含成功 / 业务失败两态，异常走 ``error`` 帧）。

    v0.1.6 加 ``content`` / ``data`` 字段：之前 frame schema 漏掉
    :class:`core.contracts.ToolResult` 的 ``content`` / ``data``，导致 web UI
    永远看不到工具产出（只能看到 ok / error_message），实际显示 ``{}`` 是
    arguments 的回填假象。本字段补齐后下游前端可渲染真实工具结果。
    """

    kind: Literal["tool.call.end"] = "tool.call.end"
    call_id: str
    turn: int
    ok: bool
    error_message: str | None = None
    content: str = ""
    data: dict[str, Any] | None = None
    run_id: str = ""


class ToolCallStartFrame(_S2CFrameBase):
    """单次工具执行开始（在 ``approval.decision`` approved 之后）。"""

    kind: Literal["tool.call.start"] = "tool.call.start"
    tool_name: str
    call_id: str
    turn: int
    arguments: dict[str, Any]
    run_id: str = ""


class TurnEndFrame(_S2CFrameBase):
    """一轮 turn 结束标记。"""

    kind: Literal["turn.end"] = "turn.end"
    turn: int
    run_id: str = ""


class TurnStartFrame(_S2CFrameBase):
    """一轮 turn 开始标记。"""

    kind: Literal["turn.start"] = "turn.start"
    turn: int
    run_id: str = ""


class UsageFrame(_S2CFrameBase):
    """一轮 token 用量回报（prompt / completion / total + optional cache）。"""

    kind: Literal["usage"] = "usage"
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    turn: int
    run_id: str = ""


# ---------------------------------------------------------------------------
# Discriminated unions
#
# 用 Pydantic v2 的 ``Field(discriminator="kind")`` 让任意外层（路由 / WS
# 处理器 / 测试）能从 JSON 字典里按 ``kind`` 分派到具体帧类。
#
# - ``WSFrameC2S``：浏览器 → 后端的全部入站帧
# - ``WSFrameS2C``：后端 → 浏览器的全部出站帧
#
# 反序列化用法：
#     ``WSFrameC2SAdapter.validate_python({"kind": "user.input", "text": "...", "request_id": "..."})``
#     → 返回具体的 ``UserInputFrame`` 实例。
# 拿到错误的 kind 会被 Pydantic 拒绝（``ValidationError``）。
# ---------------------------------------------------------------------------


WSFrameC2S = Annotated[
    UserInputFrame | ApprovalAckFrame | PingFrame,
    Field(discriminator="kind"),
]
"""C2S 帧 union（discriminated by ``kind``）。"""


WSFrameS2C = Annotated[
    ThreadHistoryFrame
    | AssistantFinalFrame
    | ContentDeltaFrame
    | ReasoningDeltaFrame
    | ToolCallStartFrame
    | ToolCallEndFrame
    | ApprovalRequestFrame
    | ApprovalDecisionFrame
    | UsageFrame
    | ErrorFrame
    | TurnStartFrame
    | TurnEndFrame
    | PongFrame
    | SystemNoticeFrame
    | CellEvictedFrame,
    Field(discriminator="kind"),
]
"""S2C 帧 union（discriminated by ``kind``）。"""


# TypeAdapter 让外层无需写 ``RootModel`` 包装即可消费 union。
# 注意：``TypeAdapter`` 自身有缓存代价；在 module 顶层创建一次复用即可。
WSFrameC2SAdapter: TypeAdapter[WSFrameC2S] = TypeAdapter(WSFrameC2S)
"""``WSFrameC2S`` 的 TypeAdapter，提供 ``validate_python`` / ``validate_json`` / ``dump_*``。"""

WSFrameS2CAdapter: TypeAdapter[WSFrameS2C] = TypeAdapter(WSFrameS2C)
"""``WSFrameS2C`` 的 TypeAdapter，提供 ``validate_python`` / ``validate_json`` / ``dump_*``。"""


__all__: list[str] = [
    "ApprovalAckFrame",
    "ApprovalDecisionFrame",
    "ApprovalRequestFrame",
    "AssistantFinalFrame",
    "CellEvictedFrame",
    "ContentDeltaFrame",
    "ErrorFrame",
    "PingFrame",
    "PongFrame",
    "ReasoningDeltaFrame",
    "SystemNoticeFrame",
    "ThreadHistoryFrame",
    "ToolCallEndFrame",
    "ToolCallStartFrame",
    "TurnEndFrame",
    "TurnStartFrame",
    "UsageFrame",
    "UserInputFrame",
    "WSFrameC2S",
    "WSFrameC2SAdapter",
    "WSFrameS2C",
    "WSFrameS2CAdapter",
]
