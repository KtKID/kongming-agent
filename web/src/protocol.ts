/**
 * kongming-agent v0.1.5 web 协议层 TypeScript 复刻。
 *
 * ## 这份文件是什么
 *
 * 本文件是 Python 侧 `src/web/protocol/` 三个模块的 TS 1:1 复刻：
 *
 * - Python `src/web/protocol/_base.py`        → 本文件「公共枚举」段
 * - Python `src/web/protocol/rest_models.py`  → 本文件「REST DTO」段（8 个）
 * - Python `src/web/protocol/ws_frames.py`    → 本文件「C2S 帧」+「S2C 帧」+「Unions」三段（17 帧 + 2 union）
 *
 * 字段名 / 类型 / 必选与 Python 侧严格对应；可选字段（Python `X | None = None`）
 * 用 TS 的 `field?: X` 表达；Pydantic `Literal["x"]` 单值用 TS 字面量类型。
 *
 * ## 维护方式
 *
 * 本文件**由人手维护**——v0.1.5 暂不引入 OpenAPI / msgspec / datamodel-codegen
 * 这类自动生成工具，理由：
 * - 17 帧 + 8 DTO 体量小，手写一次即可，引入 codegen 工具会带来构建链复杂度
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
 */
export interface CreateThreadRequest {
  name: string;
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
 * `schema_version` 默认 1，未来字段演进时 bump 该值，旧文件读入后由迁移层升级。
 */
export interface ThreadMetadataDTO {
  id: string;
  name: string;
  preset_id: string;
  created_at: number;
  updated_at: number;
  message_count: number;
  schema_version?: 1;
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
// ===== S2C 帧（后端 → 浏览器，14 个；对应 Python ws_frames.py）=====
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
  turn: number;
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
