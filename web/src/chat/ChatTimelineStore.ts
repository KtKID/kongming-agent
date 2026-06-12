/**
 * chat-receive-side-unify #1 · ChatTimelineStore（状态机地基）
 *
 * 统一时间线状态机：把历史 `ChatHistoryBatch` 与实时 `ChatEvent` 归并成单一
 * `ChatTimelineState`（前端运行时唯一事实源）。provider 只生产事件，本 store
 * 负责 turn 合并 / 流式文本 buffer / reasoning 分轨 / tool 状态聚合（含 claude
 * pending tool）/ system & error record / 历史展开 / 幂等去重 / 顺序维护。
 *
 * 命名：接口是 `ChatTimelineStoreApi`（见 chat/types.ts），实现类是本 `ChatTimelineStore`。
 *
 * ## 设计目标（不可偏离）
 *
 * `ChatTimelineState` 必须能无损重建出等价于 stores/chat.ts `ChatItem[]` 的数据，
 * 供 #2 ChatRenderAdapter 投影回 `GenericChatItem` 保证 UI 零回归。
 *
 * ## 响应式 + notify 批量（P0）
 *
 * - `subscribe` / `getSnapshot` 配套 useSyncExternalStore。
 * - `getSnapshot` 只返回 `this.state`（稳定引用），禁止 new 对象/投影（React #185）。
 * - 所有内部 helper 写 `this.state` 但**不 notify**；只有 `applyEvent` 末尾、
 *   `applyHistory` 全部处理完末尾才 `notify()` 一次（批量合并）。
 *
 * ## 幂等去重（P0）
 *
 * history 展开的 record id 与实时 `messageKey` 同源：history 用
 * `${threadId}-turn-${turn}` 作 turnId（= generic 无 run_id 时实时 turnId），
 * messageKey = `event.messageId ?? `${turnId}:${role}``，从而同 messageId / 同
 * (turnId, role) 的历史帧 + 实时帧合并后不重复。已有 streaming/completed 的
 * assistant 时跳过历史 assistant（不覆盖实时累积内容）。
 */
import type {
  ChatTimelineStoreApi,
  ChatEvent,
  ChatHistoryBatch,
  ChatTimelineState,
  ChatMessageRecord,
  ChatMessagePart,
  ChatToolRecord,
  ChatPendingTool,
  ChatNoticePayload,
  ChatErrorPayload,
  ChatTurnState,
  ChatMessageUsage,
  ChatProviderKind,
  UserInputAttachment,
} from "@/chat/types";
import type { HistoryMessageDTO } from "@/protocol";

export class ChatTimelineStore implements ChatTimelineStoreApi {
  private state: ChatTimelineState;
  private listeners = new Set<() => void>();

  constructor(threadId: string) {
    this.state = ChatTimelineStore.emptyState(threadId);
  }

  static emptyState(threadId: string): ChatTimelineState {
    return {
      threadId,
      historyLoaded: false,
      orderedMessageIds: [],
      messagesById: {},
      toolsById: {},
      pendingTools: {},
      turnsById: {},
      activeStreamingTurnId: null,
    };
  }

  // ---- 响应式订阅（P0：getSnapshot 纯净 + notify 批量）----

  /** 注册变更监听，返回 unsubscribe。 */
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /**
   * useSyncExternalStore 读取入口：只返回稳定 `this.state` 引用。
   * 禁止在此 new 对象/做投影，否则相同 state 返回不同引用 → 无限重渲染。
   */
  getSnapshot(): ChatTimelineState {
    return this.state;
  }

  /** 旧 API 别名，等价 getSnapshot。 */
  snapshot(): ChatTimelineState {
    return this.state;
  }

  private notify(): void {
    for (const listener of this.listeners) listener();
  }

  resetThread(threadId: string): void {
    this.state = ChatTimelineStore.emptyState(threadId);
    this.notify();
  }

  applyHistory(batch: ChatHistoryBatch): void {
    // 历史批量：逐条复用 applyEventSilent（不 notify），全部处理完末尾单次 notify。
    for (const event of batch.events) {
      this.applyEventSilent(event);
    }
    this.state = { ...this.state, historyLoaded: true };
    this.notify();
  }

  applyEvent(event: ChatEvent): void {
    // 单次实时事件：内部 helper 全静默写 state，末尾单次 notify。
    this.applyEventSilent(event);
    this.notify();
  }

  /** 实际归并逻辑：写 `this.state`，**不 notify**。 */
  private applyEventSilent(event: ChatEvent): void {
    switch (event.kind) {
      case "history_batch_loaded":
        this.expandHistory(event);
        return;
      case "user_message":
        this.upsertMessage(event, "user", "completed", String(event.payload.text ?? ""));
        this.attachUserAttachments(event);
        return;
      case "assistant_message_started":
        this.ensureTurn(event);
        this.upsertMessage(event, "assistant", "streaming", "");
        this.state = { ...this.state, activeStreamingTurnId: event.turnId };
        return;
      case "assistant_message_delta":
        this.appendAssistantDelta(event);
        return;
      case "assistant_message_completed":
        this.completeAssistant(event);
        return;
      case "tool_call_started":
        this.startTool(event);
        return;
      case "tool_call_delta":
        this.appendToolDelta(event);
        return;
      case "tool_call_completed":
        this.completeTool(event);
        return;
      case "turn_completed":
        this.completeTurn(event);
        return;
      case "status":
        this.upsertNotice(event);
        return;
      case "error":
        this.upsertError(event);
        return;
      default:
        return;
    }
  }

  // ---- turn 归并 ----

  private ensureTurn(event: ChatEvent): ChatTurnState {
    const existing = this.state.turnsById[event.turnId];
    if (existing) return existing;
    const turn: ChatTurnState = {
      id: event.turnId,
      threadId: event.threadId,
      provider: event.provider as ChatProviderKind,
      phase: "streaming",
      toolCallIds: [],
    };
    this.state = {
      ...this.state,
      turnsById: { ...this.state.turnsById, [turn.id]: turn },
    };
    return turn;
  }

  private patchTurn(turnId: string, patch: Partial<ChatTurnState>): void {
    const turn = this.state.turnsById[turnId];
    if (!turn) return;
    this.state = {
      ...this.state,
      turnsById: { ...this.state.turnsById, [turnId]: { ...turn, ...patch } },
    };
  }

  /** 消息主键：优先 event.messageId，回退到 `${turnId}:${role}`（一轮一条 role）。 */
  private messageKey(event: ChatEvent, role: ChatMessageRecord["role"]): string {
    return event.messageId ?? `${event.turnId}:${role}`;
  }

  private upsertMessage(
    event: ChatEvent,
    role: ChatMessageRecord["role"],
    status: ChatMessageRecord["status"],
    text: string,
  ): void {
    const id = this.messageKey(event, role);
    const prev = this.state.messagesById[id];
    const record: ChatMessageRecord = {
      id,
      threadId: event.threadId,
      turnId: event.turnId,
      role,
      provider: event.provider as ChatProviderKind,
      parts: [{ type: "text", text }],
      reasoning: prev?.reasoning,
      usage: prev?.usage,
      status,
      createdAt: prev?.createdAt ?? event.createdAt,
      updatedAt: event.createdAt,
    };
    this.writeMessage(id, record, !prev);
    if (role === "user") this.patchTurn(event.turnId, { userMessageId: id });
    if (role === "assistant") this.patchTurn(event.turnId, { assistantMessageId: id });
  }

  /**
   * 把 user_message 事件携带的 attachments 落成 `attachment` parts。
   *
   * `upsertMessage` 只写文本 part；附件单独在此追加，让 ChatRenderAdapter
   * 的 `attachmentsOf` 能投影回 GenericChatItem.user.attachments（缩略图零回归）。
   * payload 无 attachments / 空数组时不动 parts，保持纯文本消息不变。
   */
  private attachUserAttachments(event: ChatEvent): void {
    const raw = event.payload.attachments;
    if (!Array.isArray(raw) || raw.length === 0) return;
    const id = this.messageKey(event, "user");
    const prev = this.state.messagesById[id];
    if (!prev) return;
    const attachmentParts: ChatMessagePart[] = (raw as UserInputAttachment[]).map(
      (attachment) => ({ type: "attachment", attachment }),
    );
    // 文本 part 在前、附件 part 在后；幂等：同一条消息重复处理时去掉旧附件 part。
    const textParts = prev.parts.filter((p) => p.type === "text");
    this.patchMessage(id, { parts: [...textParts, ...attachmentParts] });
  }

  /** 写入消息 + 维护 orderedMessageIds（首见追加，已存在原位更新）。 */
  private writeMessage(id: string, record: ChatMessageRecord, isNew: boolean): void {
    const orderedMessageIds = isNew
      ? [...this.state.orderedMessageIds, id]
      : this.state.orderedMessageIds;
    this.state = {
      ...this.state,
      messagesById: { ...this.state.messagesById, [id]: record },
      orderedMessageIds,
    };
  }

  private appendAssistantDelta(event: ChatEvent): void {
    const id = this.messageKey(event, "assistant");
    const prev = this.state.messagesById[id];
    const delta = String(event.payload.delta ?? "");
    const reasoningDelta = String(event.payload.reasoningDelta ?? "");
    if (!prev) {
      // 没有 started 先到（乱序 / 纯 reasoning）：补建 streaming assistant 消息。
      this.ensureTurn(event);
      this.upsertMessage(event, "assistant", "streaming", delta);
      if (reasoningDelta) {
        this.patchMessage(id, { reasoning: (this.state.messagesById[id]?.reasoning ?? "") + reasoningDelta });
      }
      return;
    }
    const patch: Partial<ChatMessageRecord> = { status: "streaming", updatedAt: event.createdAt };
    if (delta) patch.parts = mergeText(prev.parts, delta);
    if (reasoningDelta) patch.reasoning = (prev.reasoning ?? "") + reasoningDelta;
    this.patchMessage(id, patch);
  }

  private completeAssistant(event: ChatEvent): void {
    const id = this.messageKey(event, "assistant");
    const prev = this.state.messagesById[id];
    const finalText = event.payload.content;
    const usage = this.parseUsage(event.payload.usage);
    if (!prev) {
      this.ensureTurn(event);
      this.upsertMessage(event, "assistant", "completed", String(finalText ?? ""));
      if (usage) this.patchMessage(id, { usage });
    } else {
      // assistant.final 带完整内容时以它为准，否则保留已累计的 delta。
      const parts =
        typeof finalText === "string" && finalText.length > 0
          ? [{ type: "text" as const, text: finalText }]
          : prev.parts;
      const patch: Partial<ChatMessageRecord> = {
        parts,
        status: "completed",
        updatedAt: event.createdAt,
      };
      if (usage) patch.usage = usage;
      this.patchMessage(id, patch);
    }
    if (this.state.activeStreamingTurnId === event.turnId) {
      this.state = { ...this.state, activeStreamingTurnId: null };
    }
  }

  private patchMessage(id: string, patch: Partial<ChatMessageRecord>): void {
    const prev = this.state.messagesById[id];
    if (!prev) return;
    this.state = {
      ...this.state,
      messagesById: { ...this.state.messagesById, [id]: { ...prev, ...patch } },
    };
  }

  private parseUsage(raw: unknown): ChatMessageUsage | undefined {
    if (!raw || typeof raw !== "object") return undefined;
    const u = raw as Record<string, unknown>;
    if (
      typeof u.prompt === "number" &&
      typeof u.completion === "number" &&
      typeof u.total === "number"
    ) {
      return { prompt: u.prompt, completion: u.completion, total: u.total };
    }
    return undefined;
  }

  // ---- tool（含 claude pending tool 路径）----

  private startTool(event: ChatEvent): void {
    if (!event.toolCallId) return;
    this.ensureTurn(event);
    const id = event.toolCallId;

    // claude pending 路径：stream_status(phase=tool_calling) → 建占位，不入 toolsById。
    if (event.payload.pending === true) {
      if (this.state.pendingTools[id] || this.state.toolsById[id]) return;
      const pending: ChatPendingTool = {
        id,
        threadId: event.threadId,
        turnId: event.turnId,
        partialInput: "",
        startedAt: event.createdAt,
      };
      this.state = {
        ...this.state,
        pendingTools: { ...this.state.pendingTools, [id]: pending },
      };
      return;
    }

    // 正式 tool_use（claude resolve，或 generic/codex 直接 start）。
    const pendingPartial = this.state.pendingTools[id]?.partialInput;
    const args =
      event.payload.arguments && typeof event.payload.arguments === "object"
        ? (event.payload.arguments as Record<string, unknown>)
        : undefined;
    const tool: ChatToolRecord = {
      id,
      threadId: event.threadId,
      turnId: event.turnId,
      toolName: String(event.payload.toolName ?? "unknown"),
      status: "running",
      inputText:
        typeof event.payload.arguments === "string"
          ? event.payload.arguments
          : JSON.stringify(event.payload.arguments ?? {}),
      outputText: "",
      arguments: args,
      pending: false,
      partialInput: pendingPartial,
      startedAt: event.createdAt,
    };
    // resolve：从 pendingTools 移除。
    const nextPending = { ...this.state.pendingTools };
    delete nextPending[id];
    this.state = {
      ...this.state,
      toolsById: { ...this.state.toolsById, [id]: tool },
      pendingTools: nextPending,
    };
    const turn = this.state.turnsById[event.turnId];
    if (turn && !turn.toolCallIds.includes(id)) {
      this.patchTurn(event.turnId, { toolCallIds: [...turn.toolCallIds, id] });
    }
    // tool 占一个 orderedMessageIds 槽位（key=callId，与 history 同源 → 幂等），
    // 保证无损重建：stores/chat.ts 实时 appendToolStart 也往列表加一条 tool item。
    if (!this.state.messagesById[id]) {
      this.writeMessage(
        id,
        {
          id,
          threadId: event.threadId,
          turnId: event.turnId,
          role: "tool",
          provider: event.provider as ChatProviderKind,
          parts: [],
          status: "streaming",
          createdAt: event.createdAt,
          updatedAt: event.createdAt,
        },
        true,
      );
    }
  }

  private appendToolDelta(event: ChatEvent): void {
    if (!event.toolCallId) return;
    const id = event.toolCallId;

    // claude pending 路径：stream_delta(input_json) → 累积 partialInput。
    const partialDelta = event.payload.partialInputDelta;
    if (typeof partialDelta === "string") {
      const pending = this.state.pendingTools[id];
      if (pending) {
        this.state = {
          ...this.state,
          pendingTools: {
            ...this.state.pendingTools,
            [id]: { ...pending, partialInput: pending.partialInput + partialDelta },
          },
        };
        return;
      }
      // 已 resolve 进 toolsById 后仍来 input_json delta：累积到 tool.partialInput。
      const tool = this.state.toolsById[id];
      if (tool) {
        this.state = {
          ...this.state,
          toolsById: {
            ...this.state.toolsById,
            [id]: { ...tool, partialInput: (tool.partialInput ?? "") + partialDelta },
          },
        };
      }
      return;
    }

    // generic/codex 输出 delta。
    const prev = this.state.toolsById[id];
    if (!prev) return;
    const outDelta = String(event.payload.outputDelta ?? event.payload.delta ?? "");
    this.state = {
      ...this.state,
      toolsById: {
        ...this.state.toolsById,
        [id]: { ...prev, outputText: prev.outputText + outDelta },
      },
    };
  }

  private completeTool(event: ChatEvent): void {
    if (!event.toolCallId) return;
    const prev = this.state.toolsById[event.toolCallId];
    if (!prev) return;
    const ok = event.payload.ok !== false && !event.payload.isError;
    const out =
      typeof event.payload.content === "string" ? event.payload.content : prev.outputText;
    const resultData =
      event.payload.data && typeof event.payload.data === "object"
        ? (event.payload.data as Record<string, unknown>)
        : event.payload.data === null
          ? null
          : prev.resultData;
    const errorMessage =
      typeof event.payload.errorMessage === "string"
        ? event.payload.errorMessage
        : prev.errorMessage;
    this.state = {
      ...this.state,
      toolsById: {
        ...this.state.toolsById,
        [prev.id]: {
          ...prev,
          status: ok ? "completed" : "failed",
          outputText: out,
          resultData,
          errorMessage,
          pending: false,
          finishedAt: event.createdAt,
        },
      },
    };
  }

  private completeTurn(event: ChatEvent): void {
    this.ensureTurn(event);
    this.patchTurn(event.turnId, { phase: "completed" });
    if (this.state.activeStreamingTurnId === event.turnId) {
      this.state = { ...this.state, activeStreamingTurnId: null };
    }
  }

  // ---- system notice / error record（占 orderedMessageIds 时序位置）----

  private upsertNotice(event: ChatEvent): void {
    const p = event.payload;
    const noticeKey = String(p.noticeKey ?? "notice");
    // 同 (turnId, noticeKey) 覆盖更新；id 含 turnId 保证不同 run 隔离。
    const id = `notice:${event.turnId}:${noticeKey}`;
    const prev = this.state.messagesById[id];
    const notice: ChatNoticePayload = {
      noticeKey,
      source: String(p.source ?? ""),
      title: String(p.title ?? ""),
      message: String(p.message ?? ""),
      details: Array.isArray(p.details) ? p.details.map((d) => String(d)) : [],
      detailsData:
        Array.isArray(p.details) || (p.details && typeof p.details === "object")
          ? (p.details as Record<string, unknown> | string[])
          : null,
      status: String(p.status ?? ""),
      icon: String(p.icon ?? p.status ?? ""),
    };
    const record: ChatMessageRecord = {
      id,
      threadId: event.threadId,
      turnId: event.turnId,
      role: "system",
      provider: event.provider as ChatProviderKind,
      parts: [],
      notice,
      status: "completed",
      createdAt: prev?.createdAt ?? event.createdAt,
      updatedAt: event.createdAt,
    };
    this.writeMessage(id, record, !prev);
  }

  private upsertError(event: ChatEvent): void {
    const p = event.payload;
    // error 不去重（每条都独立展示）：id 含 createdAt 区分。
    const id = `error:${event.turnId}:${event.createdAt}`;
    const error: ChatErrorPayload = {
      errorCode: String(p.errorCode ?? ""),
      message: String(p.message ?? ""),
    };
    const record: ChatMessageRecord = {
      id,
      threadId: event.threadId,
      turnId: event.turnId,
      role: "error",
      provider: event.provider as ChatProviderKind,
      parts: [],
      error,
      status: "completed",
      createdAt: event.createdAt,
      updatedAt: event.createdAt,
    };
    this.writeMessage(id, record, !this.state.messagesById[id]);
  }

  // ---- history 真实展开（P0）----

  /**
   * history_batch_loaded 展开：把 HistoryMessageDTO[] 展开成 records。
   * 参考 stores/chat.ts `setHistory`。id 与实时 messageKey 同源以幂等去重。
   */
  private expandHistory(event: ChatEvent): void {
    const messages = event.payload.messages;
    if (!Array.isArray(messages)) return;
    for (const m of messages as HistoryMessageDTO[]) {
      this.expandHistoryMessage(event.provider as ChatProviderKind, event.threadId, m);
    }
  }

  private expandHistoryMessage(
    provider: ChatProviderKind,
    threadId: string,
    m: HistoryMessageDTO,
  ): void {
    // history 无 run_id：turnId 走 `${threadId}-turn-${turn}`（= generic 无 run_id
    // 实时 turnId 同源），messageKey = `${turnId}:${role}`。
    const turnId = `${threadId}-turn-${m.turn}`;
    const at = m.timestamp_ms;

    if (m.role === "user") {
      const id = `${turnId}:user`;
      if (this.state.messagesById[id]) return; // 已有同源 → 幂等跳过
      this.ensureHistoryTurn(provider, threadId, turnId);
      const parts: ChatMessagePart[] = [{ type: "text", text: m.content }];
      this.writeMessage(
        id,
        {
          id,
          threadId,
          turnId,
          role: "user",
          provider,
          parts,
          status: "completed",
          createdAt: at,
          updatedAt: at,
        },
        true,
      );
      this.patchTurn(turnId, { userMessageId: id });
      return;
    }

    if (m.role === "assistant") {
      const id = `${turnId}:assistant`;
      const prev = this.state.messagesById[id];
      // 已有 streaming/completed assistant（实时已建）→ 跳过历史，不覆盖实时内容。
      if (prev) return;
      this.ensureHistoryTurn(provider, threadId, turnId);
      this.writeMessage(
        id,
        {
          id,
          threadId,
          turnId,
          role: "assistant",
          provider,
          parts: [{ type: "text", text: m.content }],
          status: "completed",
          createdAt: at,
          updatedAt: at,
        },
        true,
      );
      this.patchTurn(turnId, { assistantMessageId: id });
      return;
    }

    // tool
    const callId = m.tool_call_id ?? `${turnId}:tool`;
    if (this.state.messagesById[callId]) return; // 幂等
    this.ensureHistoryTurn(provider, threadId, turnId);
    const ok = m.ok ?? true;
    const tool: ChatToolRecord = {
      id: callId,
      threadId,
      turnId,
      toolName: m.tool_name ?? "",
      status: ok ? "completed" : "failed",
      inputText: "",
      outputText: m.content,
      arguments: {},
      errorMessage: m.error_message ?? undefined,
      resultData: m.data ?? null,
      pending: false,
      startedAt: at,
      finishedAt: at,
    };
    this.state = {
      ...this.state,
      toolsById: { ...this.state.toolsById, [callId]: tool },
    };
    const turn = this.state.turnsById[turnId];
    if (turn && !turn.toolCallIds.includes(callId)) {
      this.patchTurn(turnId, { toolCallIds: [...turn.toolCallIds, callId] });
    }
    // tool 也占一个 orderedMessageIds 槽位（role="tool" 的轻 record，承载顺序 + 关联）。
    this.writeMessage(
      callId,
      {
        id: callId,
        threadId,
        turnId,
        role: "tool",
        provider,
        parts: [],
        status: ok ? "completed" : "failed",
        createdAt: at,
        updatedAt: at,
      },
      true,
    );
  }

  private ensureHistoryTurn(
    provider: ChatProviderKind,
    threadId: string,
    turnId: string,
  ): void {
    if (this.state.turnsById[turnId]) return;
    const turn: ChatTurnState = {
      id: turnId,
      threadId,
      provider,
      phase: "completed",
      toolCallIds: [],
    };
    this.state = {
      ...this.state,
      turnsById: { ...this.state.turnsById, [turnId]: turn },
    };
  }
}

/** 把 delta 累加进消息的文本片段（维护单个 text part）。 */
function mergeText(parts: ChatMessagePart[], delta: string): ChatMessagePart[] {
  const idx = parts.findIndex((p) => p.type === "text");
  if (idx < 0) return [...parts, { type: "text", text: delta }];
  const next = [...parts];
  const cur = next[idx];
  if (cur.type === "text") next[idx] = { type: "text", text: cur.text + delta };
  return next;
}
