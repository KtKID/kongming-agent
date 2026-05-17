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


class UserInputAttachment(_FrameBase):
    """用户上传附件 ref（asset_id + 元数据），与 TS 侧
    ``web/src/protocol.ts::UserInputAttachment`` interface 一一对齐。

    本 DTO 是协议层共享形状：

    - C2S WS 帧 :class:`web.protocol.ws_frames.UserInputFrame` 的
      ``attachments`` 字段透传一组该 DTO，把用户在 Composer 粘贴的图片
      引用传给后端。
    - REST :class:`HistoryMessageDTO` 的 ``attachments`` 字段在历史回放时
      回传同一 DTO 形状，让浏览器刷新后能继续显示缩略图。

    字段语义：

    - ``asset_id``：上传成功后 ``UploadController`` 分配的唯一 ID，可通过
      ``GET /api/uploads/{asset_id}`` 取回原始资源。
    - ``kind``：资源类型；当前 Phase 1 仅处理 ``"image"``，``"video"`` /
      ``"file"`` 已在 union 中预留，后续 Phase 2 接同一上传/组装链路时
      无需 schema 破坏性升级。
    - ``mime_type``：MIME 类型，前端白名单与后端校验复用同一字符串
      （Phase 1 白名单：``image/png`` / ``image/jpeg`` / ``image/webp``
      / ``image/gif``）。
    - ``size_bytes``：字节数；前端按硬限制（单图 ≤ 5MB）阻止上传。
    - ``width`` / ``height``：图片像素尺寸；视频 / 文件类型可为 ``None``。
    - ``duration_ms``：视频时长（毫秒），图片 / 文件为 ``None``，给未来
      ``"video"`` kind 留位。
    - ``preview_url``：前端缩略图 URL；通常指向 ``GET /api/uploads/...``
      或 data: URL（小图可内联），浏览器历史回放时直接用。
    - ``status``：上传状态机三态；``"ready"`` 表示落盘完成且可纳入下一轮
      请求，``"processing"`` 给视频转码等异步链路预留，``"failed"`` 让
      前端能保留占位并提示重试。
    """

    asset_id: str
    kind: Literal["image", "video", "file"]
    mime_type: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    preview_url: str
    status: Literal["ready", "processing", "failed"]


class HistoryMessageDTO(_FrameBase):
    """单条历史消息 DTO（user / assistant / tool 三类）。

    既用于 ``GET /api/threads/{id}/history`` REST 响应,也作为
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

    claude-image-paste-e2e v0.1（contract layer）加 ``attachments`` 字段：
    历史回放时用户消息携带的图片附件 ref 列表，与 user.input WS 帧的
    ``attachments`` 共享 :class:`UserInputAttachment` DTO，让浏览器刷新后
    缩略图能从 ``preview_url`` 直接恢复。``None`` 表示该消息没有附件
    （绝大多数老消息 / 非 user 消息）。
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
    attachments: list[UserInputAttachment] | None = None


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
    """更新 thread 属性请求体（``PATCH /api/threads/{id}``）。

    支持重命名（name）和/或切换置顶（is_pinned）和/或归档（is_archived），
    至少传一个。
    """

    name: Annotated[str, Field(max_length=200)] | None = None
    is_pinned: bool | None = None
    is_archived: bool | None = None


class ThreadMetadataDTO(_FrameBase):
    """Thread 元数据，``GET /api/threads/{id}`` 响应体。

    schema 演进史：
    - v2 (v0.1.6)：加 ``backend_kind``
    - v3 (v0.2)：加 ``claude_thread_id`` / ``cwd``
    - v4 (v0.2.1)：加 thread 级累计 usage 字段（已在 v8 删除）
    - v5 (v0.2.2)：加 ``codex_thread_id`` + ``backend_kind="codex"``
    - v6 (v0.2.3)：``sdk_session_id`` → ``claude_thread_id``
    - v7 (v0.2.4)：加 ``is_pinned``
    - **v8 (usage-token task#3.3)**：删 5 个 ``cumulative_*_tokens`` 平铺字段；
      改为嵌套 ``usage_summary: dict`` 字段（语义跟 ``ThreadUsageSummary`` 一致）

    ``usage_summary`` 字段说明：

    ``web.protocol`` 不允许 import ``web.usage_token`` 内部类型
    （Contract 5 / web-protocol-no-deps），所以 DTO 这一层用透明 ``dict`` 透传，
    前端 ``protocol.ts`` 用 strict ``ThreadUsageSummary`` interface 描述结构。
    格式示例（Anthropic 系）::

        {
          "channel": "anthropic",
          "cumulative_input_tokens": 100000,
          "cumulative_output_tokens": 3000,
          "extras": {
            "cache_read_input_tokens": 80000,
            "cache_creation_input_tokens": 4000
          },
          "last_run_context_usage": 184000,
          "model_name": "claude-opus-4",
          "model_context_window": 1000000,
          "context_usage_pct": 18.4
        }

    OpenAI 系 ``channel="openai"``，``extras`` 含 ``cached_input_tokens`` /
    ``reasoning_output_tokens``。``None`` 表示 thread 还没跑过任何 turn。

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
    # v9 (usage-token-v2-bigbang)：删 ``usage_summary`` 字段。
    # token 数据通过独立端点 ``GET /threads/<tid>/usage`` 拿 v2 manager
    # ``get_thread_usage`` 派生结果。
    is_pinned: bool = False
    is_archived: bool = False
    schema_version: Literal[1, 2, 3, 4, 5, 6, 7, 8, 9, 10] = 10


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


class ProjectRegistryEntryDTO(_FrameBase):
    """项目登记条目（``GET /api/{claude,codex}/projects`` 中嵌入用，可选）。

    主 list 接口仍返回 ``ProjectSummary`` 形态——本 DTO 是给将来纯 registry list
    端点预留 + 服务端内部 typing 用。

    - ``cwd``：项目工作目录绝对路径。
    - ``alias``：用户给项目起的别名，空串表示未设置。
    - ``added_at``：登记时间戳（Unix 秒，可含小数）。
    """

    cwd: Annotated[str, Field(min_length=1)]
    alias: str = ""
    added_at: float


class AddProjectRequest(_FrameBase):
    """``POST /api/{claude,codex}/projects`` 请求体。

    ``cwd`` 必须以 ``/`` 开头（绝对路径）；后端再做 ``is_dir`` 校验，不存在
    则返 400。``alias`` 可选，缺省空串表示无别名。
    """

    cwd: Annotated[str, Field(min_length=1, pattern=r"^/")]
    alias: str = ""


class ServerInfoResponse(_FrameBase):
    """``GET /api/server/info`` 响应体。

    - ``repo_root``：web server 进程的项目根绝对路径，前端用于 "📍 一键填
      当前 worktree" 按钮。
    - ``schema_version``：响应 schema 版本号，当前 1。
    """

    repo_root: str
    schema_version: int = 1


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
    """白板卡片完整 DTO。

    ``scope`` 区分卡片所属作用域：

    - ``"project"``：项目级，落在当前 cwd 下的 ``.kongming/whiteboard/``。
    - ``"global"``：全局级，落在 ``KONGMING_HOME/whiteboard/``。
    """

    id: Annotated[str, Field(pattern=r"^card-[a-f0-9]{12}$")]
    scope: Literal["project", "global"]
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
    """Workspace 级白板聚合快照（双 scope 合并视图）。

    用 ``global_title`` + ``project_title`` 取代单一 ``title``：

    - ``global_title``：全局白板标题，恒非空。
    - ``project_title``：项目白板标题。``None`` **专用**于「cwd 空」（如纯聊天
      thread），此时前端主按钮 disabled。cwd 非空但 project workspace 尚未创建时
      取默认标题，让用户能点出第一张 project 卡。

    ``cards`` 列表混合包含两个 scope 的卡片，前端按 ``scope`` 字段分别渲染。
    """

    global_title: Annotated[str, Field(min_length=1, max_length=200)]
    project_title: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    cards: list[WhiteboardCardDTO] = Field(default_factory=list)
    schema_version: Literal[1] = 1


class CreateWhiteboardCardRequest(_FrameBase):
    """创建白板卡片请求体。

    ``scope`` 必填，由前端显式指定目标作用域。
    """

    scope: Literal["project", "global"]
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
    """更新白板布局请求体。

    ``scope`` 必填，指定本次布局更新作用在哪个作用域的白板：

    - ``"project"``：更新项目白板的 title / cards 布局。
    - ``"global"``：更新全局白板的 title / cards 布局。
    """

    scope: Literal["project", "global"]
    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    cards: list[WhiteboardCardLayoutDTO] = Field(default_factory=list)


__all__: list[str] = [
    "AddProjectRequest",
    "CellSummaryDTO",
    "CreateThreadRequest",
    "CreateWhiteboardCardRequest",
    "ErrorResponseDTO",
    "HistoryMessageDTO",
    "ImportClaudeSessionRequest",
    "ImportClaudeSessionResponse",
    "LLMPresetDTO",
    "LoginRequest",
    "ProjectRegistryEntryDTO",
    "RenameThreadRequest",
    "ServerInfoResponse",
    "ThreadMetadataDTO",
    "UpdateWhiteboardCardRequest",
    "UpdateWhiteboardLayoutRequest",
    "UpdateWorkspaceFileRequest",
    "UserInputAttachment",
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
