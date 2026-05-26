"""WS 帧 Pydantic v2 模型定义（v0.1.5 web 宿主壳协议层）。

本文件集中定义全部 18 个 WebSocket 帧的数据模型——3 个 C2S（浏览器 → 后端）
+ 15 个 S2C（后端 → 浏览器）。每个帧类都带 ``frame_type: Literal[...]`` 字段，
供 Pydantic v2 的 discriminated union 在外层 union（``WSFrameC2S`` /
``WSFrameS2C``，由后续任务 #8 在 ``ws_frames`` 模块的 union 文件中聚合）做
``Field(discriminator='frame_type')`` 自动分派。

本文件**只**定义具体帧类，不定义 union 类型。union 类型由主流程在后续任务
（#8）单独引入；这里保持帧类纯粹，使添加新帧 / 调整字段时不会牵动 union
拼装逻辑。

公共枚举（``ErrorCode`` / ``EvictReason`` / ``ApprovalOutcome``）和帧基类
（``_C2SFrameBase`` / ``_S2CFrameBase``）均从 :mod:`web.protocol._base`
import，不在本文件重复定义。``ThreadHistoryFrame`` 引用的
``HistoryMessageDTO`` 由 :mod:`web.protocol.rest_models` 提供（REST 与 WS
共享同一份历史消息 DTO，避免漂移）。

protocol-frame-type-unify-v0.2 取整：所有 wire 协议判别字段统一为
``frame_type``（v0.1 已统一 claude / codex 业务协议，本期把 ws_frames 的
discriminator 字段、心跳跨通道 ping/pong、auto_approval / approval.inbox /
cron / SSE 进度帧等剩余 wire 帧字段名一并切换）。**业务字段（``UserInputAttachment.kind``
``WorkspaceTreeNodeDTO.kind`` ``EvolutionNutrientDTO.kind`` 等 rest_models 内
非 wire 判别用途的 ``kind``）不动**——这些是字段含义而非帧种类判别。
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
from web.protocol.rest_models import HistoryMessageDTO, UserInputAttachment

# ---------------------------------------------------------------------------
# C2S 帧（浏览器 → 后端,3 个）
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

    frame_type: Literal["approval.ack"] = "approval.ack"
    call_id: str
    action: Literal["accept_once", "accept_for_session", "reject"]


class PingFrame(_C2SFrameBase):
    """浏览器侧 keep-alive 心跳；后端以 ``pong`` 回应。"""

    frame_type: Literal["ping"] = "ping"
    ts: int | None = None  # 客户端发送时的 epoch ms，用于 RTT 计算


class InterruptFrame(_C2SFrameBase):
    """浏览器请求打断当前 thread 上正在进行的 run（interrupt-run-v0.1）。

    UX 入口：前端在 ``cell.status in ("running","awaiting_approval")`` 时显示
    "Stop" 按钮，点击后发本帧。

    后端 ws 路由层（``src/web/ws.py``）收到本帧 → 检查
    ``cell.current_run_task``：
    - ``None`` / 已 ``done()`` → 推 ``SystemNoticeFrame`` 提示 "no active run"
    - 否则调 ``task.cancel()`` → runner 顶层 except 收尾 → emit ``run.cancelled``
      event → WSEventSink fanout 转 :class:`RunInterruptedFrame` 给所有 attach
      的 ws（多 tab 自动同步）

    ``run_id`` 可选：``None`` = 打断当前正在跑的 run（最常见）；不为 None 时
    可以让后端校验"我要打断的就是这个 run"，避免 race（用户点 stop 那一刹那
    旧 run 刚好完成、新 run 又起来了）。本期前端不强制带，后端拿到也仅做
    诊断日志，不依赖它做正确性。
    """

    frame_type: Literal["interrupt"] = "interrupt"
    run_id: str | None = None


class UserInputFrame(_C2SFrameBase):
    """浏览器提交一轮用户输入；后端按 ``request_id`` 关联回执。

    claude-image-paste-e2e v0.1（contract layer）加 ``attachments`` 字段：
    用户在 Composer 粘贴图片后，前端先走 ``POST /api/uploads/images`` 拿到
    :class:`UserInputAttachment` 列表，再随本帧一并提交。后端按 ``asset_id``
    在 ``Message.metadata["attachments"]`` 留 ref，由 InputAssembler 组装
    成 provider 多模态消息。``None`` 表示纯文本输入（绝大多数轮次）。
    """

    frame_type: Literal["user.input"] = "user.input"
    text: str
    request_id: str
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    attachments: list[UserInputAttachment] | None = None


# ---------------------------------------------------------------------------
# S2C 帧（后端 → 浏览器，15 个）
# ---------------------------------------------------------------------------


class AssistantFinalFrame(_S2CFrameBase):
    """一轮 assistant 输出收尾的最终内容（非流式或流式累计完成态）。"""

    frame_type: Literal["assistant.final"] = "assistant.final"
    content: str
    turn: int
    run_id: str = ""


class ApprovalDecisionFrame(_S2CFrameBase):
    """审批结局通知（approved / rejected / cancelled）。"""

    frame_type: Literal["approval.decision"] = "approval.decision"
    call_id: str
    outcome: ApprovalOutcome
    turn: int


class ApprovalRequestFrame(_S2CFrameBase):
    """工具执行前向用户请求审批，浏览器需回 ``approval.ack``。

    v0.1.6+ elevated 审批：``policy_hint="elevated"`` 时前端应：
    - 隐藏「本 session 同意」按钮
    - 显示 ``confirm_token``（8 hex），用户需输入后才能点「同意」
    - 红色边框 / 警告图标视觉区分
    """

    frame_type: Literal["approval.request"] = "approval.request"
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    reason: str | None = None
    turn: int
    policy_hint: str | None = None
    confirm_token: str | None = None


class CellEvictedFrame(_S2CFrameBase):
    """thread cell 被回收（idle / 手动停止 / shutdown / 错误）。"""

    frame_type: Literal["cell.evicted"] = "cell.evicted"
    thread_id: str
    reason: EvictReason
    message: str | None = None


class ContentDeltaFrame(_S2CFrameBase):
    """assistant 文本流式增量（按 ``seq`` 重排）。"""

    frame_type: Literal["content.delta"] = "content.delta"
    delta: str
    turn: int
    seq: int
    run_id: str = ""


class ErrorFrame(_S2CFrameBase):
    """错误事件（network / llm_error / tool_error / approval_timeout / internal）。"""

    frame_type: Literal["error"] = "error"
    error_code: ErrorCode
    message: str
    turn: int | None = None


class RunInterruptedFrame(_S2CFrameBase):
    """run 被用户 interrupt 后的收尾通知（interrupt-run-v0.1）。

    触发路径：runner 顶层 ``except asyncio.CancelledError`` → emit
    ``run.cancelled`` event → WSEventSink fanout 转本帧给该 thread 名下
    所有 attach 的 ws（A tab 点 Stop → B tab 也收到）。

    后续 runner 还会 emit 一条 ``run.end``（status="cancelled"），上层
    cell.status 切回 idle；前端可隐藏 Stop 按钮、显示"已中断"提示。

    payload 字段语义见 :class:`core.contracts.EventKind` ``run.cancelled``
    段；``interrupted_tool_call_id`` 为 None 表示打断在 LLM / approval 阶段
    （pending tool 已被 runner 写占位 tool_result）。
    """

    frame_type: Literal["run.interrupted"] = "run.interrupted"
    run_id: str
    cancelled_at_turn: int
    cancelled_tool_call_id: str | None = None
    cancel_reason: str = "user_interrupt"


class PongFrame(_S2CFrameBase):
    """对 ``ping`` 的应答；含服务端 ``timestamp_ms`` + 客户端原始 ``ts``。"""

    frame_type: Literal["pong"] = "pong"
    ts: int | None = None  # 原样回传客户端的 ts


class SystemNoticeFrame(_S2CFrameBase):
    """系统级 notice。

    v0.1.9 首个来源是 self-evolution review 状态通知；字段保持通用，
    后续其它后端模块也可复用同一帧种类。
    """

    frame_type: Literal["system.notice"] = "system.notice"
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

    frame_type: Literal["reasoning.delta"] = "reasoning.delta"
    delta: str
    turn: int
    seq: int
    run_id: str = ""


class ThreadHistoryFrame(_S2CFrameBase):
    """连接建立 / resume 后下发的历史消息列表。"""

    frame_type: Literal["thread.history"] = "thread.history"
    messages: list[HistoryMessageDTO]


class ToolCallEndFrame(_S2CFrameBase):
    """单次工具执行结束（含成功 / 业务失败两态，异常走 ``error`` 帧）。

    v0.1.6 加 ``content`` / ``data`` 字段：之前 frame schema 漏掉
    :class:`core.contracts.ToolResult` 的 ``content`` / ``data``，导致 web UI
    永远看不到工具产出（只能看到 ok / error_message），实际显示 ``{}`` 是
    arguments 的回填假象。本字段补齐后下游前端可渲染真实工具结果。
    """

    frame_type: Literal["tool.call.end"] = "tool.call.end"
    call_id: str
    turn: int
    ok: bool
    error_message: str | None = None
    content: str = ""
    data: dict[str, Any] | None = None
    run_id: str = ""


class ToolCallStartFrame(_S2CFrameBase):
    """单次工具执行开始（在 ``approval.decision`` approved 之后）。"""

    frame_type: Literal["tool.call.start"] = "tool.call.start"
    tool_name: str
    call_id: str
    turn: int
    arguments: dict[str, Any]
    run_id: str = ""


class TurnEndFrame(_S2CFrameBase):
    """一轮 turn 结束标记。"""

    frame_type: Literal["turn.end"] = "turn.end"
    turn: int
    run_id: str = ""


class TurnStartFrame(_S2CFrameBase):
    """一轮 turn 开始标记。"""

    frame_type: Literal["turn.start"] = "turn.start"
    turn: int
    run_id: str = ""


class UsageFrame(_S2CFrameBase):
    """一轮 token 用量回报。

    **usage-token-v2-bigbang**：``usage`` 字段是分通道 DTO dict
    （``ClaudeUsage`` / ``CodexUsage`` / ``GenericChatAnthropicUsage``
    / ``GenericChatOpenAIUsage`` 之一的 ``model_dump()`` 输出），自带
    ``provider`` discriminator 字段。前端按 ``usage.provider`` narrowing。

    ``web.protocol`` 不允许 import ``web.usage_token_v2`` 内部类型
    （Contract 5 / web-protocol-no-deps），所以这一层用透明 ``dict`` 透传，
    前端 ``protocol.ts`` 用 strict union interface 描述。

    usage dict 形态（Claude 系，``provider="claude"``）::

        {
          "provider": "claude",
          "input_tokens": 6,
          "output_tokens": 881,
          "cache_read_input_tokens": 341086,
          "cache_creation_input_tokens": 431,
          "cache_creation": {
            "ephemeral_1h_input_tokens": 431,
            "ephemeral_5m_input_tokens": 0
          },
          "context_usage": 341523,
          "model": "claude-opus-4",
          "context_window": 1000000
        }

    Codex 系 ``provider="openai"``，含 ``total`` / ``last`` / ``model_context_window``
    / ``rate_limits``；详见 ``docs/usage-token-v2/04-data-and-state.md``。
    """

    frame_type: Literal["usage"] = "usage"
    turn: int
    run_id: str = ""
    usage: dict[str, Any]
    """嵌套 channel-specific DTO dict（含 ``provider`` discriminator）。"""


# ---------------------------------------------------------------------------
# Discriminated unions
#
# 用 Pydantic v2 的 ``Field(discriminator="frame_type")`` 让任意外层（路由 / WS
# 处理器 / 测试）能从 JSON 字典里按 ``frame_type`` 分派到具体帧类。
#
# - ``WSFrameC2S``：浏览器 → 后端的全部入站帧
# - ``WSFrameS2C``：后端 → 浏览器的全部出站帧
#
# 反序列化用法：
#     ``WSFrameC2SAdapter.validate_python({"frame_type": "user.input", "text": "...", "request_id": "..."})``
#     → 返回具体的 ``UserInputFrame`` 实例。
# 拿到错误的 frame_type 会被 Pydantic 拒绝（``ValidationError``）。
# ---------------------------------------------------------------------------


WSFrameC2S = Annotated[
    UserInputFrame | ApprovalAckFrame | PingFrame | InterruptFrame,
    Field(discriminator="frame_type"),
]
"""C2S 帧 union（discriminated by ``frame_type``）。"""


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
    | CellEvictedFrame
    | RunInterruptedFrame,
    Field(discriminator="frame_type"),
]
"""S2C 帧 union（discriminated by ``frame_type``）。"""


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
    "InterruptFrame",
    "PingFrame",
    "PongFrame",
    "ReasoningDeltaFrame",
    "RunInterruptedFrame",
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
