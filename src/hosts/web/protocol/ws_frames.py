"""WS 帧 Pydantic v2 模型定义（v0.1.5 web 宿主壳协议层）。

本文件集中定义 WebSocket 帧的数据模型。每个帧类都带
``frame_type: Literal[...]`` 字段，
供 Pydantic v2 的 discriminated union 在外层 union（``WSFrameC2S`` /
``WSFrameS2C``，由后续任务 #8 在 ``ws_frames`` 模块的 union 文件中聚合）做
``Field(discriminator='frame_type')`` 自动分派。

本文件**只**定义具体帧类，不定义 union 类型。union 类型由主流程在后续任务
（#8）单独引入；这里保持帧类纯粹，使添加新帧 / 调整字段时不会牵动 union
拼装逻辑。

公共枚举（``ErrorCode`` / ``EvictReason`` / ``ApprovalOutcome``）和帧基类
（``_C2SFrameBase`` / ``_S2CFrameBase``）均从 :mod:`web.protocol._base`
import，不在本文件重复定义。``ThreadHistoryFrame`` 引用
:class:`web.app_support.llm_protocol.NormalizedMessage`，与 Claude/Codex 历史形态对齐。

protocol-frame-type-unify-v0.2 取整：所有 wire 协议判别字段统一为
``frame_type``（v0.1 已统一 claude / codex 业务协议，本期把 ws_frames 的
discriminator 字段、心跳跨通道 ping/pong、auto_approval / approval.inbox /
cron / SSE 进度帧等剩余 wire 帧字段名一并切换）。**业务字段（``UserInputAttachment.kind``
``WorkspaceTreeNodeDTO.kind`` ``EvolutionNutrientDTO.kind`` 等 rest_models 内
非 wire 判别用途的 ``kind``）不动**——这些是字段含义而非帧种类判别。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, model_validator

from hosts.web.app_support.llm_protocol import NormalizedMessage
from hosts.web.protocol._base import (
    ApprovalOutcome,
    ErrorCode,
    EvictReason,
    _C2SFrameBase,
    _FrameBase,
    _S2CFrameBase,
)
from hosts.web.protocol.conversation_references import ConversationReferenceDTO
from hosts.web.protocol.rest_models import UserInputAttachment

# ---------------------------------------------------------------------------
# C2S 帧（浏览器 → 后端）
# ---------------------------------------------------------------------------


class PingFrame(_C2SFrameBase):
    """浏览器侧 keep-alive 心跳；后端以 ``pong`` 回应。"""

    frame_type: Literal["ping"] = "ping"
    ts: int | None = None  # 客户端发送时的 epoch ms，用于 RTT 计算


class InterruptFrame(_C2SFrameBase):
    """浏览器请求打断当前 thread 上正在进行的 run（interrupt-run-v0.1）。

    UX 入口：前端在 ``cell.status in ("running","awaiting_approval")`` 时显示
    "Stop" 按钮，点击后发本帧。

    后端 ws 路由层（``src/hosts/web/websocket/routes.py``）收到本帧 → 检查
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
    reasoning_effort: Literal["none", "low", "medium", "high", "max"] | None = None
    attachments: list[UserInputAttachment] | None = None
    references: list[ConversationReferenceDTO] | None = None


class ChoiceAnswerDTO(_C2SFrameBase):
    """用户对单个 Choice 问题的确认答案。"""

    question_id: str
    option_id: str
    option_label: str
    custom_text: str | None = None
    value: dict[str, Any] | None = None


class ChoiceSubmitFrame(_C2SFrameBase):
    """浏览器提交 ChoicePanel 的结构化选择结果。"""

    frame_type: Literal["choice.submit"] = "choice.submit"
    request_id: str
    answers: list[ChoiceAnswerDTO]


class PendingInputUpdateFrame(_C2SFrameBase):
    """浏览器编辑尚未启动的 pending input。

    content 是用户保存后的完整文本；后端负责 trim、空内容拒绝和 version 递增。
    """

    frame_type: Literal["pending-input.update"] = "pending-input.update"
    pending_input_id: str
    content: str


class PendingInputCancelFrame(_C2SFrameBase):
    """浏览器删除尚未启动的 pending input。

    pending_input_id 只定位队列项；运行中的 current_run_task 取消仍走 interrupt。
    """

    frame_type: Literal["pending-input.cancel"] = "pending-input.cancel"
    pending_input_id: str


class PendingInputReorderFrame(_C2SFrameBase):
    """浏览器提交尚未启动 pending input 的最终排序。

    ordered_ids 是松手后的完整队列 ID 顺序；后端校验它与当前队列项集合一致后
    重写 sequence，并通过 pending-input.changed(reason="reordered") 回写真源。
    """

    frame_type: Literal["pending-input.reorder"] = "pending-input.reorder"
    ordered_ids: list[str]


class PendingInputSendNowFrame(_C2SFrameBase):
    """浏览器请求把某条 queued pending input 立即发送。

    后端在 active run 下走 Runner.steer；idle 下启动下一轮 root mailbox run。
    """

    frame_type: Literal["pending-input.send-now"] = "pending-input.send-now"
    pending_input_id: str
    request_id: str | None = None


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
    reason: str | None = None


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
    messages: list[NormalizedMessage]


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
    history_index: int | None = None
    has_tool_calls: bool = False


class TurnStartFrame(_S2CFrameBase):
    """一轮 turn 开始标记。"""

    frame_type: Literal["turn.start"] = "turn.start"
    turn: int
    run_id: str = ""


class CronMessageAppendedFrame(_S2CFrameBase):
    """cron 结果已追加到绑定 thread 的聊天时间线。"""

    frame_type: Literal["cron.message.appended"] = "cron.message.appended"
    thread_id: str
    content: str
    message_id: str
    run_id: str
    task_id: str
    session_id: str
    task_name: str = ""


CronRunTerminalStatusValue = Literal[
    "completed",
    "silent",
    "failed",
    "inactivity_timeout",
    "abandoned",
    "cancelled",
]


class CronRunStartedFrame(_S2CFrameBase):
    """定时任务 run 已持久化 RUNNING，面板可加入 live run 集合。"""

    frame_type: Literal["cron.run.started"] = "cron.run.started"
    task_id: str
    task_name: str
    run_id: str
    thread_id: str
    session_id: str | None
    scheduled_for: str
    started_at: str | None
    status: Literal["running"]


class CronRunFinishedFrame(_S2CFrameBase):
    """定时任务 run 已持久化 terminal，面板可移除对应 live run。"""

    frame_type: Literal["cron.run.finished"] = "cron.run.finished"
    task_id: str
    task_name: str
    run_id: str
    thread_id: str
    session_id: str | None
    scheduled_for: str
    started_at: str | None
    finished_at: str | None
    status: CronRunTerminalStatusValue
    final_message: str | None
    error_message: str | None
    delivery_error: str | None
    next_run_at: str | None


class CronRunCompletedFrame(_S2CFrameBase):
    """定时任务 terminal 已交给 Web delivery broker。"""

    frame_type: Literal["cron.run.completed"] = "cron.run.completed"
    task_id: str
    task_name: str
    run_id: str
    thread_id: str
    session_id: str | None
    final_message: str
    delivered_at_iso: str | None
    scheduled_for: str
    delivery_target: str | None
    next_run_at: str | None
    status: CronRunTerminalStatusValue


class UsageFrame(_S2CFrameBase):
    """一轮 token 用量回报。

    **usage-token-v2-bigbang**：``usage`` 字段是分通道 DTO dict
    （``ClaudeUsage`` / ``CodexUsage`` / ``GenericChatAnthropicUsage``
    / ``GenericChatOpenAIUsage`` 之一的 ``model_dump()`` 输出），自带
    ``provider`` discriminator 字段。前端按 ``usage.provider`` narrowing。

    ``web.protocol`` 不允许 import ``web.usage.usage_token_v2`` 内部类型
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


class UsageSummaryUpdatedFrame(_FrameBase):
    """thread-status 全局 WS 通道上的 usage summary 刷新帧。

    Claude/Codex service 在后台派生最新 thread usage 后广播本帧；前端
    ``useThreadStatusWS`` 收到后更新 ``usageByThread``。该帧走全局
    ``/ws/thread-status`` 通道，wire shape 沿用当前生产形态，不携带
    ``timestamp_ms`` / ``turn`` / ``run_id``。
    """

    frame_type: Literal["usage_summary_updated"] = "usage_summary_updated"
    threadId: str
    usage: dict[str, Any]


ThreadStatusPhase = Literal[
    "idle",
    "responding",
    "thinking",
    "tool_calling",
    "waiting_approval",
    "complete",
    "error",
]
"""``/ws/thread-status`` 广播的运行阶段枚举。"""


class ThreadStatusFrame(_FrameBase):
    """``/ws/thread-status`` 上的线程运行阶段广播帧。

    ``run_end_reason`` 仅在终态帧（run.end 触发的 phase 变更）携带，是
    :class:`core.result.RunEndReason` bitmask 的 int 值。前端据此决定：
    按钮复位（``reason > 0``）+ UI 显示（INTERRUPT 位优先"已停止"）。
    非终态帧不携带（None），保证终态字段语义清晰。
    """

    frame_type: Literal["thread-status"] = "thread-status"
    threadId: str
    phase: ThreadStatusPhase
    sequence: int
    runId: str
    runGeneration: int
    toolName: str | None = None
    run_end_reason: int | None = None


class ThreadStatusSnapshotFrame(_FrameBase):
    """新 ``/ws/thread-status`` 连接收到的 active 状态全量快照。"""

    frame_type: Literal["thread-status.snapshot"] = "thread-status.snapshot"
    watermark: int
    items: list[ThreadStatusFrame]


class RememberRule(_FrameBase):
    """审批卡展示并原样回传的 canonical 规则候选。"""

    expression: str
    displayText: str
    scopeCwd: str | None


class ApprovalInboxItem(_FrameBase):
    """全局 approval inbox 单条审批项，不含 ``frame_type``。"""

    requestId: str
    threadId: str
    toolName: str
    toolInput: Any
    blockedByRule: str | None
    isElevated: bool
    danger: bool
    rememberAllowed: bool
    channel: str
    cwd: str
    arrivedAtMs: int
    timeoutMs: int | None = None
    autoApproveAtMs: int | None = None
    autoRejectAtMs: int | None = None
    rememberRule: RememberRule | None


class ApprovalInboxAddFrame(ApprovalInboxItem):
    """全局 approval inbox 新增审批项广播帧。"""

    frame_type: Literal["approval.inbox.add"] = "approval.inbox.add"


ApprovalInboxRemoveReason = Literal[
    "user_decided",
    "timeout",
    "cancelled",
    "auto_allowed",
]
"""全局 approval inbox 删除原因枚举。"""


class ApprovalInboxRemoveFrame(_FrameBase):
    """全局 approval inbox 删除审批项广播帧。"""

    frame_type: Literal["approval.inbox.remove"] = "approval.inbox.remove"
    requestId: str
    reason: ApprovalInboxRemoveReason


class ApprovalInboxSnapshotFrame(_FrameBase):
    """全局 approval inbox 连接建立时的全量快照帧。"""

    frame_type: Literal["approval.inbox.snapshot"] = "approval.inbox.snapshot"
    items: list[ApprovalInboxItem]


class ApprovalInboxResolveFrame(_C2SFrameBase):
    """浏览器对全局 approval inbox 审批项的决策帧。"""

    frame_type: Literal["approval.inbox.resolve"] = "approval.inbox.resolve"
    threadId: str
    requestId: str
    allow: bool
    remember: bool = False
    rememberRule: RememberRule | None = None
    message: str | None = None

    @model_validator(mode="after")
    def _require_remember_rule_for_remember(self) -> ApprovalInboxResolveFrame:
        """记忆决策必须原样回传服务端冻结候选。"""
        if self.remember and self.rememberRule is None:
            raise ValueError("rememberRule is required when remember=true")
        return self


class ApprovalInboxResolveResultFrame(_FrameBase):
    """审批 resolve 的服务端接收结果；失败时 pending 保持可重试。"""

    frame_type: Literal["approval.inbox.resolve_result"] = "approval.inbox.resolve_result"
    requestId: str
    accepted: bool
    message: str | None = None


class AutoApprovalSetModeFrame(_C2SFrameBase):
    """浏览器设置指定 cwd 的审批处置模式。"""

    frame_type: Literal["auto-approval-set-mode"] = "auto-approval-set-mode"
    cwd: str
    mode: Literal["user", "llm", "full_trust"]


class AutoApprovalQueryFrame(_C2SFrameBase):
    """浏览器查询指定 cwd 的自动审批状态。"""

    frame_type: Literal["auto-approval-query"] = "auto-approval-query"
    cwd: str


class AutoApprovalStateFrame(_FrameBase):
    """后端返回指定 cwd 的审批处置模式。"""

    frame_type: Literal["auto_approval_state"] = "auto_approval_state"
    channel: Literal["claude_code", "generic_chat"]
    cwd: str
    mode: Literal["user", "llm", "full_trust"]
    timeoutMs: int
    ruleOverrides: dict[str, bool]


class AbortSessionFrame(_C2SFrameBase):
    """浏览器请求中断指定 session。"""

    frame_type: Literal["abort-session"] = "abort-session"
    sessionId: str
    provider: str | None = None


class CheckSessionStatusFrame(_C2SFrameBase):
    """浏览器查询指定 session 是否仍在运行。"""

    frame_type: Literal["check-session-status"] = "check-session-status"
    sessionId: str
    provider: str | None = None


class SessionStatusFrame(_FrameBase):
    """``check-session-status`` 的出站应答帧。"""

    frame_type: Literal["session-status"] = "session-status"
    sessionId: str
    isProcessing: bool


ReasoningEffort = Literal["none", "low", "medium", "high", "max"]
"""前端 composer 透传的 reasoning effort 枚举。"""


class CodexPermissionMode(StrEnum):
    """Codex CLI permission mode 枚举。"""

    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    BYPASS_PERMISSIONS = "bypassPermissions"


class CodexCommandOptions(_FrameBase):
    """``codex-command`` 的可选参数。"""

    cwd: str | None = None
    sessionId: str | None = None
    resume: bool | None = None
    model: str | None = None
    permissionMode: CodexPermissionMode | None = None
    reasoningEffort: ReasoningEffort | None = None


class CodexCommandFrame(_C2SFrameBase):
    """浏览器发起 Codex run 的命令帧。"""

    frame_type: Literal["codex-command"] = "codex-command"
    command: str
    options: CodexCommandOptions | None = None
    attachments: list[UserInputAttachment] | None = None


class ChoiceOptionDTO(_C2SFrameBase):
    """Choice 请求里的单个 LLM 候选选项。"""

    id: str
    label: str
    description: str
    value: dict[str, Any] | None = None


class ChoiceQuestionDTO(_C2SFrameBase):
    """Choice 请求里的单个问题。"""

    id: str
    title: str
    description: str | None = None
    options: list[ChoiceOptionDTO]


class ChoiceRequestFrame(_S2CFrameBase):
    """后端要求浏览器在 composer 上方展示选择面板。"""

    frame_type: Literal["choice.request"] = "choice.request"
    request_id: str
    title: str
    description: str
    questions: list[ChoiceQuestionDTO]
    turn: int
    run_id: str = ""


class PendingInputDTO(_FrameBase):
    """队列中尚未启动的普通输入。

    该 DTO 是后端 ThreadCell.pending_inputs 对前端的投影；content 是执行真源，
    preview 只用于列表展示，sequence 只在同优先级排序中生效。
    """

    # pin-* 稳定 ID，用于编辑、删除、排序和 started 事件关联。
    id: str
    # 队列归属 thread；前端 reducer 用它过滤跨 tab 或旧连接帧。
    thread_id: str
    # 输入来源：普通 composer、ChoicePanel 或 Avatar。
    source: Literal["user_input", "choice_submit", "avatar"]
    # drain 优先级：choice_response 排在普通 user_message 前面。
    priority: Literal["choice_response", "user_message"]
    # 完整输入内容，启动 run 时交给 root agent mailbox。
    content: str
    # 列表展示用短文本，不能作为执行内容。
    preview: str
    # queued 表示仍在列表中；starting 表示刚出队并交给 current_run_task。
    status: Literal["queued", "starting"] = "queued"
    created_at_ms: int
    updated_at_ms: int
    # cell 内单调递增序号，用于同优先级 FIFO 和手动排序。
    sequence: int
    # request_id、reasoning_effort、attachments、references 等透传运行参数。
    metadata: dict[str, Any] = Field(default_factory=dict)


class PendingInputSnapshotFrame(_S2CFrameBase):
    """pending input 队列全量快照。

    WS 建连后发送；前端用 thread_id + version 合并状态，items 是完整后端真源。
    """

    frame_type: Literal["pending-input.snapshot"] = "pending-input.snapshot"
    thread_id: str
    items: list[PendingInputDTO]
    max_items: int
    active_run_id: str | None = None
    version: int


class PendingInputChangedFrame(_S2CFrameBase):
    """pending input 队列变更后的全量快照。

    任意 add/update/remove/reorder/drain 后发送；reason 用于 UI 诊断和测试断言。
    items 是完整队列真源。
    """

    frame_type: Literal["pending-input.changed"] = "pending-input.changed"
    thread_id: str
    items: list[PendingInputDTO]
    max_items: int
    reason: Literal[
        "added",
        "updated",
        "removed",
        "reordered",
        "drained",
        "cleared",
        "sent_now",
        "steer_undelivered",
    ]
    active_run_id: str | None = None
    version: int


class PendingInputSteeredFrame(_S2CFrameBase):
    """send-now 输入已写入当前活跃 run 的 steer buffer。

    pending_input 携带被认领队列项的最终内容；聊天 timeline 可用它立即生成用户气泡。
    真正进入模型上下文仍由 Runner 在下一个 turn 边界 drain。
    """

    frame_type: Literal["pending-input.steered"] = "pending-input.steered"
    thread_id: str
    pending_input_id: str
    pending_input: PendingInputDTO
    active_run_id: str | None = None
    run_id: str = ""
    turn: int | None = None
    version: int


class PendingInputStartedFrame(_S2CFrameBase):
    """某个 pending input 已经成为下一轮 run。

    pending_input 携带后端确认启动时的最终内容；前端聊天 timeline 只从这里
    生成用户气泡，队列编辑后的内容会自然生效。随后的 changed 帧继续校准完整列表。
    """

    frame_type: Literal["pending-input.started"] = "pending-input.started"
    thread_id: str
    pending_input_id: str
    pending_input: PendingInputDTO
    run_id: str = ""
    version: int


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


GenericChatC2S = Annotated[
    UserInputFrame
    | PingFrame
    | InterruptFrame
    | ChoiceSubmitFrame
    | PendingInputUpdateFrame
    | PendingInputCancelFrame
    | PendingInputReorderFrame
    | PendingInputSendNowFrame
    | AutoApprovalSetModeFrame
    | AutoApprovalQueryFrame,
    Field(discriminator="frame_type"),
]
"""C2S 帧 union（discriminated by ``frame_type``）。"""


GenericChatS2C = Annotated[
    ThreadHistoryFrame
    | AssistantFinalFrame
    | ContentDeltaFrame
    | ReasoningDeltaFrame
    | ToolCallStartFrame
    | ToolCallEndFrame
    | ApprovalDecisionFrame
    | UsageFrame
    | UsageSummaryUpdatedFrame
    | ErrorFrame
    | TurnStartFrame
    | TurnEndFrame
    | CronMessageAppendedFrame
    | CronRunStartedFrame
    | CronRunFinishedFrame
    | CronRunCompletedFrame
    | PongFrame
    | SystemNoticeFrame
    | CellEvictedFrame
    | RunInterruptedFrame
    | ChoiceRequestFrame
    | PendingInputSnapshotFrame
    | PendingInputChangedFrame
    | PendingInputSteeredFrame
    | PendingInputStartedFrame
    | AutoApprovalStateFrame,
    Field(discriminator="frame_type"),
]
"""S2C 帧 union（discriminated by ``frame_type``）。"""


ThreadStatusC2S = Annotated[
    PingFrame | ApprovalInboxResolveFrame,
    Field(discriminator="frame_type"),
]
"""``/ws/thread-status`` C2S 帧 union。"""


ThreadStatusS2C = Annotated[
    PongFrame
    | ThreadStatusFrame
    | ThreadStatusSnapshotFrame
    | UsageSummaryUpdatedFrame
    | ApprovalInboxAddFrame
    | ApprovalInboxRemoveFrame
    | ApprovalInboxSnapshotFrame
    | ApprovalInboxResolveResultFrame,
    Field(discriminator="frame_type"),
]
"""``/ws/thread-status`` S2C 帧 union。"""


CodexC2S = Annotated[
    CodexCommandFrame | AbortSessionFrame | CheckSessionStatusFrame,
    Field(discriminator="frame_type"),
]
"""``/ws/codex`` C2S 帧 union。"""


CodexS2C = Annotated[
    NormalizedMessage | SessionStatusFrame,
    Field(discriminator="frame_type"),
]
"""``/ws/codex`` S2C 控制帧与归一化消息 union。"""


WSFrameC2S = GenericChatC2S
"""兼容旧导入名：generic_chat C2S 帧 union。"""


WSFrameS2C = GenericChatS2C
"""兼容旧导入名：generic_chat S2C 帧 union。"""


# TypeAdapter 让外层无需写 ``RootModel`` 包装即可消费 union。
# 注意：``TypeAdapter`` 自身有缓存代价；在 module 顶层创建一次复用即可。
WSFrameC2SAdapter: TypeAdapter[WSFrameC2S] = TypeAdapter(WSFrameC2S)
"""``WSFrameC2S`` 的 TypeAdapter，提供 ``validate_python`` / ``validate_json`` / ``dump_*``。"""

WSFrameS2CAdapter: TypeAdapter[WSFrameS2C] = TypeAdapter(WSFrameS2C)
"""``WSFrameS2C`` 的 TypeAdapter，提供 ``validate_python`` / ``validate_json`` / ``dump_*``。"""

GenericChatC2SAdapter: TypeAdapter[GenericChatC2S] = TypeAdapter(GenericChatC2S)
"""generic_chat C2S 帧 TypeAdapter。"""

GenericChatS2CAdapter: TypeAdapter[GenericChatS2C] = TypeAdapter(GenericChatS2C)
"""generic_chat S2C 帧 TypeAdapter。"""

ThreadStatusC2SAdapter: TypeAdapter[ThreadStatusC2S] = TypeAdapter(ThreadStatusC2S)
"""``/ws/thread-status`` C2S 帧 TypeAdapter。"""

ThreadStatusS2CAdapter: TypeAdapter[ThreadStatusS2C] = TypeAdapter(ThreadStatusS2C)
"""``/ws/thread-status`` S2C 帧 TypeAdapter。"""

CodexC2SAdapter: TypeAdapter[CodexC2S] = TypeAdapter(CodexC2S)
"""``/ws/codex`` C2S 帧 TypeAdapter。"""

CodexS2CAdapter: TypeAdapter[CodexS2C] = TypeAdapter(CodexS2C)
"""``/ws/codex`` S2C 帧 TypeAdapter。"""


__all__: list[str] = [
    "AbortSessionFrame",
    "ApprovalDecisionFrame",
    "ApprovalInboxAddFrame",
    "ApprovalInboxItem",
    "ApprovalInboxRemoveFrame",
    "ApprovalInboxRemoveReason",
    "ApprovalInboxResolveFrame",
    "ApprovalInboxResolveResultFrame",
    "ApprovalInboxSnapshotFrame",
    "AssistantFinalFrame",
    "AutoApprovalQueryFrame",
    "AutoApprovalSetModeFrame",
    "AutoApprovalStateFrame",
    "CellEvictedFrame",
    "CheckSessionStatusFrame",
    "ChoiceAnswerDTO",
    "ChoiceOptionDTO",
    "ChoiceQuestionDTO",
    "ChoiceRequestFrame",
    "ChoiceSubmitFrame",
    "CodexC2S",
    "CodexC2SAdapter",
    "CodexCommandFrame",
    "CodexCommandOptions",
    "CodexPermissionMode",
    "CodexS2C",
    "CodexS2CAdapter",
    "ContentDeltaFrame",
    "CronMessageAppendedFrame",
    "CronRunCompletedFrame",
    "CronRunFinishedFrame",
    "CronRunStartedFrame",
    "CronRunTerminalStatusValue",
    "ErrorFrame",
    "GenericChatC2S",
    "GenericChatC2SAdapter",
    "GenericChatS2C",
    "GenericChatS2CAdapter",
    "InterruptFrame",
    "PendingInputCancelFrame",
    "PendingInputChangedFrame",
    "PendingInputDTO",
    "PendingInputReorderFrame",
    "PendingInputSendNowFrame",
    "PendingInputSnapshotFrame",
    "PendingInputStartedFrame",
    "PendingInputUpdateFrame",
    "PingFrame",
    "PongFrame",
    "ReasoningDeltaFrame",
    "ReasoningEffort",
    "RememberRule",
    "RunInterruptedFrame",
    "SessionStatusFrame",
    "SystemNoticeFrame",
    "ThreadHistoryFrame",
    "ThreadStatusC2S",
    "ThreadStatusC2SAdapter",
    "ThreadStatusFrame",
    "ThreadStatusPhase",
    "ThreadStatusS2C",
    "ThreadStatusS2CAdapter",
    "ThreadStatusSnapshotFrame",
    "ToolCallEndFrame",
    "ToolCallStartFrame",
    "TurnEndFrame",
    "TurnStartFrame",
    "UsageFrame",
    "UsageSummaryUpdatedFrame",
    "UserInputFrame",
    "WSFrameC2S",
    "WSFrameC2SAdapter",
    "WSFrameS2C",
    "WSFrameS2CAdapter",
]
