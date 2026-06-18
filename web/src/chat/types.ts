/**
 * kongming-agent message-runtime-v0.1 · 前端聊天运行时共享契约真源
 *
 * ## 这份文件是什么
 *
 * `message-runtime-v0.1` spec 的「公用输入 + provider 扩展 + 通用事件 + 通用时间线」
 * 四层类型的单一真源。Generic / Claude / Codex 三条聊天链路的发送、接收、历史回放
 * 都先归一到这里定义的结构，再由各自的 `ChatProvider` 翻译成频道 wire frame。
 *
 * 设计文档：
 * - `docs/web-chat/message-runtime-v0.1/02-module-breakdown.md`（模块接口）
 * - `docs/web-chat/message-runtime-v0.1/04-data-and-state.md`（数据模型，本文件的事实源）
 *
 * ## 真源复用约束（不允许第二份同名协议定义）
 *
 * - `UserInputAttachment` 复用 Python↔TS 协议真源 `@/protocol`（见根 CLAUDE.md 约束 16）。
 *   本地上传临时态（`localPath` / `File` / 进度条等 UI-only 数据）需要时另起
 *   `DraftAttachment` 之类的名字，**不占用** `UserInputAttachment`。
 * - `ReasoningEffort` 复用 `Composer` 既有定义，不在本文件新建第二份字面量 union。
 *
 * ## 时间戳约定
 *
 * 全链路统一用毫秒时间戳（`number`）：`RawFrameEnvelope.receivedAt`、wire 帧的
 * `timestamp_ms`、本文件所有 `*At` 字段都是同一数值语义，中途不做 number→string 转换。
 */

import type {
  ChoiceSubmitFrame,
  UserInputAttachment,
} from "@/protocol";
import type { ReasoningEffort } from "@/components/Composer";

// 复用既有真源，转手 re-export，让 chat/* 其它模块从本文件统一 import，
// 而不是各自去够 `@/components/Composer`。
export type { UserInputAttachment, ReasoningEffort };

// ---------------------------------------------------------------------------
// 基础枚举
// ---------------------------------------------------------------------------

/** 频道种类标识。三条聊天链路共用。 */
export type ChatProviderKind = "generic" | "claude" | "codex";

// ---------------------------------------------------------------------------
// 发送输入：公用层 common + provider 专有层 provider
// ---------------------------------------------------------------------------

/** 所有频道共享的公用发送输入。文本、附件、思考等级等公用字段只定义一次。 */
export interface CommonSendInput {
  /** 用户输入的主文本。 */
  text: string;
  /**
   * 用户附带的已上传附件，直接复用现有协议真源。
   * 本地上传临时态属于 UI-only 数据，需要时另起 `DraftAttachment`，不占用本类型。
   */
  attachments?: UserInputAttachment[];
  /** 思考等级，属于跨频道公共能力。 */
  reasoningEffort?: ReasoningEffort | null;
  /** 当前工作目录，便于 provider 侧执行或 resume。 */
  cwd?: string | null;
  /** 预留的扩展元数据，例如来源端、实验开关。 */
  metadata?: Record<string, unknown> | null;
}

/** Generic 频道发送选项：generic 后端无 provider session 概念，只需 threadId。 */
export interface GenericSendOptions {
  /** provider 固定值，便于联合类型分发。 */
  provider: "generic";
  /** 所属线程 id。 */
  threadId: string;
  /** 当前发送采用的 preset；缺省时沿用 thread metadata。 */
  presetId?: string | null;
  /** UI 层模型家族标识，便于审计日志和测试断言。 */
  modelFamilyId?: string | null;
}

/** Claude 频道发送选项。 */
export interface ClaudeSendOptions {
  /** provider 固定值。 */
  provider: "claude";
  /** 所属线程 id。 */
  threadId: string;
  /** 已存在的 claude session id，resume 时携带。 */
  sessionId?: string | null;
  /** 是否请求 resume 现有会话。 */
  resume?: boolean;
  /** 可选模型名，当前先保留扩展位。 */
  model?: string | null;
}

/** Codex 频道发送选项。 */
export interface CodexSendOptions {
  /** provider 固定值。 */
  provider: "codex";
  /** 所属线程 id。 */
  threadId: string;
  /** 已存在的 codex session id，resume 时携带。 */
  sessionId?: string | null;
  /** 是否 resume。 */
  resume?: boolean;
  /** 可选模型名。 */
  model?: string | null;
  /** 权限模式，映射 codex 的审批策略（与 `lib/codex-ws.ts` 既有枚举一致）。 */
  permissionMode?: "default" | "acceptEdits" | "bypassPermissions";
}

/** provider 专有发送选项联合类型，按 `provider` 字段分发。 */
export type ProviderSendOptions =
  | GenericSendOptions
  | ClaudeSendOptions
  | CodexSendOptions;

/** 统一发送请求：公用输入层 + provider 专有配置层。 */
export interface SendRequest {
  /** 公共输入层，所有频道共享。 */
  common: CommonSendInput;
  /** provider 独有配置层，按联合类型分发。 */
  provider: ProviderSendOptions;
}

/** ChoicePanel 确认后的结构化提交请求。 */
export interface ChoiceSubmitRequest {
  /** 当前线程 id。 */
  threadId: string;
  /** 首版只支持 generic_chat 频道。 */
  provider: "generic";
  /** 与后端 ChoiceSubmitFrame 一一对应的 wire 帧。 */
  frame: ChoiceSubmitFrame;
}

// ---------------------------------------------------------------------------
// 历史 / 打断 / 会话状态请求
// ---------------------------------------------------------------------------

/** 历史加载请求。 */
export interface HistoryLoadRequest {
  /** 当前线程 id。 */
  threadId: string;
  /** 频道类型。 */
  provider: ChatProviderKind;
  /**
   * Codex 专有：thread metadata 落盘的 resume session id（`codex_thread_id`），
   * 历史 REST 端点 `/api/codex/sessions/{codex_thread_id}/history` 的真正路径参数，
   * **不等于** `threadId`。调用方（ChatManager / CodexView）从 thread metadata 透传；
   * 其它频道留空。
   */
  codexThreadId?: string;
  /** 是否包含工具类消息。 */
  includeTools?: boolean;
  /** 历史条数上限。 */
  maxMessages?: number;
}

/** 打断请求。 */
export interface InterruptRequest {
  /** 当前线程 id。 */
  threadId: string;
  /** 频道类型。 */
  provider: ChatProviderKind;
  /** provider 的 session id，存在时更精准。 */
  sessionId?: string | null;
}

/** 会话状态查询请求。 */
export interface SessionStatusRequest {
  /** 当前线程 id。 */
  threadId: string;
  /** 频道类型。 */
  provider: ChatProviderKind;
}

/** 会话状态查询结果。 */
export interface SessionStatus {
  /** provider 当前是否有活跃会话。 */
  active: boolean;
  /** provider 返回的真实 session id。 */
  sessionId?: string | null;
  /** 给界面展示的补充说明。 */
  message?: string | null;
}

// ---------------------------------------------------------------------------
// 传输层契约（本 task 暂留在 chat 运行时侧，
// `network-manager-multi-channel` 收编时再判断是否下沉到 network 层）
// ---------------------------------------------------------------------------

/** NetworkManager 收到的原始入站帧信封；network 层不解释业务语义。 */
export interface RawFrameEnvelope {
  /** 连接唯一标识，便于多频道并发连接区分。 */
  connectionId: string;
  /** 频道类型，便于上层按 provider 路由。 */
  channel: ChatProviderKind;
  /** 该帧所属线程 id；NetworkManager 投递时按连接绑定附带，缺省时上层用 connectionId 兜底。 */
  threadId?: string;
  /** 原始帧载荷，NetworkManager 不解释业务语义。 */
  frame: unknown;
  /** 浏览器收到该帧的毫秒时间戳。 */
  receivedAt: number;
}

/** 业务侧发送句柄；只暴露 send / close，不接触底层 WebSocket。 */
export interface NetworkHandle {
  /** 当前连接 id，对应内部 socket 池主键。 */
  connectionId: string;
  /** 发送原始帧，业务层自行决定帧结构。 */
  send(frame: unknown): void;
  /** 主动关闭连接。 */
  close(): void;
}

// ---------------------------------------------------------------------------
// 通用事件模型：历史回放与实时流式帧统一进入状态机前的归一化形态
// ---------------------------------------------------------------------------

/** 通用事件类型。 */
export type ChatEventKind =
  | "history_batch_loaded"
  | "user_message"
  | "assistant_message_started"
  | "assistant_message_delta"
  | "assistant_message_completed"
  | "tool_call_started"
  | "tool_call_delta"
  | "tool_call_completed"
  | "status"
  | "error"
  | "turn_completed";

/** 通用事件：provider 把历史帧 / 实时帧都翻译成本结构后进入状态机。 */
export interface ChatEvent {
  /** 通用事件类型。 */
  kind: ChatEventKind;
  /** 来源 provider。 */
  provider: ChatProviderKind;
  /** 当前线程 id。 */
  threadId: string;
  /** 当前 turn id，用于把同一轮 user / assistant / tool 归并。 */
  turnId: string;
  /** 当前消息 id，文本和状态消息都可用。 */
  messageId?: string;
  /** 当前工具调用 id，仅工具事件使用。 */
  toolCallId?: string;
  /** 事件创建时间，统一毫秒时间戳。 */
  createdAt: number;
  /** 事件正文，结构由 kind 决定。 */
  payload: Record<string, unknown>;
}

/** 历史批量：loadHistory 产出，最终与实时 event 进入同一个时间线状态机。 */
export interface ChatHistoryBatch {
  /** 历史所属线程。 */
  threadId: string;
  /** 历史来源 provider。 */
  provider: ChatProviderKind;
  /** 已按时间排好序的事件列表。 */
  events: ChatEvent[];
  /** 后端是否还有更多历史。 */
  hasMore: boolean;
}

// ---------------------------------------------------------------------------
// 通用时间线实体
// ---------------------------------------------------------------------------

/**
 * 时间线消息角色。
 *
 * `system` / `error` 是非对话型 record，但仍占 `orderedMessageIds` 槽位以保留
 * 时序位置（对照 stores/chat.ts ChatItem 的 system / error kind）。它们不挂 turn
 * 主链路，渲染字段分别落在 `ChatMessageRecord.notice` / `.error`。
 */
export type ChatMessageRole =
  | "user"
  | "assistant"
  | "system"
  | "tool"
  | "error";

/** 消息展示分片，允许文本、附件、工具摘要并存。 */
export type ChatMessagePart =
  | {
      /** 文本片段。 */
      type: "text";
      /** 已归并的文本内容。 */
      text: string;
    }
  | {
      /** 附件片段。 */
      type: "attachment";
      /** 附件内容。 */
      attachment: UserInputAttachment;
    }
  | {
      /** 工具摘要片段。 */
      type: "tool-summary";
      /** 工具名称。 */
      toolName: string;
      /** 工具结果摘要。 */
      summary: string;
    };

/** assistant 消息的 token 用量（对应 stores/chat.ts `AssistantUsage` 页脚）。 */
export interface ChatMessageUsage {
  /** 输入 token。 */
  prompt: number;
  /** 输出 token。 */
  completion: number;
  /** 合计 token。 */
  total: number;
}

/**
 * system 通知 record 载荷（对应 stores/chat.ts ChatItem 的 system kind 全字段）。
 *
 * 落在 `ChatMessageRecord.notice`（role="system"）。同 `(turnId, noticeKey)` 的
 * status 事件覆盖更新而非新增，保留 orderedMessageIds 时序位置。
 */
export interface ChatNoticePayload {
  /** 通知去重键（同 runId 下同 key 覆盖更新）。 */
  noticeKey: string;
  /** 通知来源模块。 */
  source: string;
  /** 标题。 */
  title: string;
  /** 正文。 */
  message: string;
  /** 展开明细行。 */
  details: string[];
  /** 结构化明细（可选）。 */
  detailsData?: Record<string, unknown> | string[] | null;
  /** 状态语义（running / success / warning / error）。 */
  status: string;
  /** 图标语义。 */
  icon: string;
}

/** error record 载荷（对应 stores/chat.ts ChatItem 的 error kind）。 */
export interface ChatErrorPayload {
  /** 错误码。 */
  errorCode: string;
  /** 错误消息。 */
  message: string;
}

/** 时间线消息记录。 */
export interface ChatMessageRecord {
  /** 时间线消息主键。 */
  id: string;
  /** 所属线程。 */
  threadId: string;
  /** 所属 turn。 */
  turnId: string;
  /** 消息角色。 */
  role: ChatMessageRole;
  /** provider 来源。 */
  provider: ChatProviderKind;
  /** 展示内容分片。 */
  parts: ChatMessagePart[];
  /**
   * reasoning（思考过程）分轨内容。与正文 text part 物理分离：
   * provider 把 reasoning.delta 翻成 `assistant_message_delta` 且 payload 带
   * `reasoningDelta`，store 累加到本字段而非污染 `parts`。对应现状 assistant
   * 卡片的独立折叠 reasoning UI。
   */
  reasoning?: string;
  /** assistant token 用量页脚。 */
  usage?: ChatMessageUsage;
  /** role="system" 时的通知载荷。 */
  notice?: ChatNoticePayload;
  /** role="error" 时的错误载荷。 */
  error?: ChatErrorPayload;
  /** 当前消息状态，流式阶段会持续更新。 */
  status: "pending" | "streaming" | "completed" | "failed";
  /** 创建时间（ms）。 */
  createdAt: number;
  /** 最近更新时间（ms）。 */
  updatedAt: number;
}

/**
 * 工具调用记录。富字段对照 stores/chat.ts ChatItem 的 tool kind 全字段，
 * 保证 #2 ChatRenderAdapter 能无损投影回 GenericChatItem。
 */
export interface ChatToolRecord {
  /** 工具调用主键。 */
  id: string;
  /** 所属线程。 */
  threadId: string;
  /** 所属 turn。 */
  turnId: string;
  /** 工具名。 */
  toolName: string;
  /** 工具状态。 */
  status: "running" | "completed" | "failed";
  /** 已累计的输入文本。 */
  inputText: string;
  /** 已累计的输出文本（对应 ToolResult.content）。 */
  outputText: string;
  /** 结构化入参（对应 ChatItem.tool.arguments）。 */
  arguments?: Record<string, unknown>;
  /** 失败时的错误消息（对应 ChatItem.tool.errorMessage）。 */
  errorMessage?: string;
  /** 结构化输出（对应 ToolResult.data / ChatItem.tool.resultData）。 */
  resultData?: Record<string, unknown> | null;
  /**
   * claude 流式 tool_use 子态：true=参数构建中（input_json delta 累积），
   * false=已收到完整 tool_use 帧。对应 ChatItem.tool.pending。
   */
  pending?: boolean;
  /**
   * claude 流式 tool_use 期间累积的原始 input JSON 文本；resolve 后清空。
   * 对应 ChatItem.tool.partialInput。
   */
  partialInput?: string;
  /** 开始时间（ms）。 */
  startedAt: number;
  /** 结束时间（ms）。 */
  finishedAt?: number | null;
}

/**
 * 待解析的 pending tool 占位（claude 特有路径）。
 *
 * 生命周期：`stream_status(phase=tool_calling)` 建占位 → `stream_delta(input_json)`
 * 累积 `partialInput` → `tool_use` resolve 为正式 `ChatToolRecord` 进 toolsById、
 * 同时从 `pendingTools` 移除。generic / codex 不触发此路径。
 */
export interface ChatPendingTool {
  /** 工具调用 id（与最终 ChatToolRecord.id 同源）。 */
  id: string;
  /** 所属线程。 */
  threadId: string;
  /** 所属 turn。 */
  turnId: string;
  /** 已累积的原始 input JSON 文本。 */
  partialInput: string;
  /** 占位创建时间（ms）。 */
  startedAt: number;
}

/** turn 级状态。 */
export interface ChatTurnState {
  /** turn 主键。 */
  id: string;
  /** 所属线程。 */
  threadId: string;
  /** provider 来源。 */
  provider: ChatProviderKind;
  /** 当前轮次阶段。 */
  phase: "ready" | "sending" | "streaming" | "completed" | "failed";
  /** 用户消息 id。 */
  userMessageId?: string | null;
  /** 助手消息 id。 */
  assistantMessageId?: string | null;
  /** 工具调用顺序列表。 */
  toolCallIds: string[];
}

/** 前端运行时唯一事实源：当前线程的完整时间线状态。 */
export interface ChatTimelineState {
  /** 当前线程 id。 */
  threadId: string;
  /** 是否已经完成历史首屏加载。 */
  historyLoaded: boolean;
  /** 时间线展示顺序。 */
  orderedMessageIds: string[];
  /** 消息字典。 */
  messagesById: Record<string, ChatMessageRecord>;
  /** 工具调用字典。 */
  toolsById: Record<string, ChatToolRecord>;
  /**
   * 未 resolve 的 pending tool 占位字典（claude 流式 tool_use 期间）。
   * resolve 后从此移除、写入 `toolsById`。
   */
  pendingTools: Record<string, ChatPendingTool>;
  /** turn 字典。 */
  turnsById: Record<string, ChatTurnState>;
  /** 当前是否有流式消息进行中。 */
  activeStreamingTurnId?: string | null;
}

// ---------------------------------------------------------------------------
// 通用渲染视图模型（ChatRenderAdapter 真源，#2）
// ---------------------------------------------------------------------------

/**
 * 通用渲染视图项（spec 04 真源）。
 *
 * `ChatRenderAdapter.toViewModel(state)` 把 `ChatTimelineState`（数据真源）投影成
 * 一串与频道无关的 `ChatViewItem`，再由各频道适配层翻回各自现有 RenderItem。
 * 未来换 Streamdown 等渲染层只动核心层，适配层 / 视图不动。
 *
 * 表达力覆盖三类现状 ChatItem：
 * - `kind:"message"` —— user / assistant（含 reasoning / usage / streaming）。
 * - `kind:"tool"` —— 工具调用富字段（toolName / arguments / ok / errorMessage /
 *   result / resultData / partialInput / pending），从 `toolsById` 回查。
 * - `kind:"notice"` —— system 通知（对应 stores/chat.ts system kind）。
 * - `kind:"error"` —— 错误（对应 stores/chat.ts error kind）。
 *
 * 每项都带 `turn` / `runId`，让适配层无损翻出 GenericChatItem 的复合 key
 * （turnId 形如 `${threadId}-turn-${turn}` 解出 turn；否则 turnId = run_id）。
 */
export type ChatViewItem =
  | {
      /** 对话型消息（user / assistant）。 */
      kind: "message";
      /** 时间线消息主键。 */
      id: string;
      /** user / assistant 角色（notice / error / tool 走各自 kind）。 */
      role: "user" | "assistant";
      /** 所属线程。 */
      threadId: string;
      /** 解析自 turnId 的 turn 序号（无法解析时为 0）。 */
      turn: number;
      /** 解析自 turnId 的 run 编号（历史 / 无 run_id 时为空串）。 */
      runId: string;
      /** 正文文本（assistant/user 的 text part 合并）。 */
      content: string;
      /** assistant reasoning 分轨内容。 */
      reasoning?: string;
      /** assistant token 用量页脚。 */
      usage?: ChatMessageUsage;
      /** 用户附件（从 attachment part 还原）。 */
      attachments?: UserInputAttachment[];
      /** 是否流式进行中（status==="streaming"）。 */
      streaming: boolean;
      /** 创建时间（ms）。 */
      timestampMs: number;
    }
  | {
      /** 工具调用项。 */
      kind: "tool";
      /** 工具调用主键（= callId）。 */
      id: string;
      /** 所属线程。 */
      threadId: string;
      /** 解析自 turnId 的 turn 序号。 */
      turn: number;
      /** 解析自 turnId 的 run 编号。 */
      runId: string;
      /** 工具名。 */
      toolName: string;
      /** 工具调用 id。 */
      callId: string;
      /** 结构化入参。 */
      arguments: Record<string, unknown>;
      /** 成功标志：true=成功，false=失败，null=进行中。 */
      ok: boolean | null;
      /** 失败错误消息。 */
      errorMessage?: string;
      /** 工具产出文本（ToolResult.content）。 */
      result?: string;
      /** 工具产出结构化数据（ToolResult.data）。 */
      resultData?: Record<string, unknown> | null;
      /** claude 流式 tool_use 期间累积的原始 input JSON 文本。 */
      partialInput?: string;
      /** true=参数构建中（pending tool 占位），false=已 resolve 正式 tool。 */
      pending?: boolean;
      /** 创建时间（ms）。 */
      timestampMs: number;
    }
  | {
      /** system 通知项（对应 stores/chat.ts system kind）。 */
      kind: "notice";
      /** 时间线消息主键。 */
      id: string;
      /** 所属线程。 */
      threadId: string;
      /** 解析自 turnId 的 run 编号。 */
      runId: string;
      /** 通知去重键。 */
      noticeKey: string;
      /** 通知来源模块。 */
      source: string;
      /** 标题。 */
      title: string;
      /** 正文。 */
      message: string;
      /** 展开明细行。 */
      details: string[];
      /** 结构化明细。 */
      detailsData?: Record<string, unknown> | string[] | null;
      /** 状态语义（已归一到 SystemChatStatus 取值）。 */
      status: string;
      /** 图标语义。 */
      icon: string;
      /** 创建时间（ms）。 */
      timestampMs: number;
    }
  | {
      /** error 项（对应 stores/chat.ts error kind）。 */
      kind: "error";
      /** 时间线消息主键。 */
      id: string;
      /** 所属线程。 */
      threadId: string;
      /** 错误消息。 */
      message: string;
      /** 错误码。 */
      errorCode: string;
      /** 创建时间（ms）。 */
      timestampMs: number;
    };

/**
 * 通用渲染视图模型（ChatRenderAdapter 核心层产物）。
 *
 * 纯函数 `toViewModel(state)` 的输出，可在组件侧 `useMemo` 缓存；
 * 适配层 `toGenericRenderItems` / `toClaudeRenderItems` / `toCodexRenderItems`
 * 消费它翻回各频道现有 RenderItem。
 */
export interface ChatViewModel {
  /** 按 `orderedMessageIds` 顺序排列的视图项。 */
  items: ChatViewItem[];
  /** 当前是否有流式消息进行中（= activeStreamingTurnId 非空）。 */
  isStreaming: boolean;
  /** 是否已完成历史首屏加载。 */
  historyLoaded: boolean;
}

// ---------------------------------------------------------------------------
// 模块接口契约
// ---------------------------------------------------------------------------

/**
 * provider 适配层契约：保留频道独有协议和历史接口，把差异封装在频道侧。
 *
 * Generic 没有 provider session 概念，`checkSessionStatus` 仍实现该方法，
 * 但固定返回 `{ active: false }`（见 `02-module-breakdown.md` 三频道能力对照）。
 */
export interface ChatProvider {
  /** 当前 provider 标识。 */
  readonly provider: ChatProviderKind;
  /** 把公共发送请求翻译为 provider 自己的 wire 协议。 */
  send(handle: NetworkHandle, request: SendRequest): Promise<void>;
  /** 加载 provider 历史。 */
  loadHistory(request: HistoryLoadRequest): Promise<ChatHistoryBatch>;
  /** 处理原始入站帧，翻译为通用事件列表。 */
  mapInboundFrame(envelope: RawFrameEnvelope): ChatEvent[];
  /** 打断当前 provider 会话。 */
  interrupt(handle: NetworkHandle, request: InterruptRequest): Promise<void>;
  /** 提交 ChoicePanel 结构化选择结果；支持该能力的 provider 实现。 */
  submitChoice?(handle: NetworkHandle, request: ChoiceSubmitRequest): Promise<void>;
  /** 查询 provider session 状态。Generic 返回固定的「无 session」结果。 */
  checkSessionStatus(request: SessionStatusRequest): Promise<SessionStatus>;
}

/** ChatManager 对外统一暴露的能力。 */
export interface ChatManagerApi {
  /** 发送一条用户消息，内部负责首次创建和 provider 分发。 */
  sendMessage(request: SendRequest): Promise<void>;
  /** 加载当前 thread 的历史消息。 */
  loadHistory(request: HistoryLoadRequest): Promise<void>;
  /** 把 NetworkManager 收到的原始帧灌入聊天状态机。 */
  ingestFrame(envelope: RawFrameEnvelope): void;
  /** 提交 ChoicePanel 结构化选择结果。 */
  submitChoice(request: ChoiceSubmitRequest): Promise<void>;
  /** 打断当前会话执行。 */
  interrupt(request: InterruptRequest): Promise<void>;
  /** 查询 provider 会话状态。 */
  checkSessionStatus(request: SessionStatusRequest): Promise<SessionStatus>;
}

/**
 * 时间线状态机契约。
 *
 * 命名注意：接口叫 `ChatTimelineStoreApi`，实现类叫 `ChatTimelineStore`（见 #4），
 * 避免同名 interface 与 class 在同作用域冲突。
 */
export interface ChatTimelineStoreApi {
  /** 历史批量灌入（内部逐条处理，末尾单次 notify）。 */
  applyHistory(batch: ChatHistoryBatch): void;
  /** 实时事件灌入（单次调用末尾单次 notify）。 */
  applyEvent(event: ChatEvent): void;
  /** 读取当前稳定时间线（保留作旧 API 别名，等价 getSnapshot）。 */
  snapshot(): ChatTimelineState;
  /**
   * useSyncExternalStore 读取入口：只返回稳定 `this.state` 引用，
   * state 未变时返回同一对象。禁止在此 new 任何对象/做投影（React #185）。
   */
  getSnapshot(): ChatTimelineState;
  /**
   * useSyncExternalStore 订阅入口：注册变更监听，返回 unsubscribe。
   * 状态变更（applyEvent / applyHistory 末尾）批量触发一次。
   */
  subscribe(listener: () => void): () => void;
  /** 清理指定 thread 的临时流式状态。 */
  resetThread(threadId: string): void;
}
