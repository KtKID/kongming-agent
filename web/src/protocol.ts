/**
 * kongming-agent v0.1.5 web 协议层 TypeScript 复刻。
 *
 * ## 这份文件是什么
 *
 * 本文件是 Python 侧 `src/web/protocol/` 三个模块的 TS 1:1 复刻：
 *
 * - Python `src/web/protocol/_base.py`        → 本文件「公共枚举」段
 * - Python `src/web/protocol/rest_models.py`  → 本文件「REST DTO」段
 * - Python `src/web/protocol/ws_frames.py`    → 本文件「C2S 帧」+「S2C 帧」+「Unions」三段（18 帧 + 2 union）
 *
 * 字段名 / 类型 / 必选与 Python 侧严格对应；可选字段（Python `X | None = None`）
 * 用 TS 的 `field?: X` 表达；Pydantic `Literal["x"]` 单值用 TS 字面量类型。
 *
 * ## 维护方式
 *
 * 本文件**由人手维护**——v0.1.5 暂不引入 OpenAPI / msgspec / datamodel-codegen
 * 这类自动生成工具，理由：
 * - 18 帧 + 8 DTO 体量小，手写一次即可，引入 codegen 工具会带来构建链复杂度
 * - 漂移由两道护栏兜底：
 *   1. Python 侧 110 个 round-trip + 拒绝场景单测（保证 Python 模型与 wire 一致）
 *   2. PR 审查时必须同时改 Python + TS，review checklist 强制对照
 *
 * ## 修改约定
 *
 * - 增删字段：必须**同步**改 Python `src/web/protocol/` 与本文件，缺一即视为协议漂移
 * - 字段顺序与 Python 保持一致，便于双侧 diff 对照 review
 * - 公共枚举新增枚举值视为破坏性变更，需 bump 协议小版本（v0.1.6+）
 * - 不要在本文件加 React / 浏览器 / WS 客户端逻辑——这里只放纯类型定义
 *
 * ## TypeScript 设施使用约定
 *
 * - 对象类型用 `interface`（便于扩展 / 工具类型）
 * - union / 字面量别名用 `type`
 * - `frame_type` 字段是 wire 帧 discriminated union 的判别 tag（v0.2 起统一），
 *   外层用 switch / 类型守卫 narrow；REST DTO / render item 的 `kind`/`type`
 *   字段是业务属性（如 `WorkspaceTreeNodeDTO.kind`、`UserInputAttachment.kind`），
 *   不参与 wire 判别
 * - Python `dict[str, Any]` → TS `Record<string, unknown>`（避免 `any` 污染下游）
 * - Python `int` / `float` → TS `number`（TS 没有 int 区分）
 */

// ============================================================================
// ===== 公共枚举（对应 Python src/web/protocol/_base.py）=====
// ============================================================================

/**
 * `error` 帧的错误分类（5 枚举，与 Python `ErrorCode` 一致）。
 *
 * - `network`：连接 LLM endpoint / 工具远端失败（DNS / TCP / TLS 层）
 * - `llm_error`：LLM 端返回 4xx / 5xx 或解析模型响应失败
 * - `tool_error`：工具执行抛异常（不含 ok=false 的业务失败，那个走 tool.call.end）
 * - `approval_timeout`：浏览器在 pending_approval_timeout_seconds 内没回 ack
 * - `internal`：runtime 内部 panic / 不在以上四类的兜底
 */
export type ErrorCode =
  | "network"
  | "llm_error"
  | "tool_error"
  | "approval_timeout"
  | "internal";

/**
 * `cell.evicted` 帧的回收原因（4 枚举，与 Python `EvictReason` 一致）。
 *
 * - `idle`：超过 idle_timeout_seconds 无活动，被 ThreadManager 自动回收
 * - `manual_stop`：用户在管理页点了"停止"
 * - `server_shutdown`：uvicorn 收到 SIGTERM，所有 cell 一并清理
 * - `error`：runtime 内部异常无法继续
 */
export type EvictReason = "idle" | "manual_stop" | "server_shutdown" | "error";

/**
 * `approval.decision` 帧的审批结局三态（与 Python `ApprovalOutcome` 一致）。
 *
 * 与 Python core.contracts.ApprovalOutcome 同名同义。
 */
export type ApprovalOutcome = "approved" | "rejected" | "cancelled";

/**
 * 历史消息角色（与 Python `HistoryMessageRole` 一致）。
 */
export type HistoryMessageRole = "user" | "assistant" | "tool";

/**
 * Thread 后端类型（v0.1.6 新增）。
 *
 * - `generic_chat`：走 InputAssembler + LLM provider 的原有路径，
 *   使用 `/ws/threads/{thread_id}` endpoint
 * - `claude_code`：走 Claude Agent SDK，使用 `/ws/claude-code?thread_id={thread_id}`
 * - `codex`：走 /ws/codex + codex CLI 子进程
 *
 * 默认 `generic_chat`；老 v1 thread 文件读入时由后端自动补 `generic_chat`。
 */
export type BackendKind = "generic_chat" | "claude_code" | "codex";

/**
 * Thread 业务类型。
 *
 * `chat`：用户普通对话。
 * `scheduled_task`：定时任务专属历史 thread。
 */
export type ThreadKind = "chat" | "scheduled_task";

/**
 * `system.notice` 的系统提示状态（由后端语义层直接给出）。
 */
export type SystemNoticeStatus =
  | "started"
  | "completed"
  | "failed"
  | "drain_timeout";

/**
 * `system.notice` 的建议图标语义。
 *
 * 前端卡片直接用这四态映射图标；缺失时可按 `status` 回退。
 */
export type SystemNoticeIcon = "running" | "success" | "warning" | "error";

// ============================================================================
// ===== REST DTO（对应 Python src/web/protocol/rest_models.py）=====
// ============================================================================

/**
 * 用户消息附件引用（图片/视频/文件，Phase 1 只支持 image）。
 * 与 Python `web.protocol.ws_frames.UserInputAttachment` 一一对齐。
 * kind 字段为 union，已为未来 video/file 预留位。
 */
export interface UserInputAttachment {
  asset_id: string;
  kind: "image" | "video" | "file";
  mime_type: string;
  size_bytes: number;
  width?: number;
  height?: number;
  duration_ms?: number;
  preview_url: string;
  status: "ready" | "processing" | "failed";
}

/**
 * 管理页单个 cell 的快照（`GET /api/manage/cells` 返回元素）。
 *
 * `thread_id` 形如 `thread-<12 位 hex>`；
 * `pending_approval_count >= 0`；
 * `status` 取 idle / running / awaiting_approval 之一。
 */
export interface CellSummaryDTO {
  thread_id: string;
  thread_name: string;
  preset_id: string;
  created_at: number;
  last_active_at: number;
  current_turn?: number;
  pending_approval_count: number;
  status: "idle" | "running" | "awaiting_approval";
}

export interface RuntimeStatusPollingDTO {
  interval_seconds: number;
}

export interface RuntimeStatusProcessDTO {
  running: boolean;
  pid: number;
  host: string;
  port: number;
  url: string;
  log_path: string;
}

export interface RuntimeStatusGlobalWSDTO {
  thread_status_connections: number;
  cron_connections: number;
  approval_subscribers: number;
}

export interface RuntimeStatusProviderSessionsDTO {
  claude_active_sessions: number;
  codex_active_sessions: number;
}

export interface ActiveCellStatusDTO {
  thread_id: string;
  thread_name: string;
  backend_kind: BackendKind;
  preset_id: string;
  cwd?: string;
  created_at: number;
  last_active_at: number;
  pending_approval_count: number;
  status: "idle" | "running" | "awaiting_approval";
  chat_ws_connections: number;
}

export interface RuntimeStatusSnapshotDTO {
  process: RuntimeStatusProcessDTO;
  polling: RuntimeStatusPollingDTO;
  global_ws: RuntimeStatusGlobalWSDTO;
  provider_sessions: RuntimeStatusProviderSessionsDTO;
  cells_total: number;
  chat_ws_connections_total: number;
  approval_pending_total: number;
  workspace_shell_connections?: number | null;
  cells: ActiveCellStatusDTO[];
  generated_at_ms: number;
}

/**
 * 创建 thread 请求体（`POST /api/threads`）。
 *
 * `name.length <= 200`（Python 侧 Pydantic 校验，TS 侧不重复校验）。
 *
 * v0.1.6：
 * - `preset_id` 从必填改为可选；`backend_kind="generic_chat"` 时必须传非空字符串，
 *   `backend_kind="claude_code"` 时可省略（后端忽略）
 * - `backend_kind` 默认 `generic_chat`，可省略
 * - `cwd` 可选；传入时必须是绝对路径，用作 workspace 根目录
 */
export interface CreateThreadRequest {
  name: string;
  preset_id?: string;
  backend_kind?: BackendKind;
  cwd?: string;
}

export interface CreateGenericThreadFromFirstMessageRequest {
  text: string;
  preset_id: string;
  cwd?: string;
  reasoning_effort?: "low" | "medium" | "high" | null;
}

export interface CreateGenericThreadFromFirstMessageResponse {
  thread: ThreadMetadataDTO;
}

export interface UpdateThreadPresetRequest {
  preset_id: string;
}

/**
 * REST 通用错误响应。
 *
 * 与 WS `error` 帧的差异：REST 是请求-响应一对一，无 timestamp_ms；
 * `error_code` 复用同一枚举集合便于前端统一文案表。
 */
export interface ErrorResponseDTO {
  error_code: ErrorCode;
  message: string;
  details?: Record<string, unknown>;
}

/**
 * LLM preset 摘要（`GET /api/presets` 返回元素）。
 *
 * `api_key` **故意省略**——preset 持久化文件里有 api_key，但 REST 出口必须
 * 脱敏，前端只通过 `requires_api_key` 知道该 preset 是否需要鉴权。
 * `base_url_summary` 是 host 部分的精简展示（如 `api.openai.com`），避免
 * 完整 URL（含路径 / query）暴露内部部署细节。
 */
export interface LLMPresetDTO {
  id: string;
  display_name: string;
  model: string;
  base_url_summary: string;
  requires_api_key: boolean;
}

/**
 * 登录请求体（`POST /api/auth/login`）。
 *
 * `password.length >= 1`（Python 侧 Pydantic 校验）。
 */
export interface LoginRequest {
  password: string;
}

/**
 * 重置密码请求体（`POST /api/auth/reset-password`）。
 */
export interface ResetPasswordRequest {
  new_password: string;
}

/**
 * 更新 thread 属性请求体（`PATCH /api/threads/{id}`）。
 *
 * 支持重命名（`name`）和/或切换置顶（`is_pinned`）和/或归档（`is_archived`），
 * 至少传一个；多个可同请求一并提交。
 *
 * `name.length <= 200`（Python 侧 Pydantic 校验）。
 */
export interface RenameThreadRequest {
  name?: string;
  is_pinned?: boolean;
  is_archived?: boolean;
}

export type ThreadTaskProgressStatus =
  | "pending"
  | "in_progress"
  | "completed";

export type ThreadTaskProgressSource = "llm" | "workflow" | "api";

export interface ThreadTaskProgressCounts {
  pending: number;
  in_progress: number;
  completed: number;
  total: number;
}

export interface ThreadTaskProgressItem {
  id: string;
  orchestration_task_id: string;
  workflow_id?: string | null;
  task_id: string;
  task_run_id: string;
  desc: string;
  status: ThreadTaskProgressStatus;
  source_status?: string | null;
  error_message?: string | null;
  display_order: number;
  updated_at_ms?: number | null;
}

export interface ThreadTaskProgressSnapshot {
  schema_version: 1;
  session_id: string;
  updated_at_ms: number;
  source: ThreadTaskProgressSource;
  tasks: ThreadTaskProgressItem[];
  counts: ThreadTaskProgressCounts;
}

export type ThreadTaskProgressIconVariant =
  | "check_circle"
  | "ring"
  | "active_ring";

export interface ThreadTaskProgressDisplayItem {
  key: string;
  orchestration_task_id: string;
  task_id: string;
  desc: string;
  status: ThreadTaskProgressStatus;
  status_label: "未完成" | "进行中" | "已完成";
  icon_variant: ThreadTaskProgressIconVariant;
  order: number;
  aria_label: string;
}

export interface ThreadTaskProgressViewModel {
  title: "进度";
  variant: "compact_checklist";
  items: ThreadTaskProgressDisplayItem[];
  empty: { title: string; desc?: string };
}

export type ThreadSubAgentStatus = string;

export interface ThreadSubAgentDTO {
  id?: string | null;
  task_name?: string | null;
  name?: string | null;
  status: ThreadSubAgentStatus;
  source?: string | null;
  workflow_id?: string | null;
  started_at_ms?: number | null;
  updated_at_ms?: number | null;
}

export interface ThreadSubAgentListDTO {
  subagents: ThreadSubAgentDTO[];
}

export type ThreadSubAgentListResponse =
  | ThreadSubAgentDTO[]
  | ThreadSubAgentListDTO;

export type ThreadSubAgentIconVariant =
  | "running"
  | "success"
  | "error"
  | "pending";

export interface ThreadSubAgentDisplayItem {
  key: string;
  name: string;
  status: ThreadSubAgentStatus;
  status_label: string;
  icon_variant: ThreadSubAgentIconVariant;
  is_active: boolean;
  source_label?: string;
  started_at_ms?: number | null;
  updated_at_ms?: number | null;
  aria_label: string;
}

/**
 * Thread 元数据 + `GET /api/threads/{id}` 响应体。
 *
 * `id` 形如 `thread-<12 位 hex>`；
 * `name.length <= 200`；
 * `message_count >= 0`；
 * `schema_version`：当前 v11；老文件由后端读盘时懒升级。
 *
 * v0.1.6 新增 `backend_kind`；老 v1 文件读入默认 `generic_chat`。
 * v0.2 新增 `claude_thread_id` + `cwd`（claude_code thread 与 SDK session 持久化绑定）；
 * 老 v2 文件读入默认空字符串。
 * v0.2.1 新增 thread 级累计 `cumulative_*_tokens`；老 v3 文件读入默认 0。
 * v0.2.2 新增 `codex_thread_id`，`backend_kind` 支持 `codex`。
 * v0.2.3 将旧 `sdk_session_id` 改名为 `claude_thread_id`。
 * v0.2.x（schema v10）新增 `is_archived`，作为归档真源（替代旧 jsonl ``archived``
 * 事件方案）；老 v9 文件懒升级补 `is_archived=false`。
 * scheduled-task-thread（schema v11）新增 `thread_kind/source_kind/source_id`。
 * `preset_id` 在 `backend_kind="claude_code"` 时允许空字符串占位。
 */
export interface ThreadMetadataDTO {
  /** thread 唯一 ID，格式 `thread-{12位hex}`（如 `thread-a1b2c3d4e5f6`），由 ThreadManager 通过 secrets.token_hex(6) 生成 */
  id: string;
  /** 用户给 thread 起的名字，最长 200 字符；通用 tab 用户手填，Claude tab 延迟创建时自动取 project display_name */
  name: string;
  /** 创建时选的 LLM preset ID；generic_chat 必须非空，claude_code/codex 为空字符串（不需要 preset） */
  preset_id: string;
  /**
   * 该 thread 使用哪种后端引擎驱动对话。
   * 类型 BackendKind 当前有三个值：
   * - "generic_chat" — 通用 LLM 对话，走 InputAssembler + LLM provider
   * - "claude_code"  — Claude Agent SDK，走 /ws/claude-code
   * - "codex"        — Codex CLI 子进程，走 /ws/codex
   */
  backend_kind: BackendKind;
  /** Claude 底层 thread/session id；空字符串=未绑定（首次对话前）；绑定后用于定位 ~/.claude/projects/ 下的 .jsonl 历史文件和 resume 对话 */
  claude_thread_id: string;
  /** Codex CLI 的 UUIDv7 thread id；仅 backend_kind="codex" 时有值，其他后端为空字符串 */
  codex_thread_id: string;
  /** workspace 工作目录绝对路径（如 /Volumes/machub_app/proj/kongming-agent）；Files/Git/Shell 面板绑定此目录；空字符串=纯聊天不绑 workspace */
  cwd: string;
  /** 创建时间，Unix 时间戳（秒） */
  created_at: number;
  /** 最近更新时间，Unix 时间戳（秒）；rename / 一轮对话结束时更新 */
  updated_at: number;
  /** 历史消息总数（≥0），UI 上"X 条消息"展示用 */
  message_count: number;
  /** 是否置顶；置顶的 thread 在列表中排在最前面 */
  is_pinned: boolean;
  /** 是否归档；归档的 thread 不在历史列表中显示（claude_code 真源由本字段决定，不再读 jsonl ``archived`` 事件） */
  is_archived: boolean;
  /** 业务类型；缺失时按普通聊天处理。 */
  thread_kind?: ThreadKind;
  /** 业务来源类型；定时任务 thread 使用 scheduled_task。 */
  source_kind?: string;
  /** 业务来源 ID；定时任务 thread 使用 task_id。 */
  source_id?: string;
  /** 元数据 schema 版本号，当前 11（新增 scheduled_task thread 来源字段） */
  schema_version?: 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11;
}

// usage-token-v2-bigbang: token 数据通过独立端点 GET /threads/<tid>/usage 拿，
// 返回 ClaudeUsage / CodexUsage / GenericChat*Usage 之一（自带 provider discriminator）

/**
 * Anthropic prompt cache TTL 细分（cache_creation 子结构）。
 * 后端真源：`web.usage_token_v2._models.ClaudeCacheCreation`。
 */
export interface ClaudeCacheCreation {
  ephemeral_1h_input_tokens: number;
  ephemeral_5m_input_tokens: number;
}

/**
 * Claude 系 token 用量（claude_code / generic_chat-anthropic）。
 * 后端真源：`web.usage_token_v2.ClaudeUsage` / `GenericChatAnthropicUsage`。
 *
 * 取最后一条 SDK assistant message 的 usage，不累加。
 * `context_usage = input + cache_read + cache_creation`（当前 context 占用）。
 */
export interface ClaudeUsage {
  provider: "claude";
  input_tokens: number;
  output_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  cache_creation: ClaudeCacheCreation;
  context_usage: number;
  model: string;
  context_window: number;
}

/** Codex token 用量 5 字段细分（OpenAI 语义）。 */
export interface CodexTokenBreakdown {
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
  total_tokens: number;
}

/** Codex 速率限制单窗口（5h 或 7d）。 */
export interface CodexRateLimitWindow {
  used_percent: number;
  window_minutes: number;
  resets_at: number;
}

/** Codex 速率限制（两个窗口 + 计划等级）。 */
export interface CodexRateLimits {
  primary: CodexRateLimitWindow;
  secondary: CodexRateLimitWindow;
  plan_type: string;
}

/**
 * Codex（OpenAI 系）token 用量。
 * 后端真源：`web.usage_token_v2.CodexUsage`。
 * codex 自带 total/last/model_context_window/rate_limits 累加。
 */
export interface CodexUsage {
  provider: "openai";
  total: CodexTokenBreakdown;
  last: CodexTokenBreakdown;
  model_context_window: number;
  rate_limits: CodexRateLimits | null;
}

/**
 * generic_chat 通道 token 用量（底层 LLMProvider 是 OpenAI 系）。
 * 后端真源：`web.usage_token_v2.GenericChatOpenAIUsage`。
 * 形态比 CodexUsage 简化：只有 last（无 total/rate_limits）。
 */
export interface GenericChatOpenAIUsage {
  provider: "openai";
  last: CodexTokenBreakdown;
  model: string;
  context_window: number;
}

/**
 * manager.get_thread_usage 返回类型（前端按 provider discriminator 分支）。
 *
 * GenericChatAnthropicUsage 跟 ClaudeUsage 平行（discriminator 都是 "claude"），
 * 前端复用 StatusLineClaude 组件渲染；GenericChatOpenAIUsage 跟 CodexUsage
 * discriminator 同为 "openai" 但形态不同，前端需要按是否有 `total`/`rate_limits`
 * 区分（或在 v3 加额外 discriminator 字段）。
 */
export type ThreadUsage = ClaudeUsage | CodexUsage | GenericChatOpenAIUsage;

/**
 * 当前 thread 的共享 workspace 上下文。
 *
 * 首版用于 `Chat / Files / Shell` 三 tab 共用：
 * - `workspace_root` 取 thread metadata 的 `cwd`
 * - `files_available` 表示当前 thread 已绑定 workspace
 * - `shell_available` 表示当前 thread 已具备可进入的 workspace cwd
 */
export interface WorkspaceContextDTO {
  thread_id: string;
  backend_kind: BackendKind;
  workspace_root: string;
  claude_thread_id: string;
  shell_provider: "claude_code" | "system_shell" | "none";
  files_available: boolean;
  shell_available: boolean;
  unavailable_reason?: string | null;
}

export interface WorkspaceTreeNodeDTO {
  path: string;
  name: string;
  kind: "file" | "dir";
  has_children: boolean;
}

export interface WorkspaceTreeDTO {
  path: string;
  entries: WorkspaceTreeNodeDTO[];
}

export interface WorkspaceFileDTO {
  path: string;
  name: string;
  content: string;
  size_bytes: number;
  is_text: boolean;
  too_large: boolean;
  encoding: string;
}

export interface WorkspaceGitStatusEntryDTO {
  path: string;
  name: string;
  staged_status: string;
  unstaged_status: string;
  previous_path?: string | null;
}

export interface WorkspaceGitStatusDTO {
  workspace_root: string;
  repo_root: string;
  current_branch: string;
  tracking_branch?: string | null;
  ahead_count: number;
  behind_count: number;
  changes: WorkspaceGitStatusEntryDTO[];
}

export interface WorkspaceGitBranchesDTO {
  current_branch: string;
  local_branches: string[];
  remote_branches: string[];
}

export interface WorkspaceGitCommitDTO {
  commit: string;
  short_commit: string;
  author: string;
  authored_at: string;
  subject: string;
}

export interface WorkspaceGitCommitsDTO {
  commits: WorkspaceGitCommitDTO[];
}

export interface WorkspaceGitFileDiffDTO {
  path: string;
  diff: string;
}

export interface WorkspaceGitPathsRequest {
  paths: string[];
}

export interface WorkspaceGitCheckoutRequest {
  branch: string;
}

export interface WorkspaceGitCreateBranchRequest {
  branch: string;
  checkout?: boolean;
}

export interface WorkspaceGitCommitRequest {
  message: string;
}

export interface WorkspaceGitActionResultDTO {
  detail: string;
  current_branch?: string | null;
  commit?: string | null;
  short_commit?: string | null;
}

export interface UpdateWorkspaceFileRequest {
  path: string;
  content: string;
}

export interface EvolutionDecisionSummaryDTO {
  total: number;
  accepted_memory: number;
  accepted_skill: number;
  ignored: number;
  pending: number;
}

export type EvolutionDecisionValue =
  | "accept_memory"
  | "accept_skill"
  | "ignore";

export type EvolutionAppliedStatus =
  | "pending"
  | "written"
  | "skipped"
  | "failed";

export type EvolutionAppliedMode =
  | "append"
  | "update"
  | "create"
  | "ignore";

export interface EvolutionDecisionItemDTO {
  nutrient_id: string;
  decision: EvolutionDecisionValue;
  target?: "memory" | "skill" | null;
  decided_at_ms: number;
  applied_status?: EvolutionAppliedStatus | null;
  applied_path?: string | null;
  applied_mode?: EvolutionAppliedMode | null;
  applied_at_ms?: number | null;
  applied_error?: string | null;
}

export interface EvolutionNutrientDTO {
  nutrient_id: string;
  kind: "memory" | "workflow" | "error";
  title: string;
  content: string;
  summary: string;
  confidence: number;
  evidence_turns: number[];
  source_run_id: string;
  source_session_id: string;
  suggested_target?: "memory" | "skill" | "errorbook" | null;
  tags: string[];
}

export interface EvolutionReviewDTO {
  review_id: string;
  run_id: string;
  session_id: string;
  reviewed_at_ms: number;
  review_summary: string;
  nutrients: EvolutionNutrientDTO[];
  decision_summary: EvolutionDecisionSummaryDTO;
  decisions: EvolutionDecisionItemDTO[];
}

export interface EvolutionDecisionRequest {
  nutrient_id: string;
  decision: EvolutionDecisionValue;
}

export interface EvolutionDecisionResponse {
  review: EvolutionReviewDTO;
}

export type WorkspaceShellC2SFrame =
  | { type: "shell-input"; data: string }
  | { type: "shell-resize"; cols: number; rows: number }
  | { type: "shell-terminate" };

export type WorkspaceShellS2CFrame =
  | {
      type: "shell-status";
      status: "starting" | "running" | "exited" | "terminated";
      cwd: string;
      command: string[];
      exitCode?: number;
    }
  | {
      type: "shell-output";
      data: string;
    }
  | {
      type: "shell-error";
      detail: string;
    };

/**
 * 卡片作用域：
 * - `global`：所有项目可见，layout 存 `<root>/global/board.json`
 * - `project`：仅当前 cwd 可见，layout 存 `<root>/projects/<encoded>/board.json`
 *
 * 由 Manager 运行时按卡片所在 workspace 目录推断，不持久化到 board.json。
 */
export type CardScope = "project" | "global";

/**
 * 白板中的单张卡片。
 *
 * 一个卡片对应一个 markdown 文件；布局和内容一并由
 * `GET /api/threads/{thread_id}/whiteboard` 返回，避免前端首屏多请求。
 */
export interface WhiteboardCardDTO {
  scope: CardScope;
  id: string;
  title: string;
  category: string;
  filename: string;
  content: string;
  x: number;
  y: number;
  height: number;
  collapsed: boolean;
  z_index: number;
  updated_at: number;
}

/**
 * thread-scoped 白板聚合快照。
 *
 * - `global_title`：global board.json 的 title
 * - `project_title`：当前 thread.cwd 对应 project board.json 的 title；
 *   thread.cwd 为空或项目目录不存在时为 `null`
 */
export interface WhiteboardDTO {
  global_title: string;
  project_title: string | null;
  cards: WhiteboardCardDTO[];
  schema_version?: 1;
}

/**
 * 新建白板卡片请求体。
 *
 * `scope` 必填——前端必须显式选择 project 还是 global。
 */
export interface CreateWhiteboardCardRequest {
  scope: CardScope;
  title: string;
  category: string;
  content: string;
  x: number;
  y: number;
  height: number;
  collapsed?: boolean;
}

/**
 * 更新单张白板卡片请求体。
 */
export interface UpdateWhiteboardCardRequest {
  content: string;
  title?: string | null;
  category?: string | null;
  expected_updated_at?: number | null;
}

/**
 * 单张卡片的布局数据。
 */
export interface WhiteboardCardLayoutDTO {
  id: string;
  x: number;
  y: number;
  height: number;
  collapsed: boolean;
  z_index: number;
}

/**
 * 白板布局更新请求体。
 *
 * `scope` 必填——前端拖拽事件本来就知道是在 global 还是 project 卡上发生的，
 * 显式传比让后端通过遍历两份 board.json 推断更安全。
 */
export interface UpdateWhiteboardLayoutRequest {
  scope: CardScope;
  title?: string | null;
  cards: WhiteboardCardLayoutDTO[];
}

// ----------------------------------------------------------------------------
// 项目登记 / Server info DTO（v0.1，web-projects-registry-v0.1）
//
// 这些类型对应 Python `ProjectRegistryEntryDTO` / `AddProjectRequest`
// / `ServerInfoResponse`，覆盖 `/api/{claude,codex}/projects` (POST) 与
// `/api/server/info` (GET) 端点。
//
// 字段名与 Python 端**严格一致**（snake_case），不要驼峰化——pydantic 没设
// alias_generator，且与现有 `cwd` / `claude_thread_id` 等保持一致。
// ----------------------------------------------------------------------------

/**
 * 项目登记条目（`GET /api/{claude,codex}/projects` 嵌入用，可选）。
 *
 * 主 list 接口仍返回 `ProjectSummary` 形态；本 interface 是给将来纯 registry
 * list 端点预留 + 客户端内部 typing 用。
 */
export interface ProjectRegistryEntry {
  cwd: string;
  alias: string;
  added_at: number;
}

/**
 * `POST /api/{claude,codex}/projects` 请求体。
 *
 * `cwd` 必须以 `/` 开头（绝对路径）；后端再做 is_dir 校验，不存在则返 400。
 * `alias` 可选，缺省空串。
 */
export interface AddProjectRequest {
  cwd: string;
  alias?: string;
}

/**
 * `GET /api/server/info` 响应体。
 *
 * `repo_root` 是 web server 进程的项目根绝对路径，用于前端 "📍 一键填当前
 * worktree" 按钮；`schema_version` 当前固定为 1。
 */
export interface ServerInfoResponse {
  repo_root: string;
  schema_version: number;
}

// ----------------------------------------------------------------------------
// claude_code 历史浏览相关 DTO（v0.2，对应后端 ProjectSummary / SessionSummary）
//
// `GET /api/claude/projects` 返回 `{ projects: ClaudeProjectSummaryDTO[] }`；
// `POST /api/threads/import-claude-session` 入参 / 出参见下两个接口。
// 这些类型与 generic_chat 的 thread / preset 流无关，只服务于左栏 Claude tab
// 的"历史 session 树 + 一键续聊"路径。
// ----------------------------------------------------------------------------

/** 单条 Claude SDK session 摘要（从 `~/.claude/projects/<dir>/<sid>.jsonl` 抽取）。 */
export interface ClaudeSessionSummaryDTO {
  claude_thread_id: string;
  title: string;
  last_modified: number; // Unix 秒
  message_count: number;
  is_pinned: boolean;
}

/** 单个项目目录的 Claude session 列表（按 last_modified desc 排序）。 */
export interface ClaudeProjectSummaryDTO {
  name: string; // 编码目录名
  cwd: string; // 解码后绝对路径
  display_name: string; // cwd 末段
  sessions: ClaudeSessionSummaryDTO[];
}

/** Claude 项目树刷新过程中的进度帧。 */
export interface ClaudeProjectsRefreshProgressDTO {
  current: number;
  total: number;
  current_project: string;
}

/** `POST /api/threads/import-claude-session` 请求体。 */
export interface ImportClaudeSessionRequest {
  claude_thread_id: string;
  cwd: string;
  name: string;
}

/**
 * `POST /api/threads/import-claude-session` 响应。
 * `imported=false` 表示该 claude_thread_id 已绑定旧 thread，直接复用。
 */
export interface ImportClaudeSessionResponse {
  thread: ThreadMetadataDTO;
  imported: boolean;
}

// ============================================================================
// ===== C2S 帧（浏览器 → 后端，3 个；对应 Python ws_frames.py）=====
//
// C2S 帧通常无 timestamp_ms / turn / seq——这些是后端 emit 给前端时的服务端
// 语义；浏览器发出帧时由后端按收到顺序处理。
// ============================================================================

/**
 * 浏览器对 `approval.request` 的应答（v0.1.6 三态）。
 *
 * `action` 字面值与 `core.contracts.ApprovalAction` 对齐，零翻译层：
 * - `accept_once`：仅本次放行
 * - `accept_for_session`：本次放行 + 写入 session GrantStore，本 thread
 *   后续同 capability 静默放行
 * - `reject`：拒绝；超时 / ESC 也走这条
 *
 * v0.1.5 的 `approved: bool` 字段废弃，开发期不留兼容 shim。
 */
export type ApprovalAckAction =
  | "accept_once"
  | "accept_for_session"
  | "reject";

export interface ApprovalAckFrame {
  frame_type: "approval.ack";
  call_id: string;
  action: ApprovalAckAction;
}

/**
 * 浏览器侧 keep-alive 心跳；后端以 `pong` 回应。
 */
export interface PingFrame {
  frame_type: "ping";
  ts?: number;  // 客户端时间戳，用于 RTT 计算
}

/**
 * 浏览器提交一轮用户输入；后端按 `request_id` 关联回执。
 *
 * `attachments`：多模态输入（Phase 1 仅图片）。前端粘贴/上传后填入
 * 已 ready 的 `UserInputAttachment`，后端按 `asset_id` 反查实际资源。
 */
export interface UserInputFrame {
  frame_type: "user.input";
  text: string;
  request_id: string;
  reasoning_effort?: "low" | "medium" | "high" | null;
  attachments?: UserInputAttachment[];
}

/**
 * 浏览器请求打断当前 thread 上正在进行的 run（interrupt-run-v0.1）。
 *
 * UX：cell.status 处于 running / awaiting_approval 时显示 Stop 按钮，
 * 点击后发本帧。后端收到 → cancel 当前 run task → runner 顶层 except 收尾
 * → emit run.cancelled → WSEventSink fanout 转 RunInterruptedFrame
 * （多 tab 自动同步）。
 *
 * `run_id` 可选，主要给后端做诊断日志；后端不依赖它做正确性。
 * 没有 active run 时后端推 SystemNoticeFrame(notice_key="no_active_run")。
 */
export interface InterruptFrame {
  frame_type: "interrupt";
  run_id?: string | null;
}

export interface ChoiceAnswerDTO {
  question_id: string;
  option_id: string;
  option_label: string;
  custom_text?: string | null;
  value?: Record<string, unknown> | null;
}

export interface ChoiceSubmitFrame {
  frame_type: "choice.submit";
  request_id: string;
  answers: ChoiceAnswerDTO[];
}

// ============================================================================
// ===== S2C 帧（后端 → 浏览器，15 个；对应 Python ws_frames.py）=====
//
// 所有 S2C 帧必带 `timestamp_ms` 服务端时间戳，让前端按时序重排。
// 流式增量类（content.delta / reasoning.delta）额外带 `turn` 和 `seq`。
// 控制类（pong / cell.evicted / thread.history）不带 turn / seq。
// ============================================================================

/**
 * 一轮 assistant 输出收尾的最终内容（非流式或流式累计完成态）。
 */
export interface AssistantFinalFrame {
  frame_type: "assistant.final";
  timestamp_ms: number;
  content: string;
  turn: number;
  /** session 内自增 run 编号，形如 `run-{session_id}-{n}`；空串表示未携带（兼容旧后端）。 */
  run_id?: string;
}

/**
 * 审批结局通知（approved / rejected / cancelled）。
 */
export interface ApprovalDecisionFrame {
  frame_type: "approval.decision";
  timestamp_ms: number;
  call_id: string;
  outcome: ApprovalOutcome;
  turn: number;
}

/**
 * 工具执行前向用户请求审批，浏览器需回 `approval.ack`。
 */
export interface ApprovalRequestFrame {
  frame_type: "approval.request";
  timestamp_ms: number;
  call_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  reason?: string;
  turn: number;
  /** elevated 审批时为 "elevated"，标准审批时为 "standard" 或 undefined */
  policy_hint?: string;
  /** elevated 审批时的确认令牌（8 hex），用户需输入后才能点同意 */
  confirm_token?: string;
}

export interface ChoiceOptionDTO {
  id: string;
  label: string;
  description: string;
  value?: Record<string, unknown> | null;
}

export interface ChoiceQuestionDTO {
  id: string;
  title: string;
  description?: string | null;
  options: ChoiceOptionDTO[];
}

export interface ChoiceRequestFrame {
  frame_type: "choice.request";
  timestamp_ms: number;
  request_id: string;
  title: string;
  description: string;
  questions: ChoiceQuestionDTO[];
  turn: number;
  run_id?: string;
}

/**
 * thread cell 被回收（idle / 手动停止 / shutdown / 错误）。
 */
export interface CellEvictedFrame {
  frame_type: "cell.evicted";
  timestamp_ms: number;
  thread_id: string;
  reason: EvictReason;
  message?: string;
}

/**
 * run 被用户 interrupt 后的收尾通知（interrupt-run-v0.1）。
 *
 * 触发路径：runner 顶层 `except asyncio.CancelledError` → emit `run.cancelled`
 * event → WSEventSink fanout 转本帧给该 thread 名下所有 attach 的 ws
 * （A tab 点 Stop → B tab 也收到）。
 *
 * 后续 runner 还会 emit `run.end`（status="cancelled"），cell.status 切回 idle；
 * 前端可隐藏 Stop 按钮、显示"已中断"提示。
 *
 * `cancelled_tool_call_id` 为 null 表示打断在 LLM / approval 阶段
 * （pending tool 已被 runner 写占位 tool_result）。
 */
export interface RunInterruptedFrame {
  frame_type: "run.interrupted";
  timestamp_ms: number;
  run_id: string;
  cancelled_at_turn: number;
  cancelled_tool_call_id?: string | null;
  cancel_reason: string;
}

/**
 * 系统提示卡片帧。
 *
 * 第一版用于 self-evolution 复盘链路，把 started / completed / failed /
 * drain_timeout 显式送到聊天时间线。
 */
export interface SystemNoticeFrame {
  frame_type: "system.notice";
  timestamp_ms: number;
  notice_key: string;
  source: string;
  status: SystemNoticeStatus;
  title: string;
  message: string;
  details?: Record<string, unknown> | string[] | null;
  icon?: SystemNoticeIcon | null;
  run_id?: string;
}

/**
 * assistant 文本流式增量（按 `seq` 重排）。
 */
export interface ContentDeltaFrame {
  frame_type: "content.delta";
  timestamp_ms: number;
  delta: string;
  turn: number;
  seq: number;
  /** session 内自增 run 编号；前端 buffer key 用 `(threadId, run_id, turn)` 三参隔离不同 run。 */
  run_id?: string;
}

/**
 * 错误事件（network / llm_error / tool_error / approval_timeout / internal）。
 */
export interface ErrorFrame {
  frame_type: "error";
  timestamp_ms: number;
  error_code: ErrorCode;
  message: string;
  turn?: number;
}

/**
 * 对 `ping` 的应答；仅含 `timestamp_ms`。
 */
export interface PongFrame {
  frame_type: "pong";
  timestamp_ms: number;
  ts?: number;  // 原样回传客户端的 ts，用于 RTT 计算
}

/**
 * assistant reasoning 流式增量（按 `seq` 重排）。
 */
export interface ReasoningDeltaFrame {
  frame_type: "reasoning.delta";
  timestamp_ms: number;
  delta: string;
  turn: number;
  seq: number;
  run_id?: string;
}

/**
 * 连接建立 / resume 后下发的历史消息列表。
 */
export interface ThreadHistoryFrame {
  frame_type: "thread.history";
  timestamp_ms: number;
  messages: NormalizedMessage[];
}

/**
 * 单次工具执行结束（含成功 / 业务失败两态，异常走 `error` 帧）。
 *
 * v0.1.6 加 `content` / `data` 字段：之前协议漏字段导致 UI 看不到工具结果，
 * 展开框退化为显示 `arguments`（入参）造成 `{}` 假象。
 */
export interface ToolCallEndFrame {
  frame_type: "tool.call.end";
  timestamp_ms: number;
  call_id: string;
  turn: number;
  ok: boolean;
  error_message?: string | null;
  /** 工具产出文本（ToolResult.content）；空串表示无文本输出。 */
  content?: string;
  /** 工具产出结构化数据（ToolResult.data）；null/undefined 表示无结构化数据。 */
  data?: Record<string, unknown> | null;
  run_id?: string;
}

/**
 * 单次工具执行开始（在 `approval.decision` approved 之后）。
 */
export interface ToolCallStartFrame {
  frame_type: "tool.call.start";
  timestamp_ms: number;
  tool_name: string;
  call_id: string;
  turn: number;
  arguments: Record<string, unknown>;
  run_id?: string;
}

/**
 * 一轮 turn 结束标记。
 */
export interface TurnEndFrame {
  frame_type: "turn.end";
  timestamp_ms: number;
  turn: number;
  run_id?: string;
}

/**
 * 一轮 turn 开始标记。
 */
export interface TurnStartFrame {
  frame_type: "turn.start";
  timestamp_ms: number;
  turn: number;
  run_id?: string;
}

/**
 * 一轮 token 用量回报（usage-token-v2-bigbang 重构）。
 *
 * 后端真源：`web.protocol.ws_frames.UsageFrame`（usage 字段是 v2
 * channel-specific DTO dict，含 provider discriminator）。
 *
 * 前端按 `usage.provider` 分支：
 * - `"claude"` → 渲染 StatusLineClaude（用 ClaudeUsage 字段）
 * - `"openai"` → 渲染 StatusLineCodex 或 StatusLineGenericChat
 *   （按是否有 `total` / `rate_limits` 字段区分）
 */
export interface UsageFrame {
  frame_type: "usage";
  timestamp_ms: number;
  turn: number;
  run_id?: string;
  usage: ThreadUsage;
}

// ============================================================================
// ===== Unions（对应 Python ws_frames.py 的 WSFrameC2S / WSFrameS2C）=====
//
// 在 TS 侧用 discriminated union（discriminant = `frame_type`，v0.2 起统一），
// 消费方可用 switch (frame.frame_type) 或类型守卫函数把 union 收窄到具体帧类型。
// ============================================================================

/** C2S 帧 union（浏览器 → 后端）。 */
export type WSFrameC2S =
  | UserInputFrame
  | ApprovalAckFrame
  | PingFrame
  | InterruptFrame
  | ChoiceSubmitFrame;

/** S2C 帧 union（后端 → 浏览器）。 */
export type WSFrameS2C =
  | ThreadHistoryFrame
  | AssistantFinalFrame
  | ContentDeltaFrame
  | ReasoningDeltaFrame
  | SystemNoticeFrame
  | ToolCallStartFrame
  | ToolCallEndFrame
  | ApprovalRequestFrame
  | ApprovalDecisionFrame
  | UsageFrame
  | ErrorFrame
  | TurnStartFrame
  | TurnEndFrame
  | PongFrame
  | CellEvictedFrame
  | RunInterruptedFrame
  | ChoiceRequestFrame;

// ============================================================================
// ===== 类型守卫示例（Type Guards）=====
//
// 这里给每个 S2C 帧提供一个 narrow 函数；C2S 也类似。
// 业务代码也可以直接用 `switch (frame.frame_type)`，TS 编译器会自动 narrow，
// 不一定要走 isXxx 函数。两种姿势都对，按团队风格选一即可。
//
// 示例：
//
//   function handleS2C(frame: WSFrameS2C) {
//     if (isContentDelta(frame)) {
//       // 这里 frame 已被 narrow 为 ContentDeltaFrame
//       console.log(frame.delta, frame.seq);
//     }
//   }
//
//   // 或直接用 switch（更紧凑，编译器会做穷尽性检查）：
//   function handleS2CSwitch(frame: WSFrameS2C) {
//     switch (frame.frame_type) {
//       case "content.delta": return frame.delta;
//       case "reasoning.delta": return frame.delta;
//       // ...其它 case
//       default: return null;
//     }
//   }
// ============================================================================

export function isContentDelta(f: WSFrameS2C): f is ContentDeltaFrame {
  return f.frame_type === "content.delta";
}

export function isReasoningDelta(f: WSFrameS2C): f is ReasoningDeltaFrame {
  return f.frame_type === "reasoning.delta";
}

export function isAssistantFinal(f: WSFrameS2C): f is AssistantFinalFrame {
  return f.frame_type === "assistant.final";
}

export function isApprovalRequest(f: WSFrameS2C): f is ApprovalRequestFrame {
  return f.frame_type === "approval.request";
}

export function isApprovalDecision(f: WSFrameS2C): f is ApprovalDecisionFrame {
  return f.frame_type === "approval.decision";
}

export function isToolCallStart(f: WSFrameS2C): f is ToolCallStartFrame {
  return f.frame_type === "tool.call.start";
}

export function isSystemNotice(f: WSFrameS2C): f is SystemNoticeFrame {
  return f.frame_type === "system.notice";
}

export function isToolCallEnd(f: WSFrameS2C): f is ToolCallEndFrame {
  return f.frame_type === "tool.call.end";
}

export function isUsage(f: WSFrameS2C): f is UsageFrame {
  return f.frame_type === "usage";
}

export function isError(f: WSFrameS2C): f is ErrorFrame {
  return f.frame_type === "error";
}

export function isTurnStart(f: WSFrameS2C): f is TurnStartFrame {
  return f.frame_type === "turn.start";
}

export function isTurnEnd(f: WSFrameS2C): f is TurnEndFrame {
  return f.frame_type === "turn.end";
}

export function isPong(f: WSFrameS2C): f is PongFrame {
  return f.frame_type === "pong";
}

export function isCellEvicted(f: WSFrameS2C): f is CellEvictedFrame {
  return f.frame_type === "cell.evicted";
}

export function isThreadHistory(f: WSFrameS2C): f is ThreadHistoryFrame {
  return f.frame_type === "thread.history";
}

export function isUserInput(f: WSFrameC2S): f is UserInputFrame {
  return f.frame_type === "user.input";
}

export function isApprovalAck(f: WSFrameC2S): f is ApprovalAckFrame {
  return f.frame_type === "approval.ack";
}

export function isPing(f: WSFrameC2S): f is PingFrame {
  return f.frame_type === "ping";
}

// ============================================================================
// ===== claude_code 路径协议（v0.1.6，对应 Python src/web/claude_code/llm_protocol.py）
//
// 这是 `/ws/claude-code` endpoint 的协议，与上面的 generic_chat（/ws/threads/{id}）
// 协议**完全独立**——形态不同：generic_chat 是严格 discriminated union，claude_code
// 是扁平 dict + `kind` 判别字段，字段大多可选（来自 Claude Agent SDK 流式输出的
// 异构特性）。
//
// 不要把两者 union 在一起——前端按 thread.backend_kind 选不同路径渲染。
// ============================================================================

/**
 * `NormalizedMessage` 的 provider 维度（与 Python `LLMProvider` 一致）。
 *
 * v0.1 仅 claude；保留 codex/gemini/cursor 占位以便后续扩展同协议接入。
 */
export type NormalizedProvider = "claude" | "codex" | "gemini" | "cursor" | "generic_chat";

/**
 * `NormalizedMessage` 的 15 种 kind（与 Python `MessageKind` 一致）。
 *
 * `stream_status` 由 `StreamEvent.message_start` / `content_block_start`
 * 控制帧翻译而来，前端用于在第一个 token 到达前显示当前阶段
 * （思考中 / 生成中 / 调用工具）。
 */
export type NormalizedMessageKind =
  | "text"
  | "tool_use"
  | "tool_result"
  | "thinking"
  | "stream_delta"
  | "stream_end"
  | "stream_status"
  | "session_created"
  | "permission_request"
  | "permission_cancelled"
  | "complete"
  | "error"
  | "status"
  | "interactive_prompt"
  | "task_notification";

/**
 * `/ws/claude-code` endpoint 的归一化消息（来自 Claude Agent SDK 流的翻译）。
 *
 * 形态选择说明：
 *
 * - 这里**有意**用扁平 interface + 大量可选字段，不用 discriminated union——
 *   原因是 SDK 流式 partial message 的字段组合非常稀疏（同一类 kind 在不同
 *   消息序列里也可能字段缺失），用严格 union 反而要为每个 kind 写三五份变体
 * - 消费方按 `kind` 分支渲染：`text` / `thinking` 用普通气泡；`stream_delta`
 *   累加到当前流式 buffer；`stream_end` 收尾；`tool_use` / `tool_result`
 *   渲染工具卡片；`permission_request` 弹审批 dialog；`session_created` 不
 *   渲染只记 newSessionId；`complete` 显示对话结束 + tokenBudget；`error` 报错
 * - 字段命名走 camelCase 与 SDK / ccui 对齐（与 Python wire 已通过
 *   `ClaudeNormalizer._snake_to_camel` 规范化）
 */
export interface NormalizedMessage {
  /** 消息类型；用作前端 switch 分支主键 */
  frame_type: NormalizedMessageKind;
  /** SDK provider（v0.1 总是 "claude"） */
  provider?: NormalizedProvider;
  /** SDK session_id（首条 session_created 之后切换到真实 id） */
  sessionId?: string | null;
  /** ISO 时间戳，由 normalizer 填充 */
  timestamp?: string;
  /** 消息 id（SDK 提供） */
  id?: string;
  /** assistant / user 角色 */
  role?: "user" | "assistant";
  /** kind="text" / "thinking" / "tool_result" 的内容；类型不固定（SDK 透传） */
  content?: unknown;
  /** kind="tool_use" 的工具名 */
  toolName?: string;
  /** kind="tool_use" 的工具入参；与 `input` 同义（normalizer 出 toolInput） */
  toolInput?: unknown;
  /** kind="tool_use" / "tool_result" / "stream_status"(phase=tool_calling) 关联的 SDK tool_use_id */
  toolId?: string;
  /** kind="tool_result" 是否错误 */
  isError?: boolean;
  /** kind="permission_request" 的 requestId（用于回 claude-permission-response） */
  requestId?: string;
  /** kind="permission_request" 的入参（与 toolInput 等价的别名，normalizer 选填） */
  input?: unknown;
  /** kind="session_created" 的真实 SDK session_id */
  newSessionId?: string;
  /** kind="complete" 的退出码（SDK 提供） */
  exitCode?: number;
  /** kind="complete" 是否被 abort */
  aborted?: boolean;
  /** kind="complete" 的 token 用量摘要（camelCase） */
  tokenBudget?: Record<string, unknown>;
  /** kind="error" 的错误描述 */
  error?: string;
  /** kind="stream_status"：当前阶段（responding=生成 / thinking=思考 / tool_calling=调用工具） */
  phase?: "responding" | "thinking" | "tool_calling";
  /** kind="stream_status"：SDK content_block 的 index（同一 turn 内自增） */
  blockIndex?: number;
  /** kind="stream_delta"：delta 子类型（text / thinking / input_json） */
  deltaType?: "text" | "thinking" | "input_json";
  /** kind="stream_status"：message_start 携带的 model 名 */
  model?: string;
}

// ---------------------------------------------------------------------------
// Thread Status（全局 WS 广播 /ws/thread-status）
// ---------------------------------------------------------------------------

export type ThreadStatusPhase =
  | "idle"
  | "responding"
  | "thinking"
  | "tool_calling"
  | "waiting_approval"
  | "complete"
  | "error";

export interface ThreadStatusFrame {
  frame_type: "thread-status";
  threadId: string;
  phase: ThreadStatusPhase;
  toolName?: string | null;
}

// ---------------------------------------------------------------------------
// Claude Code WebSocket 协议（v0.1.6；原 `web/src/lib/claude-ws.ts` 内业务类型，
// network-layer v0.1 重构时迁入 protocol 真源以便随 Python 端协议同步维护。
// ---------------------------------------------------------------------------

/**
 * Claude Code endpoint 客户端帧（v0.1.6）。
 *
 * 这是 `/ws/claude-code` 接受的 6 类入站帧，对应 `src/web/claude_code/route.py::_dispatch`。
 *
 * 跟 generic_chat 的 WSFrameC2S **完全独立** —— 字段命名走 SDK / ccui 风格
 * （camelCase + `frame_type` 判别字段，v0.2 统一）。
 *
 * 网络层 v0.1 之后，心跳帧 `{ frame_type: "ping", ts }` 走 NetworkManager 透明拦截，
 * 不进入本 union。
 */
export type ClaudeCodeC2SFrame =
  | {
      frame_type: "claude-command";
      command: string;
      options?: Record<string, unknown>;
      /**
       * 图片附件（claude-code-channel-image-paste）。
       *
       * 后端 `_dispatch` 解析后传给 `service.query(attachments=...)`，
       * 由 `AttachmentPrefixBuilder` 拼成 `@<abs_path>` 注入 prompt 头部。
       * 缺省 / 空数组 → 走纯文本 prompt 路径，向后兼容。
       */
      attachments?: UserInputAttachment[];
    }
  | {
      frame_type: "claude-permission-response";
      requestId: string;
      allow: boolean;
      message?: string;
      rememberEntry?: string;
    }
  | { frame_type: "abort-session"; sessionId: string }
  | { frame_type: "check-session-status"; sessionId: string }
  /* smart-approval-v1 */
  | { frame_type: "auto-approval-toggle"; cwd: string; enabled: boolean }
  | { frame_type: "auto-approval-query"; cwd: string };

/**
 * 后端到前端的两类帧：NormalizedMessage（主流）+ session-status（特殊）+
 * auto_approval_state（smart-approval-v1）。
 *
 * `session-status` 是 `check-session-status` 的应答；v0.2 起所有 wire 帧
 * 都用 `frame_type` 判别字段，单独建模便于不同字段集独立演化。
 */
export interface SessionStatusFrame {
  frame_type: "session-status";
  sessionId: string;
  isProcessing: boolean;
}

/** smart-approval-v1 状态帧（per-cwd toggle 配置） */
export interface AutoApprovalStateWireFrame {
  frame_type: "auto_approval_state";
  channel: "claude_code" | "generic_chat";
  cwd: string;
  enabled: boolean;
  timeoutMs: number;
  ruleOverrides: Record<string, boolean>;
}

export type ClaudeCodeS2CFrame =
  | NormalizedMessage
  | SessionStatusFrame
  | AutoApprovalStateWireFrame;
