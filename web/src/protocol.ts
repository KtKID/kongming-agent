/**
 * kongming-agent v0.1.5 web 协议层 TypeScript 复刻。
 *
 * ## 这份文件是什么
 *
 * 本文件是 Python 侧 `src/web/protocol/` 三个模块的 TS 1:1 复刻：
 *
 * - Python `src/web/protocol/_base.py`        → 本文件「公共枚举」段
 * - Python `src/web/protocol/rest_models.py`  → 本文件「REST DTO」段（8 个）
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
 * - `kind` 字段是 discriminated union 的判别 tag，外层用 switch / 类型守卫 narrow
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
 *
 * 用于 `HistoryMessageDTO.role`。
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
// ===== REST DTO（对应 Python src/web/protocol/rest_models.py，共 8 个）=====
// ============================================================================

/**
 * 单条历史消息 DTO（user / assistant / tool 三类）。
 *
 * 既用于 `GET /api/threads/{id}/history` REST 响应，也作为
 * `ThreadHistoryFrame.messages` 的元素。
 * `tool_call_id` / `tool_name` / `ok` / `data` / `error_message` 仅
 * `role === "tool"` 时有意义，其它角色应为 undefined。
 *
 * v0.1.6 加 `tool_name` / `ok` / `data` / `error_message`：让前端历史重放
 * 路径能恢复 tool 卡片的工具名 + 结果状态 + 结构化数据。
 */
export interface HistoryMessageDTO {
  role: HistoryMessageRole;
  content: string;
  turn: number;
  timestamp_ms: number;
  tool_call_id?: string | null;
  tool_name?: string | null;
  ok?: boolean | null;
  data?: Record<string, unknown> | null;
  error_message?: string | null;
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
 * 重命名 thread 请求体（`PATCH /api/threads/{id}`）。
 *
 * `name.length <= 200`（Python 侧 Pydantic 校验）。
 */
export interface RenameThreadRequest {
  name: string;
}

/**
 * Thread 元数据 + `GET /api/threads/{id}` 响应体。
 *
 * `id` 形如 `thread-<12 位 hex>`；
 * `name.length <= 200`；
 * `message_count >= 0`；
 * `schema_version`：当前 v6；老文件由后端读盘时懒升级。
 *
 * v0.1.6 新增 `backend_kind`；老 v1 文件读入默认 `generic_chat`。
 * v0.2 新增 `claude_thread_id` + `cwd`（claude_code thread 与 SDK session 持久化绑定）；
 * 老 v2 文件读入默认空字符串。
 * v0.2.1 新增 thread 级累计 `cumulative_*_tokens`；老 v3 文件读入默认 0。
 * v0.2.2 新增 `codex_thread_id`，`backend_kind` 支持 `codex`。
 * v0.2.3 将旧 `sdk_session_id` 改名为 `claude_thread_id`。
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
  /** thread 级累计输入 token 总量，每次 run 结束后累加 */
  cumulative_prompt_tokens: number;
  /** thread 级累计输出 token 总量 */
  cumulative_completion_tokens: number;
  /** thread 级累计总 token */
  cumulative_total_tokens: number;
  /** cache read token 累计；null 表示数据不可用（前端显示 "-"） */
  cumulative_cache_read_tokens: number | null;
  /** cache creation token 累计；null 表示数据不可用（前端显示 "-"） */
  cumulative_cache_creation_tokens: number | null;
  /** 元数据 schema 版本号，当前 6；接受 1-6（老文件兼容），写盘永远写最新版 */
  schema_version?: 1 | 2 | 3 | 4 | 5 | 6;
}

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
 * 白板中的单张卡片。
 *
 * 一个卡片对应一个 markdown 文件；布局和内容一并由 `GET /api/whiteboard`
 * 返回，避免前端首屏多请求。
 */
export interface WhiteboardCardDTO {
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
 * workspace 级白板快照。
 */
export interface WhiteboardDTO {
  title: string;
  cards: WhiteboardCardDTO[];
  schema_version?: 1;
}

/**
 * 新建白板卡片请求体。
 */
export interface CreateWhiteboardCardRequest {
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
 */
export interface UpdateWhiteboardLayoutRequest {
  title?: string | null;
  cards: WhiteboardCardLayoutDTO[];
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
  kind: "approval.ack";
  call_id: string;
  action: ApprovalAckAction;
}

/**
 * 浏览器侧 keep-alive 心跳；后端以 `pong` 回应。
 */
export interface PingFrame {
  kind: "ping";
}

/**
 * 浏览器提交一轮用户输入；后端按 `request_id` 关联回执。
 */
export interface UserInputFrame {
  kind: "user.input";
  text: string;
  request_id: string;
  reasoning_effort?: "low" | "medium" | "high" | null;
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
  kind: "assistant.final";
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
  kind: "approval.decision";
  timestamp_ms: number;
  call_id: string;
  outcome: ApprovalOutcome;
  turn: number;
}

/**
 * 工具执行前向用户请求审批，浏览器需回 `approval.ack`。
 */
export interface ApprovalRequestFrame {
  kind: "approval.request";
  timestamp_ms: number;
  call_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  reason?: string;
  turn: number;
}

/**
 * thread cell 被回收（idle / 手动停止 / shutdown / 错误）。
 */
export interface CellEvictedFrame {
  kind: "cell.evicted";
  timestamp_ms: number;
  thread_id: string;
  reason: EvictReason;
  message?: string;
}

/**
 * 系统提示卡片帧。
 *
 * 第一版用于 self-evolution 复盘链路，把 started / completed / failed /
 * drain_timeout 显式送到聊天时间线。
 */
export interface SystemNoticeFrame {
  kind: "system.notice";
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
  kind: "content.delta";
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
  kind: "error";
  timestamp_ms: number;
  error_code: ErrorCode;
  message: string;
  turn?: number;
}

/**
 * 对 `ping` 的应答；仅含 `timestamp_ms`。
 */
export interface PongFrame {
  kind: "pong";
  timestamp_ms: number;
}

/**
 * assistant reasoning 流式增量（按 `seq` 重排）。
 */
export interface ReasoningDeltaFrame {
  kind: "reasoning.delta";
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
  kind: "thread.history";
  timestamp_ms: number;
  messages: HistoryMessageDTO[];
}

/**
 * 单次工具执行结束（含成功 / 业务失败两态，异常走 `error` 帧）。
 *
 * v0.1.6 加 `content` / `data` 字段：之前协议漏字段导致 UI 看不到工具结果，
 * 展开框退化为显示 `arguments`（入参）造成 `{}` 假象。
 */
export interface ToolCallEndFrame {
  kind: "tool.call.end";
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
  kind: "tool.call.start";
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
  kind: "turn.end";
  timestamp_ms: number;
  turn: number;
  run_id?: string;
}

/**
 * 一轮 turn 开始标记。
 */
export interface TurnStartFrame {
  kind: "turn.start";
  timestamp_ms: number;
  turn: number;
  run_id?: string;
}

/**
 * 一轮 token 用量回报（prompt / completion / total）。
 */
export interface UsageFrame {
  kind: "usage";
  timestamp_ms: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cache_read_tokens?: number | null;
  cache_creation_tokens?: number | null;
  turn: number;
  run_id?: string;
}

// ============================================================================
// ===== Unions（对应 Python ws_frames.py 的 WSFrameC2S / WSFrameS2C）=====
//
// 在 TS 侧用 discriminated union（discriminant = `kind`），消费方可用
// switch (frame.kind) 或类型守卫函数把 union 收窄到具体帧类型。
// ============================================================================

/** C2S 帧 union（浏览器 → 后端）。 */
export type WSFrameC2S = UserInputFrame | ApprovalAckFrame | PingFrame;

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
  | CellEvictedFrame;

// ============================================================================
// ===== 类型守卫示例（Type Guards）=====
//
// 这里给每个 S2C 帧提供一个 narrow 函数；C2S 也类似。
// 业务代码也可以直接用 `switch (frame.kind)`，TS 编译器会自动 narrow，
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
//     switch (frame.kind) {
//       case "content.delta": return frame.delta;
//       case "reasoning.delta": return frame.delta;
//       // ...其它 case
//       default: return null;
//     }
//   }
// ============================================================================

export function isContentDelta(f: WSFrameS2C): f is ContentDeltaFrame {
  return f.kind === "content.delta";
}

export function isReasoningDelta(f: WSFrameS2C): f is ReasoningDeltaFrame {
  return f.kind === "reasoning.delta";
}

export function isAssistantFinal(f: WSFrameS2C): f is AssistantFinalFrame {
  return f.kind === "assistant.final";
}

export function isApprovalRequest(f: WSFrameS2C): f is ApprovalRequestFrame {
  return f.kind === "approval.request";
}

export function isApprovalDecision(f: WSFrameS2C): f is ApprovalDecisionFrame {
  return f.kind === "approval.decision";
}

export function isToolCallStart(f: WSFrameS2C): f is ToolCallStartFrame {
  return f.kind === "tool.call.start";
}

export function isSystemNotice(f: WSFrameS2C): f is SystemNoticeFrame {
  return f.kind === "system.notice";
}

export function isToolCallEnd(f: WSFrameS2C): f is ToolCallEndFrame {
  return f.kind === "tool.call.end";
}

export function isUsage(f: WSFrameS2C): f is UsageFrame {
  return f.kind === "usage";
}

export function isError(f: WSFrameS2C): f is ErrorFrame {
  return f.kind === "error";
}

export function isTurnStart(f: WSFrameS2C): f is TurnStartFrame {
  return f.kind === "turn.start";
}

export function isTurnEnd(f: WSFrameS2C): f is TurnEndFrame {
  return f.kind === "turn.end";
}

export function isPong(f: WSFrameS2C): f is PongFrame {
  return f.kind === "pong";
}

export function isCellEvicted(f: WSFrameS2C): f is CellEvictedFrame {
  return f.kind === "cell.evicted";
}

export function isThreadHistory(f: WSFrameS2C): f is ThreadHistoryFrame {
  return f.kind === "thread.history";
}

export function isUserInput(f: WSFrameC2S): f is UserInputFrame {
  return f.kind === "user.input";
}

export function isApprovalAck(f: WSFrameC2S): f is ApprovalAckFrame {
  return f.kind === "approval.ack";
}

export function isPing(f: WSFrameC2S): f is PingFrame {
  return f.kind === "ping";
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
export type NormalizedProvider = "claude" | "codex" | "gemini" | "cursor";

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
  kind: NormalizedMessageKind;
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
  /** kind="tool_use" / "tool_result" 关联的 SDK tool_use_id */
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
  type: "thread-status";
  threadId: string;
  phase: ThreadStatusPhase;
  toolName?: string | null;
}
