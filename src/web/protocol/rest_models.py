"""REST API 请求 / 响应 DTO（v0.1.5 web 宿主壳）。

本文件定义 8 个 Pydantic v2 DTO，覆盖 web 宿主的 REST 端面：

- :class:`HistoryMessageDTO`：单条历史消息。本文件**首先**定义，因为
  :class:`web.protocol.ws_frames.ThreadHistoryFrame` 的 ``messages`` 字段
  会复用它（非典型的 REST 用法，但避免协议层引入第三个文件）。
- :class:`ThreadMetadataDTO`：thread 元数据，落 ``.kongming/web/threads/{id}/metadata.json``
  的形态，也是 ``GET /api/threads/{id}`` 的响应体。
- :class:`CreateThreadRequest` / :class:`RenameThreadRequest`：thread CRUD 请求体。
- :class:`LLMPresetDTO`：``GET /api/presets`` 返回元素，**不**含 api_key（脱敏）。
- :class:`LoginRequest`：``POST /api/auth/login`` 请求体。
- :class:`CellSummaryDTO`：``GET /api/manage/cells`` 返回元素，管理页用。
- :class:`ErrorResponseDTO`：REST 端通用错误响应（与 WS ``error`` 帧不同——
  WS 帧带 ``timestamp_ms``，REST 错误是请求-响应一对一，无需时序戳）。
- 白板 DTO：workspace 级 ``GET /api/whiteboard`` 快照与 card/layout 更新请求体。

所有 DTO 继承 :class:`web.protocol._base._FrameBase`（``frozen=True``、
``extra='forbid'``），从而：

- 一经构造不可变，避免下游误改后影响审计 / 持久化语义。
- 未知字段直接拒绝，让前后端协议漂移在 round-trip 测试里立刻爆出。

本文件**不**定义 WS 帧——那是 :mod:`web.protocol.ws_frames` 的职责。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from web.protocol._base import (
    ErrorCode,
    HistoryMessageRole,
    _FrameBase,
)


class HistoryMessageDTO(_FrameBase):
    """单条历史消息 DTO（user / assistant / tool 三类）。

    既用于 ``GET /api/threads/{id}/history`` REST 响应，也作为
    :class:`web.protocol.ws_frames.ThreadHistoryFrame` 的 ``messages`` 元素。

    ``tool_call_id`` / ``tool_name`` / ``ok`` / ``data`` / ``error_message``
    仅 ``role == "tool"`` 时有意义，其它角色应为 ``None``。

    v0.1.6 加 ``tool_name`` / ``ok`` / ``data`` / ``error_message`` 字段：
    之前 DTO 只透传 role/content/turn/tool_call_id，前端从历史重建 tool 卡片时
    没法显示 toolName / 结果状态 / 结构化数据，刷新页面后用户看不到任何
    工具产出。Message 内部 ``name`` 字段对应 tool_name，``metadata`` dict
    含 ok / data / error_message（见 ``runner._build_tool_result_message``），
    本次把这些信息暴露到 DTO。``arguments`` 仍在 assistant 消息的 tool_calls
    里，需要跨消息查找，留待后续。
    """

    role: HistoryMessageRole
    content: str
    turn: int
    timestamp_ms: int
    tool_call_id: str | None = None
    tool_name: str | None = None
    ok: bool | None = None
    data: dict[str, Any] | None = None
    error_message: str | None = None


class CellSummaryDTO(_FrameBase):
    """管理页单个 cell 的快照（``GET /api/manage/cells`` 返回元素）。"""

    thread_id: Annotated[str, Field(pattern=r"^thread-[a-f0-9]{12}$")]
    thread_name: str
    preset_id: str
    created_at: float
    last_active_at: float
    current_turn: int | None = None
    pending_approval_count: Annotated[int, Field(ge=0)]
    status: Literal["idle", "running", "awaiting_approval"]


class CreateThreadRequest(_FrameBase):
    """创建 thread 请求体（``POST /api/threads``）。

    v0.1.6 加 ``backend_kind`` 字段，并把 ``preset_id`` 改成可选（默认 ``""``）：
    ``backend_kind="claude_code"`` / ``"codex"`` 路径不需要 preset，此时 preset_id 留空字符串占位。
    路由层在校验 ``backend_kind="generic_chat"`` 时强制 ``preset_id`` 非空。
    ``cwd`` 是可选 workspace 根目录；传入时要求绝对路径。
    ``name`` 可选，默认空串；为空时后端用 thread_id 兜底。
    """

    name: Annotated[str, Field(max_length=200)] = ""
    preset_id: str = ""
    backend_kind: Literal["generic_chat", "claude_code", "codex"] = "generic_chat"
    cwd: str = ""


class ErrorResponseDTO(_FrameBase):
    """REST 通用错误响应。

    与 WS ``error`` 帧的差异：REST 是请求-响应一对一，无需 ``timestamp_ms``；
    ``error_code`` 复用同一枚举集合便于前端统一文案表。
    """

    error_code: ErrorCode
    message: str
    details: dict[str, Any] | None = None


class LLMPresetDTO(_FrameBase):
    """LLM preset 摘要（``GET /api/presets`` 返回元素）。

    ``api_key`` 字段**故意省略**——preset 持久化文件里有 api_key，但 REST 出口
    必须脱敏，前端只通过 ``requires_api_key`` 知道该 preset 是否需要鉴权。
    ``base_url_summary`` 是 host 部分的精简展示（如 ``api.openai.com``），
    避免完整 URL（含路径 / query）暴露内部部署细节。
    """

    id: str
    display_name: str
    model: str
    base_url_summary: str
    requires_api_key: bool


class LoginRequest(_FrameBase):
    """登录请求体（``POST /api/auth/login``）。"""

    password: Annotated[str, Field(min_length=1)]


class ResetPasswordRequest(_FrameBase):
    """重置密码请求体（``POST /api/auth/reset-password``）。"""

    new_password: Annotated[str, Field(min_length=1)]


class RenameThreadRequest(_FrameBase):
    """重命名 thread 请求体（``PATCH /api/threads/{id}``）。"""

    name: Annotated[str, Field(max_length=200)]


class ThreadMetadataDTO(_FrameBase):
    """Thread 元数据，落盘形态 + ``GET /api/threads/{id}`` 响应体。

    v0.1.6 加 ``backend_kind`` 字段并把 ``schema_version`` 升至 ``2``。
    v0.2 加 ``claude_thread_id`` + ``cwd`` 字段，``schema_version`` 升至 ``3``。
    v0.2.1 加 thread 级累计 usage 字段，``schema_version`` 升至 ``4``
    v0.2.2 加 ``codex_thread_id`` 并允许 ``backend_kind="codex"``，
    ``schema_version`` 升至 ``5``。
    v0.2.3 将旧 ``sdk_session_id`` 改名为 ``claude_thread_id``，
    ``schema_version`` 升至 ``6``。
    （同时接受老 v1/v2 文件，懒升级在 :func:`web.thread_metadata.read_thread_metadata`
    里完成；DTO 这里只负责出/入参形态对齐 ThreadMetadata 模型）。
    ``id`` 严格匹配 ``^thread-[a-f0-9]{12}$``，防止用户在 URL 里手写 thread id
    时绕过命名约束。
    """

    id: Annotated[str, Field(pattern=r"^thread-[a-f0-9]{12}$")]
    name: Annotated[str, Field(max_length=200)]
    preset_id: str
    backend_kind: Literal["generic_chat", "claude_code", "codex"] = "generic_chat"
    claude_thread_id: str = ""
    codex_thread_id: str = ""
    cwd: str = ""
    created_at: float
    updated_at: float
    message_count: Annotated[int, Field(ge=0)]
    cumulative_prompt_tokens: Annotated[int, Field(ge=0)] = 0
    cumulative_completion_tokens: Annotated[int, Field(ge=0)] = 0
    cumulative_total_tokens: Annotated[int, Field(ge=0)] = 0
    cumulative_cache_read_tokens: int | None = None
    cumulative_cache_creation_tokens: int | None = None
    schema_version: Literal[1, 2, 3, 4, 5, 6] = 6


class WorkspaceContextDTO(_FrameBase):
    """当前 thread 的共享 workspace 上下文（workspace-shell 第一波）。

    这是 `Files` / `Shell` 面板共用的上游状态：

    - `workspace_root` 直接来自 thread metadata 的 `cwd`
    - `files_available` 表示当前 thread 已绑定有效 workspace
    - `shell_available` 表示当前 thread 已具备可进入的 workspace cwd
    - `shell_provider` 首版支持 `claude_code | system_shell | none`
    """

    thread_id: Annotated[str, Field(pattern=r"^thread-[a-f0-9]{12}$")]
    backend_kind: Literal["generic_chat", "claude_code", "codex"] = "generic_chat"
    workspace_root: str = ""
    claude_thread_id: str = ""
    shell_provider: Literal["claude_code", "system_shell", "none"] = "none"
    files_available: bool = False
    shell_available: bool = False
    unavailable_reason: str | None = None


class WorkspaceTreeNodeDTO(_FrameBase):
    """workspace 文件树节点。"""

    path: str
    name: str
    kind: Literal["file", "dir"]
    has_children: bool = False


class WorkspaceTreeDTO(_FrameBase):
    """单个目录层级的文件树响应。"""

    path: str = ""
    entries: list[WorkspaceTreeNodeDTO]


class WorkspaceFileDTO(_FrameBase):
    """workspace 文本文件读取结果。"""

    path: str
    name: str
    content: str
    size_bytes: Annotated[int, Field(ge=0)]
    is_text: bool = True
    too_large: bool = False
    encoding: str = "utf-8"


class WorkspaceGitStatusEntryDTO(_FrameBase):
    """workspace git 改动条目。"""

    path: str
    name: str
    staged_status: str
    unstaged_status: str
    previous_path: str | None = None


class WorkspaceGitStatusDTO(_FrameBase):
    """workspace git 状态快照。"""

    workspace_root: str
    repo_root: str
    current_branch: str
    tracking_branch: str | None = None
    ahead_count: Annotated[int, Field(ge=0)] = 0
    behind_count: Annotated[int, Field(ge=0)] = 0
    changes: list[WorkspaceGitStatusEntryDTO]


class WorkspaceGitBranchesDTO(_FrameBase):
    """workspace git 分支列表。"""

    current_branch: str
    local_branches: list[str]
    remote_branches: list[str]


class WorkspaceGitCommitDTO(_FrameBase):
    """单条 git 提交摘要。"""

    commit: str
    short_commit: str
    author: str
    authored_at: str
    subject: str


class WorkspaceGitCommitsDTO(_FrameBase):
    """最近 git 提交列表。"""

    commits: list[WorkspaceGitCommitDTO]


class WorkspaceGitFileDiffDTO(_FrameBase):
    """单文件 diff 结果。"""

    path: str
    diff: str


class WorkspaceGitPathsRequest(_FrameBase):
    """按路径批量执行 git 动作。"""

    paths: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)


class WorkspaceGitCheckoutRequest(_FrameBase):
    """切换现有分支请求体。"""

    branch: Annotated[str, Field(min_length=1, max_length=255)]


class WorkspaceGitCreateBranchRequest(_FrameBase):
    """创建分支请求体。"""

    branch: Annotated[str, Field(min_length=1, max_length=255)]
    checkout: bool = True


class WorkspaceGitCommitRequest(_FrameBase):
    """git commit 请求体。"""

    message: Annotated[str, Field(min_length=1, max_length=4000)]


class WorkspaceGitActionResultDTO(_FrameBase):
    """git 写操作通用响应。"""

    detail: str
    current_branch: str | None = None
    commit: str | None = None
    short_commit: str | None = None


class UpdateWorkspaceFileRequest(_FrameBase):
    """保存 workspace 文本文件请求体。"""

    path: Annotated[str, Field(min_length=1)]
    content: str


class EvolutionDecisionSummaryDTO(_FrameBase):
    total: Annotated[int, Field(ge=0)]
    accepted_memory: Annotated[int, Field(ge=0)]
    accepted_skill: Annotated[int, Field(ge=0)]
    ignored: Annotated[int, Field(ge=0)]
    pending: Annotated[int, Field(ge=0)]


class EvolutionDecisionItemDTO(_FrameBase):
    nutrient_id: Annotated[str, Field(min_length=1)]
    decision: Literal["accept_memory", "accept_skill", "ignore"]
    target: Literal["memory", "skill"] | None = None
    decided_at_ms: Annotated[int, Field(ge=0)] = 0
    applied_status: Literal["pending", "written", "skipped", "failed"] | None = None
    applied_path: str | None = None
    applied_mode: Literal["append", "update", "create", "ignore"] | None = None
    applied_at_ms: Annotated[int, Field(ge=0)] | None = None
    applied_error: str | None = None


class EvolutionNutrientDTO(_FrameBase):
    nutrient_id: Annotated[str, Field(min_length=1)]
    kind: Literal["memory", "workflow", "error"]
    title: Annotated[str, Field(min_length=1)]
    content: Annotated[str, Field(min_length=1)]
    summary: Annotated[str, Field(min_length=1)]
    confidence: float
    evidence_turns: list[int]
    source_run_id: Annotated[str, Field(min_length=1)]
    source_session_id: Annotated[str, Field(min_length=1)]
    suggested_target: Literal["memory", "skill", "errorbook"] | None = None
    tags: list[str] = Field(default_factory=list)


class EvolutionReviewDTO(_FrameBase):
    review_id: Annotated[str, Field(min_length=1)]
    run_id: Annotated[str, Field(min_length=1)]
    session_id: Annotated[str, Field(min_length=1)]
    reviewed_at_ms: Annotated[int, Field(ge=0)]
    review_summary: Annotated[str, Field(min_length=1)]
    nutrients: list[EvolutionNutrientDTO]
    decision_summary: EvolutionDecisionSummaryDTO
    decisions: list[EvolutionDecisionItemDTO] = Field(default_factory=list)


class EvolutionDecisionRequest(_FrameBase):
    nutrient_id: Annotated[str, Field(min_length=1)]
    decision: Literal["accept_memory", "accept_skill", "ignore"]


class EvolutionDecisionResponse(_FrameBase):
    review: EvolutionReviewDTO


class ImportClaudeSessionRequest(_FrameBase):
    """``POST /api/threads/import-claude-session`` 请求体（v0.2.0 dev #8）。

    把已有 Claude Agent SDK 的 jsonl session 导入为 kongming thread：

    - ``claude_thread_id``：SDK session UUID（jsonl 文件名，去 ``.jsonl``）。
    - ``cwd``：SDK 工作目录绝对路径，用于定位 ``~/.claude/projects/<encoded-cwd>/<id>.jsonl``。
    - ``name``：新 thread 名（前端用 jsonl 第 1 条 user message 前 40 字带过来）。

    校验细节：``cwd`` 必须以 ``/`` 开头（绝对路径）；``claude_thread_id`` / ``name``
    长度上限与 :class:`ThreadMetadataDTO` 保持一致。
    """

    claude_thread_id: Annotated[str, Field(min_length=1, max_length=100)]
    cwd: Annotated[str, Field(pattern=r"^/")]
    name: Annotated[str, Field(min_length=1, max_length=200)]


class ImportClaudeSessionResponse(_FrameBase):
    """``POST /api/threads/import-claude-session`` 响应体（v0.2.0 dev #8）。

    ``imported``：

    - ``True`` 表示新建了 thread 并完成 ``bind_claude_thread``。
    - ``False`` 表示 ``claude_thread_id`` 已经被绑过（防重复，返回原 thread）。
    """

    thread: ThreadMetadataDTO
    imported: bool


class ImportCodexSessionRequest(_FrameBase):
    """``POST /api/threads/import-codex-session`` 请求体。

    把已有 Codex CLI session 导入为 kongming thread：

    - ``codex_thread_id``：Codex CLI 的 UUIDv7 thread id。
    - ``cwd``：工作目录绝对路径（来自 rollout session_meta.payload.cwd）。
    - ``name``：新 thread 名。
    """

    codex_thread_id: Annotated[str, Field(min_length=1, max_length=100)]
    cwd: Annotated[str, Field(pattern=r"^/")]
    name: Annotated[str, Field(min_length=1, max_length=200)]


class ImportCodexSessionResponse(_FrameBase):
    """``POST /api/threads/import-codex-session`` 响应体。"""

    thread: ThreadMetadataDTO
    imported: bool


class WhiteboardCardDTO(_FrameBase):
    """白板卡片完整 DTO。"""

    id: Annotated[str, Field(pattern=r"^card-[a-f0-9]{12}$")]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    category: Annotated[str, Field(max_length=100)] = ""
    x: Annotated[int, Field(ge=0)]
    y: Annotated[int, Field(ge=0)]
    height: Annotated[int, Field(ge=120, le=4000)]
    collapsed: bool
    z_index: Annotated[int, Field(ge=0)]
    filename: Annotated[
        str,
        Field(pattern=r"^[a-z0-9][a-z0-9-]{0,120}-[a-f0-9]{12}\.md$"),
    ]
    content: str
    updated_at: float


class WhiteboardDTO(_FrameBase):
    """Workspace 级白板聚合快照。"""

    title: Annotated[str, Field(min_length=1, max_length=200)]
    cards: list[WhiteboardCardDTO] = Field(default_factory=list)
    schema_version: Literal[1] = 1


class CreateWhiteboardCardRequest(_FrameBase):
    """创建白板卡片请求体。"""

    title: Annotated[str, Field(min_length=1, max_length=200)] = "Untitled"
    category: Annotated[str, Field(max_length=100)] = ""
    content: str = ""
    x: Annotated[int, Field(ge=0)] = 24
    y: Annotated[int, Field(ge=0)] = 24
    height: Annotated[int, Field(ge=120, le=4000)] = 280
    collapsed: bool = False


class UpdateWhiteboardCardRequest(_FrameBase):
    """更新白板卡片正文与基础属性。"""

    content: str
    expected_updated_at: float | None = None
    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    category: Annotated[str, Field(max_length=100)] | None = None


class WhiteboardCardLayoutDTO(_FrameBase):
    """单张卡片的布局更新 DTO。"""

    id: Annotated[str, Field(pattern=r"^card-[a-f0-9]{12}$")]
    x: Annotated[int, Field(ge=0)]
    y: Annotated[int, Field(ge=0)]
    height: Annotated[int, Field(ge=120, le=4000)]
    collapsed: bool
    z_index: Annotated[int, Field(ge=0)]


class UpdateWhiteboardLayoutRequest(_FrameBase):
    """更新白板布局请求体。"""

    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    cards: list[WhiteboardCardLayoutDTO] = Field(default_factory=list)


__all__: list[str] = [
    "CellSummaryDTO",
    "CreateThreadRequest",
    "CreateWhiteboardCardRequest",
    "ErrorResponseDTO",
    "HistoryMessageDTO",
    "ImportClaudeSessionRequest",
    "ImportClaudeSessionResponse",
    "LLMPresetDTO",
    "LoginRequest",
    "RenameThreadRequest",
    "ThreadMetadataDTO",
    "UpdateWhiteboardCardRequest",
    "UpdateWhiteboardLayoutRequest",
    "UpdateWorkspaceFileRequest",
    "WhiteboardCardDTO",
    "WhiteboardCardLayoutDTO",
    "WhiteboardDTO",
    "WorkspaceContextDTO",
    "WorkspaceFileDTO",
    "WorkspaceGitActionResultDTO",
    "WorkspaceGitCheckoutRequest",
    "WorkspaceGitCommitRequest",
    "WorkspaceGitCreateBranchRequest",
    "WorkspaceGitPathsRequest",
    "WorkspaceTreeDTO",
    "WorkspaceTreeNodeDTO",
]
