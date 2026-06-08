"""WS / REST 协议层。

定义 v0.1.5 web 宿主壳的全部 WS 帧（17 种）和 REST 请求 / 响应 DTO（8 个），
作为后端其它 4 个模块（thread-manager / web-host-adapter / web-app-shell /
frontend）的接口契约真源。

设计约束：

- **零 sibling 依赖**：本子包不能 import 任何 ``core`` / ``prompting`` /
  ``executors`` / ``safety`` / ``tools`` / ``host`` / ``cli`` 等内核 / 装配层模块。
  这一点由 ``.importlinter`` 第 5 条 contract 强制（``web-protocol-no-deps``）。
- **手写双侧契约**：Python 这一份是真源，前端 ``web/src/protocol.ts`` 由人手抄
  保持字段一一对应；漂移由 round-trip 单测 + 双侧 review 兜底，不引 codegen。
- **不带协议版本**：v0.1.5 不在帧或握手里写 ``protocol_version``；将来真要做
  破坏性升级，从 HTTP 握手帧或 query 参数加版本协商，再 bump 协议小版本。

帧 contract 速查表
-------------------

C2S（浏览器 → 后端）：

- ``user.input`` —— 用户提交一轮输入；含 ``text`` / ``request_id``
- ``approval.ack`` —— 浏览器对审批弹窗的应答；含 ``call_id`` / ``approved``
- ``ping`` —— 心跳；后端以 ``pong`` 回应

S2C（后端 → 浏览器，每帧均含 ``timestamp_ms``）：

- ``thread.history`` —— 建连后下发完整历史；含 ``messages: list[HistoryMessageDTO]``
- ``turn.start`` / ``turn.end`` —— 一轮开始 / 结束标记；含 ``turn``
- ``content.delta`` / ``reasoning.delta`` —— 流式增量；含 ``delta`` / ``turn`` / ``seq``
- ``assistant.final`` —— 一轮 assistant 输出收尾；含 ``content`` / ``turn``
- ``tool.call.start`` / ``tool.call.end`` —— 工具执行开始 / 结束；后者含 ``ok``
- ``approval.request`` —— 工具执行前请求人工审批；含 ``call_id`` / ``arguments``
- ``approval.decision`` —— 审批结局通知（approved / rejected / cancelled）
- ``usage`` —— 一轮 token 用量回报
- ``error`` —— 错误事件（5 种 error_code 枚举）
- ``pong`` —— 心跳应答
- ``cell.evicted`` —— thread cell 被回收（4 种 reason 枚举）

REST DTO：

- ``ThreadMetadataDTO`` / ``CreateThreadRequest`` / ``RenameThreadRequest`` —— thread CRUD
- ``LLMPresetDTO`` —— GET /api/presets 返回元素（脱敏，不含 api_key）
- ``LoginRequest`` —— POST /api/auth/login body
- ``CellSummaryDTO`` —— GET /api/manage/cells 返回元素
- ``ErrorResponseDTO`` —— REST 端通用错误响应
- ``HistoryMessageDTO`` —— 复用为 ``thread.history`` 帧的 messages 数组元素

公共枚举：

- ``ErrorCode`` —— ``error`` 帧的 5 类错误（network / llm_error / tool_error / approval_timeout / internal）
- ``EvictReason`` —— ``cell.evicted`` 帧的 4 类原因（idle / manual_stop / server_shutdown / error）
- ``ApprovalOutcome`` —— ``approval.decision`` 的 3 类结局（approved / rejected / cancelled）

具体子模块：

- :mod:`web.protocol.ws_frames` —— 17 个 WS 帧 + 2 个 union（WSFrameC2S / WSFrameS2C）
  + 2 个 TypeAdapter（WSFrameC2SAdapter / WSFrameS2CAdapter）
- :mod:`web.protocol.rest_models` —— 8 个 REST DTO（含 HistoryMessageDTO）
"""

from __future__ import annotations

from web.protocol._base import (
    ApprovalOutcome,
    ErrorCode,
    EvictReason,
    HistoryMessageRole,
)
from web.protocol.rest_models import (
    ActiveCellStatusDTO,
    CellSummaryDTO,
    CreateThreadRequest,
    CreateWhiteboardCardRequest,
    ErrorResponseDTO,
    EvolutionDecisionItemDTO,
    EvolutionDecisionRequest,
    EvolutionDecisionResponse,
    EvolutionDecisionSummaryDTO,
    EvolutionNutrientDTO,
    EvolutionReviewDTO,
    HistoryMessageDTO,
    ImportClaudeSessionRequest,
    ImportClaudeSessionResponse,
    ImportCodexSessionRequest,
    ImportCodexSessionResponse,
    LLMPresetDTO,
    LoginRequest,
    RenameThreadRequest,
    ResetPasswordRequest,
    RuntimeStatusGlobalWSDTO,
    RuntimeStatusPollingDTO,
    RuntimeStatusProcessDTO,
    RuntimeStatusProviderSessionsDTO,
    RuntimeStatusSnapshotDTO,
    ThreadMetadataDTO,
    UpdateWhiteboardCardRequest,
    UpdateWhiteboardLayoutRequest,
    UpdateWorkspaceFileRequest,
    UserInputAttachment,
    WhiteboardCardDTO,
    WhiteboardCardLayoutDTO,
    WhiteboardDTO,
    WorkspaceContextDTO,
    WorkspaceFileDTO,
    WorkspaceGitActionResultDTO,
    WorkspaceGitBranchesDTO,
    WorkspaceGitCheckoutRequest,
    WorkspaceGitCommitDTO,
    WorkspaceGitCommitRequest,
    WorkspaceGitCommitsDTO,
    WorkspaceGitCreateBranchRequest,
    WorkspaceGitFileDiffDTO,
    WorkspaceGitPathsRequest,
    WorkspaceGitStatusDTO,
    WorkspaceGitStatusEntryDTO,
    WorkspaceTreeDTO,
    WorkspaceTreeNodeDTO,
)
from web.protocol.ws_frames import (
    ApprovalAckFrame,
    ApprovalDecisionFrame,
    ApprovalRequestFrame,
    AssistantFinalFrame,
    CellEvictedFrame,
    ContentDeltaFrame,
    ErrorFrame,
    InterruptFrame,
    PingFrame,
    PongFrame,
    ReasoningDeltaFrame,
    RunInterruptedFrame,
    SystemNoticeFrame,
    ThreadHistoryFrame,
    ToolCallEndFrame,
    ToolCallStartFrame,
    TurnEndFrame,
    TurnStartFrame,
    UsageFrame,
    UserInputFrame,
    WSFrameC2S,
    WSFrameC2SAdapter,
    WSFrameS2C,
    WSFrameS2CAdapter,
)

__all__: list[str] = [
    # 公共枚举
    "ApprovalOutcome",
    "ErrorCode",
    "EvictReason",
    "HistoryMessageRole",
    # WS 帧（C2S）
    "ApprovalAckFrame",
    "InterruptFrame",
    "PingFrame",
    "UserInputFrame",
    # WS 帧（S2C）
    "ApprovalDecisionFrame",
    "ApprovalRequestFrame",
    "AssistantFinalFrame",
    "CellEvictedFrame",
    "ContentDeltaFrame",
    "ErrorFrame",
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
    # WS union + adapter
    "WSFrameC2S",
    "WSFrameC2SAdapter",
    "WSFrameS2C",
    "WSFrameS2CAdapter",
    # REST DTO
    "ActiveCellStatusDTO",
    "CellSummaryDTO",
    "CreateThreadRequest",
    "CreateWhiteboardCardRequest",
    "ErrorResponseDTO",
    "EvolutionDecisionItemDTO",
    "EvolutionDecisionRequest",
    "EvolutionDecisionResponse",
    "EvolutionDecisionSummaryDTO",
    "EvolutionNutrientDTO",
    "EvolutionReviewDTO",
    "HistoryMessageDTO",
    "ImportClaudeSessionRequest",
    "ImportClaudeSessionResponse",
    "ImportCodexSessionRequest",
    "ImportCodexSessionResponse",
    "LLMPresetDTO",
    "LoginRequest",
    "RenameThreadRequest",
    "ResetPasswordRequest",
    "RuntimeStatusGlobalWSDTO",
    "RuntimeStatusPollingDTO",
    "RuntimeStatusProcessDTO",
    "RuntimeStatusProviderSessionsDTO",
    "RuntimeStatusSnapshotDTO",
    "ThreadMetadataDTO",
    "UpdateWorkspaceFileRequest",
    "UpdateWhiteboardCardRequest",
    "UpdateWhiteboardLayoutRequest",
    "UserInputAttachment",
    "WorkspaceGitActionResultDTO",
    "WorkspaceGitBranchesDTO",
    "WorkspaceGitCheckoutRequest",
    "WorkspaceGitCommitDTO",
    "WorkspaceGitCommitsDTO",
    "WorkspaceGitCommitRequest",
    "WorkspaceGitCreateBranchRequest",
    "WorkspaceFileDTO",
    "WorkspaceGitFileDiffDTO",
    "WorkspaceGitPathsRequest",
    "WorkspaceGitStatusDTO",
    "WorkspaceGitStatusEntryDTO",
    "WorkspaceContextDTO",
    "WorkspaceTreeDTO",
    "WorkspaceTreeNodeDTO",
    "WhiteboardCardDTO",
    "WhiteboardCardLayoutDTO",
    "WhiteboardDTO",
]
