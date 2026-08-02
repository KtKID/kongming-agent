"""REST API 请求 / 响应 DTO（v0.1.5 web 宿主壳）。

本文件定义 Pydantic v2 DTO，覆盖 web 宿主的 REST 端面：

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

from pydantic import Field, field_validator

from hosts.web.app_support.llm_protocol import NormalizedMessage
from hosts.web.app_support.path_utils import is_absolute_workspace_path
from hosts.web.protocol._base import (
    ErrorCode,
    _FrameBase,
)


class CronTaskDTO(_FrameBase):
    """scheduler task 生命周期与运行结果的正交 REST 投影。"""

    task_id: str
    name: str
    lifecycle: Literal["scheduled", "paused", "disabled", "exhausted", "deleted"]
    latest_run_status: (
        Literal[
            "running",
            "completed",
            "silent",
            "failed",
            "inactivity_timeout",
            "abandoned",
            "cancelled",
        ]
        | None
    )
    live_runtime_status: Literal["idle", "running"]
    trigger_type: Literal["once", "interval", "cron", "seconds"]
    trigger_expr: str
    timezone: str
    next_run_at: str | None
    last_run_at: str | None
    thread_id: str
    preset_id: str
    created_by: str
    input_text: str
    agent_name: str


class CronRunDTO(_FrameBase):
    """scheduler 单次 run REST 投影。"""

    run_id: str
    task_id: str
    task_name: str
    session_id: str
    thread_id: str
    scheduled_for: str
    started_at: str | None
    finished_at: str | None
    status: Literal[
        "running",
        "completed",
        "silent",
        "failed",
        "inactivity_timeout",
        "abandoned",
        "cancelled",
    ]
    failure_reason: str | None
    final_message_excerpt: str | None
    delivery_status: Literal["pending", "delivered", "failed", "skipped"]
    delivery_error: str | None


class CronRunMessagesResponse(_FrameBase):
    """单次 scheduler run 的归一化消息列表。"""

    messages: list[NormalizedMessage]


class CronRunsPage(_FrameBase):
    """scheduler 全局 run 分页响应。"""

    runs: list[CronRunDTO]
    next_cursor: str | None


class RunNowResponse(_FrameBase):
    """scheduler 手动试运行受理响应。"""

    run_id: str
    status: Literal["PENDING"]


class CreateCronTaskRequest(_FrameBase):
    """创建 scheduler task 请求。"""

    name: str
    agent_name: str
    input_text: str
    schedule_type: Literal["once", "cron"]
    once_at: str | None = None
    cron_expr: str | None = None
    timezone: str = "UTC"
    concurrency_policy: Literal["forbid", "allow", "replace"] = "forbid"
    preset_id: str | None = None


class UpdateCronTaskRequest(_FrameBase):
    """更新 scheduler task 请求；lifecycle 是任务状态唯一写入口。"""

    name: str | None = None
    schedule: str | None = None
    agent: str | None = None
    input_text: str | None = None
    preset_id: str | None = None
    lifecycle: Literal["scheduled", "paused", "disabled"] | None = None
    concurrency_policy: Literal["forbid", "allow", "replace"] | None = None


class ThreadSubAgentItemDTO(_FrameBase):
    """TaskRegistry child task 的严格 REST 投影。"""

    id: str
    agent_id: Annotated[str, Field(max_length=256)]
    thread_id: str
    source: Annotated[str, Field(max_length=128)]
    workflow_id: Annotated[str, Field(max_length=256)] | None = None
    workflow_task_id: Annotated[str, Field(max_length=256)] | None = None
    task_id: Annotated[str, Field(max_length=256)]
    task_run_id: Annotated[str, Field(max_length=256)]
    task_name: Annotated[str, Field(max_length=512)]
    session_id: Annotated[str, Field(max_length=512)]
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    started_at: str
    updated_at: str
    finished_at: str | None = None
    started_at_ms: Annotated[int, Field(ge=0)]
    updated_at_ms: Annotated[int, Field(ge=0)]
    finished_at_ms: Annotated[int, Field(ge=0)] | None = None
    error_message: Annotated[str, Field(max_length=2000)] | None = None


class ThreadSubAgentListDTO(_FrameBase):
    """GET /api/threads/{thread_id}/subagents 的唯一 wrapper。"""

    schema_version: Literal[1]
    thread_id: str
    subagents: list[ThreadSubAgentItemDTO]


class TaskProgressItemPayload(_FrameBase):
    """当前 foreground workflow 的单个任务进度 REST 投影。"""

    task_id: Annotated[str, Field(min_length=1, max_length=256)]
    task_run_id: Annotated[str, Field(min_length=1, max_length=256)]
    desc: Annotated[str, Field(min_length=1, max_length=1000)]
    depends_on: list[Annotated[str, Field(min_length=1, max_length=256)]]
    status: Literal["pending", "in_progress", "completed", "failed", "cancelled"]
    display_order: Annotated[int, Field(ge=0)]
    error_message: Annotated[str, Field(max_length=2000)] | None = None
    updated_at_ms: Annotated[int, Field(ge=0)]


class TaskProgressCountsPayload(_FrameBase):
    """五态任务计数 REST 投影。"""

    pending: Annotated[int, Field(ge=0)]
    in_progress: Annotated[int, Field(ge=0)]
    completed: Annotated[int, Field(ge=0)]
    failed: Annotated[int, Field(ge=0)]
    cancelled: Annotated[int, Field(ge=0)]
    total: Annotated[int, Field(ge=0)]


class TaskProgressSnapshotPayload(_FrameBase):
    """Thread 当前 foreground task progress 的唯一 REST 响应体。"""

    schema_version: Literal[2]
    session_id: Annotated[str, Field(min_length=1, max_length=256)]
    workflow_id: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    title: Annotated[str, Field(min_length=1, max_length=1000)] | None = None
    control_mode: Literal["llm_steps", "runtime_lifecycle"] | None = None
    updated_at_ms: Annotated[int, Field(ge=0)]
    tasks: list[TaskProgressItemPayload] = Field(max_length=128)
    counts: TaskProgressCountsPayload


class UserInputAttachment(_FrameBase):
    """用户上传附件 ref（asset_id + 元数据），与 TS 侧
    ``web/src/protocol.ts::UserInputAttachment`` interface 一一对齐。

    本 DTO 是协议层共享形状：

    - C2S WS 帧 :class:`web.protocol.ws_frames.UserInputFrame` 的
      ``attachments`` 字段透传一组该 DTO，把用户在 Composer 粘贴的图片
      引用传给后端。
    - C2S WS 帧与 provider route 共享同一 DTO 形状。

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


class PluginToolDTO(_FrameBase):
    """管理页插件工具项。"""

    id: Annotated[str, Field(min_length=1)]
    name: Annotated[str, Field(min_length=1)]
    display_name: Annotated[str, Field(min_length=1)]
    source: Literal["mcp"]
    enabled: bool
    server_id: Annotated[str, Field(min_length=1)]
    mcp_tool_name: Annotated[str, Field(min_length=1)]
    description: str = ""
    canonical_name: str = ""
    is_alias: bool = False


class PluginToolsResponseDTO(_FrameBase):
    """管理页插件列表响应。"""

    plugins: list[PluginToolDTO]


class UpdatePluginToolRequest(_FrameBase):
    """更新单个插件工具 enabled 状态。"""

    enabled: bool


class RuntimeStatusPollingDTO(_FrameBase):
    """dashboard 轮询配置真值。"""

    interval_seconds: Annotated[int, Field(ge=3)]


class RuntimeStatusProcessDTO(_FrameBase):
    """运行中 web 进程摘要。"""

    running: bool
    pid: Annotated[int, Field(ge=1)]
    host: str
    port: Annotated[int, Field(ge=1, le=65535)]
    url: str
    log_path: str


class RuntimeStatusGlobalWSDTO(_FrameBase):
    """全局 WS / 广播订阅计数。"""

    thread_status_connections: Annotated[int, Field(ge=0)]
    cron_connections: Annotated[int, Field(ge=0)]
    approval_subscribers: Annotated[int, Field(ge=0)]


class RuntimeStatusProviderSessionsDTO(_FrameBase):
    """provider 活跃 session 计数。"""

    claude_active_sessions: Annotated[int, Field(ge=0)]
    codex_active_sessions: Annotated[int, Field(ge=0)]


class ActiveCellStatusDTO(_FrameBase):
    """runtime-status 中的活跃 cell 明细。"""

    thread_id: Annotated[str, Field(pattern=r"^thread-[a-f0-9]{12}$")]
    thread_name: str
    backend_kind: Literal["generic_chat", "claude_code", "codex"] = "generic_chat"
    preset_id: str
    cwd: str = ""
    created_at: float
    last_active_at: float
    pending_approval_count: Annotated[int, Field(ge=0)]
    status: Literal["idle", "running", "awaiting_approval"]
    chat_ws_connections: Annotated[int, Field(ge=0)]


class RuntimeStatusSnapshotDTO(_FrameBase):
    """统一 runtime 快照。"""

    process: RuntimeStatusProcessDTO
    polling: RuntimeStatusPollingDTO
    global_ws: RuntimeStatusGlobalWSDTO
    provider_sessions: RuntimeStatusProviderSessionsDTO
    cells_total: Annotated[int, Field(ge=0)]
    chat_ws_connections_total: Annotated[int, Field(ge=0)]
    approval_pending_total: Annotated[int, Field(ge=0)]
    workspace_shell_connections: Annotated[int, Field(ge=0)] | None = None
    cells: list[ActiveCellStatusDTO]
    generated_at_ms: Annotated[int, Field(ge=0)]


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

    @field_validator("cwd")
    @classmethod
    def _validate_cwd(cls, value: str) -> str:
        trimmed = value.strip()
        if trimmed and not is_absolute_workspace_path(trimmed):
            raise ValueError("cwd must be an absolute path")
        return trimmed


class ForkThreadRequest(_FrameBase):
    """从源 Session 的指定 assistant 回复边界创建 thread 分支。"""

    history_index: Annotated[int, Field(ge=0)]


class CreateGenericThreadFromFirstMessageRequest(_FrameBase):
    """通用频道空白页首发创建请求体。

    ``cwd`` 为空时由后端解析为用户 home；非空时要求绝对路径。
    """

    text: Annotated[str, Field(min_length=1)]
    preset_id: Annotated[str, Field(min_length=1)]
    cwd: str = ""
    reasoning_effort: Literal["none", "low", "medium", "high", "max"] | None = None

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("text must not be empty")
        return trimmed

    @field_validator("preset_id")
    @classmethod
    def _validate_preset_id(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("preset_id must not be empty")
        return trimmed

    @field_validator("cwd")
    @classmethod
    def _validate_cwd(cls, value: str) -> str:
        trimmed = value.strip()
        if trimmed and not is_absolute_workspace_path(trimmed):
            raise ValueError("cwd must be an absolute path")
        return trimmed


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


class ProviderCatalogItemDTO(_FrameBase):
    """模型 provider catalog 摘要。"""

    providerId: str
    displayName: str
    regionLabel: str
    description: str
    logoText: str


class ProviderConnectionDTO(_FrameBase):
    """由 catalog credential 引用派生的 provider 连接状态。"""

    providerId: str
    status: Literal["connected", "disconnected", "error"]
    model: str | None
    authLabel: str | None


class ConnectedModelFamilyDTO(_FrameBase):
    """Composer 使用的已连接模型与 reasoning capability 投影。"""

    providerId: str
    providerLabel: str
    familyId: str
    displayName: str
    presetId: str
    model: str
    connected: bool
    supportedReasoningEfforts: list[Literal["none", "low", "medium", "high", "max"]]
    defaultReasoningEffort: Literal["none", "low", "medium", "high", "max"] | None
    reasoningAdapter: str | None
    contextWindowTokens: int | None


class ProviderActionResponseDTO(_FrameBase):
    """provider test/connect/disconnect 的统一响应。"""

    providerId: str
    ok: bool
    message: str
    connection: ProviderConnectionDTO | None = None


class TestProviderRequest(_FrameBase):
    """临时 credential probe 请求。"""

    apiKey: str | None = None


class ConnectProviderRequest(_FrameBase):
    """保存 provider-specific credential 请求。"""

    apiKey: str | None = None


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


class UpdateThreadPresetRequest(_FrameBase):
    """更新 Generic Chat thread 的 LLM preset。

    只服务 ``backend_kind="generic_chat"``。运行中切换时，当前 run 继续使用
    启动时 provider；后续发送按新 ``preset_id`` 装配运行时。
    """

    preset_id: Annotated[str, Field(min_length=1)]


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
    - v12：加 ``forked_from_id``，保存直接父任务
    - v13：加 ``forked_from_history_index``，保存 fork 复制终点在目标历史中的位置

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
    thread_kind: Literal["chat", "scheduled_task"] = "chat"
    source_kind: str = ""
    source_id: str = ""
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
    forked_from_id: Annotated[str, Field(pattern=r"^thread-[a-f0-9]{12}$")] | None = None
    forked_from_history_index: Annotated[int, Field(ge=0)] | None = None
    schema_version: Literal[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13] = 13


class PermissionRuleDTO(_FrameBase):
    """thread permissions 的结构化规则。"""

    expression: Annotated[str, Field(min_length=1)]
    scope_cwd: str | None


class PermissionsMigrationSummaryDTO(_FrameBase):
    """v1→v2 首次读取迁移的可见结果。"""

    from_schema_version: Literal[1]
    to_schema_version: Literal[2]
    invalidated_shell_allow_count: Annotated[int, Field(ge=0)]
    backup_path: str


class ThreadPermissionsDTO(_FrameBase):
    """单个 thread 的 permissions 本子 REST 快照。"""

    schema_version: Literal[2] = 2
    thread_id: str
    revision: Annotated[int, Field(ge=0)]
    allow: list[PermissionRuleDTO]
    deny: list[PermissionRuleDTO]
    updated_at: str | None
    migration_summary: PermissionsMigrationSummaryDTO | None = None


class UpdateThreadPermissionsRequest(_FrameBase):
    """整本替换 thread permissions 的 revision CAS 请求。"""

    thread_id: str
    revision: Annotated[int, Field(ge=0)]
    allow: list[PermissionRuleDTO]
    deny: list[PermissionRuleDTO]


class CreateGenericThreadFromFirstMessageResponse(_FrameBase):
    """通用频道空白页首发创建响应体。"""

    thread: ThreadMetadataDTO


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

    cwd: Annotated[str, Field(min_length=1)]
    alias: str = ""

    @field_validator("cwd")
    @classmethod
    def _validate_cwd(cls, value: str) -> str:
        trimmed = value.strip()
        if not is_absolute_workspace_path(trimmed):
            raise ValueError("cwd must be an absolute path")
        return trimmed


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
    cwd: str
    name: Annotated[str, Field(min_length=1, max_length=200)]

    @field_validator("cwd")
    @classmethod
    def _validate_cwd(cls, value: str) -> str:
        trimmed = value.strip()
        if not is_absolute_workspace_path(trimmed):
            raise ValueError("cwd must be an absolute path")
        return trimmed


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
    cwd: str
    name: Annotated[str, Field(min_length=1, max_length=200)]

    @field_validator("cwd")
    @classmethod
    def _validate_cwd(cls, value: str) -> str:
        trimmed = value.strip()
        if not is_absolute_workspace_path(trimmed):
            raise ValueError("cwd must be an absolute path")
        return trimmed


class ImportCodexSessionResponse(_FrameBase):
    """``POST /api/threads/import-codex-session`` 响应体。"""

    thread: ThreadMetadataDTO
    imported: bool


class WhiteboardCardDTO(_FrameBase):
    """白板卡片完整 DTO。

    ``scope`` 区分卡片所属作用域：

    - ``"project"``：项目级，落在 ``kongming_home/whiteboard/projects/`` 并按 cwd 分区。
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
    "ConnectProviderRequest",
    "ConnectedModelFamilyDTO",
    "CreateCronTaskRequest",
    "CreateGenericThreadFromFirstMessageRequest",
    "CreateGenericThreadFromFirstMessageResponse",
    "CreateThreadRequest",
    "CreateWhiteboardCardRequest",
    "CronRunDTO",
    "CronRunMessagesResponse",
    "CronRunsPage",
    "CronTaskDTO",
    "ErrorResponseDTO",
    "ForkThreadRequest",
    "ImportClaudeSessionRequest",
    "ImportClaudeSessionResponse",
    "LLMPresetDTO",
    "LoginRequest",
    "PermissionRuleDTO",
    "PermissionsMigrationSummaryDTO",
    "ProjectRegistryEntryDTO",
    "ProviderActionResponseDTO",
    "ProviderCatalogItemDTO",
    "ProviderConnectionDTO",
    "RenameThreadRequest",
    "RunNowResponse",
    "ServerInfoResponse",
    "TaskProgressCountsPayload",
    "TaskProgressItemPayload",
    "TaskProgressSnapshotPayload",
    "TestProviderRequest",
    "ThreadMetadataDTO",
    "ThreadPermissionsDTO",
    "ThreadSubAgentItemDTO",
    "ThreadSubAgentListDTO",
    "UpdateCronTaskRequest",
    "UpdateThreadPermissionsRequest",
    "UpdateThreadPresetRequest",
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
