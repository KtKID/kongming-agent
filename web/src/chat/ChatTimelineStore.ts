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
 * history 展开的 record id 使用 `NormalizedMessage.id`，从而同 messageId 的
 * 历史帧 + 实时帧合并后不重复。
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
  ConversationReferenceDTO,
  UserInputAttachment,
  ChatTimelineStoreDependencies,
  StreamingCancelHandle,
  StreamingRenderPolicy,
  StreamingRenderScheduler,
  StreamingVisibilitySource,
} from "@/chat/types";
import type { NormalizedMessage } from "@/protocol";

/** 流式渲染的默认资源预算；数值同时受 capability spec 覆盖。 */
export const DEFAULT_STREAMING_RENDER_POLICY: StreamingRenderPolicy = Object.freeze({
  foregroundFlushIntervalMs: 50,
  backgroundFlushIntervalMs: 100,
  maxBufferedEventsPerTurn: 256,
  maxBufferedCharsPerTurn: 32 * 1024,
  maxBufferedTurnsPerStore: 32,
  maxBufferedEventsPerStore: 2048,
  maxBufferedCharsPerStore: 256 * 1024,
});

type TurnKey = string;

interface PendingTurn {
  readonly threadId: string;
  readonly runId: string;
  readonly turnId: string;
  readonly turn: number | null;
  readonly provider: ChatProviderKind;
  readonly messageId?: string;
  readonly createdAt: number;
  contentParts: string[];
  reasoningParts: string[];
  eventCount: number;
  charCount: number;
  lastCreatedAt: number;
}

interface ScheduledFlush {
  readonly generation: number;
  readonly handle: StreamingCancelHandle;
}

const browserScheduler: StreamingRenderScheduler = {
  scheduleForeground(delayMs, callback) {
    let cancelled = false;
    let frame: number | null = null;
    const timeout = globalThis.setTimeout(() => {
      if (cancelled) return;
      if (typeof requestAnimationFrame === "function") {
        frame = requestAnimationFrame(() => {
          if (!cancelled) callback();
        });
        return;
      }
      callback();
    }, delayMs);
    return {
      cancel() {
        cancelled = true;
        globalThis.clearTimeout(timeout);
        if (frame !== null && typeof cancelAnimationFrame === "function") {
          cancelAnimationFrame(frame);
        }
      },
    };
  },
  scheduleBackground(delayMs, callback) {
    let cancelled = false;
    const timeout = globalThis.setTimeout(() => {
      if (!cancelled) callback();
    }, delayMs);
    return {
      cancel() {
        cancelled = true;
        globalThis.clearTimeout(timeout);
      },
    };
  },
};

const browserVisibility: StreamingVisibilitySource = {
  isHidden: () => typeof document !== "undefined" && document.hidden,
  subscribe(listener) {
    if (typeof document === "undefined") return () => {};
    document.addEventListener("visibilitychange", listener);
    return () => document.removeEventListener("visibilitychange", listener);
  },
};

export class ChatTimelineStore implements ChatTimelineStoreApi {
  private state: ChatTimelineState;
  private listeners = new Set<() => void>();
  private readonly policy: StreamingRenderPolicy;
  private readonly now: () => number;
  private readonly scheduler: StreamingRenderScheduler;
  private readonly visibility: StreamingVisibilitySource;
  private readonly unsubscribeVisibility: () => void;
  private readonly pendingByTurn = new Map<TurnKey, PendingTurn>();
  private totalPendingEvents = 0;
  private totalPendingChars = 0;
  private scheduledFlush: ScheduledFlush | null = null;
  private schedulerGeneration = 0;
  private lastVisibleCommitAt: number | null = null;
  private disposed = false;

  constructor(threadId: string, dependencies: ChatTimelineStoreDependencies = {}) {
    this.state = ChatTimelineStore.emptyState(threadId);
    this.policy = Object.freeze({ ...DEFAULT_STREAMING_RENDER_POLICY, ...dependencies.policy });
    this.now = dependencies.now ?? Date.now;
    this.scheduler = dependencies.scheduler ?? browserScheduler;
    this.visibility = dependencies.visibility ?? browserVisibility;
    this.unsubscribeVisibility = this.visibility.subscribe(() => this.handleVisibilityChange());
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
    this.cancelScheduledFlush();
    this.clearPending();
    this.state = ChatTimelineStore.emptyState(threadId);
    this.notify();
  }

  applyHistory(batch: ChatHistoryBatch): void {
    if (this.disposed) return;
    const before = this.state;
    this.cancelScheduledFlush();
    this.flushAllPendingSilent("history");
    // 历史批量绝不建调度：delta 直接写入累积消息，其他事件复用静默归并。
    for (const event of batch.events) {
      this.applyHistoryEventSilent(event);
    }
    this.state = { ...this.state, historyLoaded: true };
    if (this.state !== before) this.notify();
  }

  applyEvent(event: ChatEvent): void {
    if (this.disposed) return;
    const category = classifyEvent(event);
    switch (category) {
      case "delta":
        this.bufferAssistantDelta(event);
        return;
      case "terminal":
        this.applyTerminal(event);
        return;
      case "ordering":
        this.applyOrderingBoundary(event);
        return;
      case "stream-failure":
        this.applyStreamFailure(event);
        return;
      case "history":
        this.applyHistoryBoundary(event);
        return;
      case "immediate":
        this.applyImmediate(event);
        return;
      default: {
        const _exhaustive: never = category;
        return _exhaustive;
      }
    }
  }

  /** 取消全部异步资源并丢弃尚未提交的流式分片；不再通知订阅者。 */
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.cancelScheduledFlush();
    this.clearPending();
    this.unsubscribeVisibility();
    this.listeners.clear();
  }

  /** 普通事件归并：写 `this.state`，不调度、不通知。 */
  private applyImmediateEventSilent(event: ChatEvent): void {
    switch (event.kind) {
      case "history_batch_loaded":
        this.expandHistory(event);
        return;
      case "user_message":
        this.upsertMessage(
          event,
          "user",
          "completed",
          String(event.payload.text ?? ""),
        );
        this.attachUserAttachments(event);
        this.attachUserReferences(event);
        return;
      case "assistant_message_started":
        this.ensureTurn(event);
        this.state = { ...this.state, activeStreamingTurnId: event.turnId };
        return;
      case "assistant_message_delta":
        this.appendAssistantDeltaCommitted(event);
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
        return assertNever(event.kind);
    }
  }

  /** history 批量入口的静默归并；历史 delta 直接落已提交状态，绝不创建 scheduler。 */
  private applyHistoryEventSilent(event: ChatEvent): void {
    this.applyImmediateEventSilent(event);
  }

  private applyImmediate(event: ChatEvent): void {
    const before = this.state;
    this.applyImmediateEventSilent(event);
    if (this.state !== before) this.notify();
  }

  private applyHistoryBoundary(event: ChatEvent): void {
    const before = this.state;
    this.cancelScheduledFlush();
    this.flushAllPendingSilent("history");
    this.applyHistoryEventSilent(event);
    this.state = { ...this.state, historyLoaded: true };
    if (this.state !== before) this.notify();
  }

  private applyTerminal(event: ChatEvent): void {
    const before = this.state;
    this.cancelScheduledFlush();
    this.flushAllPendingSilent("terminal");
    if (event.kind === "assistant_message_completed") {
      this.completeAssistant(event);
    } else {
      this.completeTurn(event);
    }
    if (this.state !== before) this.notify();
  }

  private applyOrderingBoundary(event: ChatEvent): void {
    const before = this.state;
    this.cancelScheduledFlush();
    this.flushAllPendingSilent("ordering");
    this.applyImmediateEventSilent(event);
    if (this.state !== before) this.notify();
  }

  private applyStreamFailure(event: ChatEvent): void {
    const before = this.state;
    this.cancelScheduledFlush();
    const failedTurnId = this.state.activeStreamingTurnId ?? event.turnId;
    this.clearFailedTurn(failedTurnId);
    this.flushAllPendingSilent("stream-error");
    this.upsertError(event);
    if (this.state !== before) this.notify();
  }

  // ---- turn 归并 ----

  private ensureTurn(event: ChatEvent): ChatTurnState {
    const existing = this.state.turnsById[event.turnId];
    if (existing) return existing;
    const turn: ChatTurnState = {
      id: event.turnId,
      threadId: event.threadId,
      runId: eventRunId(event),
      turn: eventTurn(event),
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
    const incomingRunId = eventRunId(event);
    const incomingTurn = eventTurn(event);
    const hasIncomingTurnIdentity = Boolean(incomingRunId) || incomingTurn !== null;
    const turnId = hasIncomingTurnIdentity ? event.turnId : (prev?.turnId ?? event.turnId);
    const record: ChatMessageRecord = {
      id,
      threadId: event.threadId,
      turnId,
      runId: incomingRunId || prev?.runId || "",
      turn: incomingTurn ?? prev?.turn ?? null,
      role,
      provider: event.provider as ChatProviderKind,
      parts: [{ type: "text", text }],
      reasoning: prev?.reasoning,
      usage: prev?.usage,
      status,
      deliveryStatus:
        role === "user" && event.payload.deliveryStatus === "steered" ? "steered" : undefined,
      createdAt: prev?.createdAt ?? event.createdAt,
      updatedAt: event.createdAt,
    };
    this.writeMessage(id, record, !prev);
    if (role === "user") this.patchTurn(turnId, { userMessageId: id });
    if (role === "assistant") this.patchTurn(turnId, { assistantMessageId: id });
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

  /** 把 user_message 事件携带的 conversation references 落到 user record。 */
  private attachUserReferences(event: ChatEvent): void {
    const references = conversationReferencesFromValue(event.payload.references);
    if (!references) return;
    const id = this.messageKey(event, "user");
    const prev = this.state.messagesById[id];
    if (!prev) return;
    this.patchMessage(id, { references });
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

  /**
   * 实时 delta 只进入私有 pending buffer。这里允许建立 turn 状态，禁止可见 notify；
   * 真正的 assistant record 在 scheduler / 边界 flush 中统一提交。
   */
  private bufferAssistantDelta(event: ChatEvent): void {
    this.ensureTurn(event);
    if (this.state.activeStreamingTurnId !== event.turnId) {
      this.state = { ...this.state, activeStreamingTurnId: event.turnId };
    }
    const key = turnKey(event);
    let pending = this.pendingByTurn.get(key);
    if (!pending) {
      pending = {
        threadId: event.threadId,
        runId: eventRunId(event),
        turnId: event.turnId,
        turn: eventTurn(event),
        provider: event.provider as ChatProviderKind,
        messageId: event.messageId,
        createdAt: event.createdAt,
        contentParts: [],
        reasoningParts: [],
        eventCount: 0,
        charCount: 0,
        lastCreatedAt: event.createdAt,
      };
      this.pendingByTurn.set(key, pending);
    }
    const delta = stringPayload(event.payload.delta);
    const reasoningDelta = stringPayload(event.payload.reasoningDelta);
    if (delta) pending.contentParts.push(delta);
    if (reasoningDelta) pending.reasoningParts.push(reasoningDelta);
    const chars = delta.length + reasoningDelta.length;
    pending.eventCount += 1;
    pending.charCount += chars;
    pending.lastCreatedAt = event.createdAt;
    this.totalPendingEvents += 1;
    this.totalPendingChars += chars;

    if (this.hasReachedPendingLimit(pending)) {
      const before = this.state;
      this.cancelScheduledFlush();
      this.flushAllPendingSilent("emergency-size");
      if (this.state !== before) this.notify();
      return;
    }
    this.ensureScheduledFlush();
  }

  /** 把一个 delta 直接写入已提交 state，仅用于 history 批量或 flush。 */
  private appendAssistantDeltaCommitted(event: ChatEvent): void {
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

  /** scheduler、terminal、ordering、history 共享的全 Store 静默提交入口。 */
  private flushAllPendingSilent(
    _reason: "interval" | "emergency-size" | "terminal" | "ordering" | "history" | "stream-error",
  ): boolean {
    if (this.pendingByTurn.size === 0) return false;
    const pendingTurns = [...this.pendingByTurn.values()];
    this.clearPending();
    for (const pending of pendingTurns) {
      const event: ChatEvent = {
        kind: "assistant_message_delta",
        provider: pending.provider,
        threadId: pending.threadId,
        turnId: pending.turnId,
        runId: pending.runId,
        turn: pending.turn,
        messageId: pending.messageId,
        createdAt: pending.lastCreatedAt,
        payload: {
          delta: pending.contentParts.join(""),
          reasoningDelta: pending.reasoningParts.join(""),
        },
      };
      this.appendAssistantDeltaCommitted(event);
    }
    return true;
  }

  private ensureScheduledFlush(): void {
    if (this.scheduledFlush || this.pendingByTurn.size === 0 || this.disposed) return;
    const generation = ++this.schedulerGeneration;
    const callback = () => {
      if (this.disposed || generation !== this.schedulerGeneration) return;
      this.scheduledFlush = null;
      const before = this.state;
      this.flushAllPendingSilent("interval");
      if (!this.visibility.isHidden()) this.lastVisibleCommitAt = this.now();
      if (this.state !== before) this.notify();
    };
    const handle = this.visibility.isHidden()
      ? this.scheduler.scheduleBackground(this.policy.backgroundFlushIntervalMs, callback)
      : this.scheduler.scheduleForeground(this.foregroundDelayMs(), callback);
    this.scheduledFlush = { generation, handle };
  }

  private foregroundDelayMs(): number {
    if (this.lastVisibleCommitAt === null) return this.policy.foregroundFlushIntervalMs;
    return Math.max(0, this.policy.foregroundFlushIntervalMs - (this.now() - this.lastVisibleCommitAt));
  }

  private handleVisibilityChange(): void {
    if (this.disposed || this.pendingByTurn.size === 0) return;
    this.cancelScheduledFlush();
    this.ensureScheduledFlush();
  }

  private cancelScheduledFlush(): void {
    const scheduled = this.scheduledFlush;
    this.schedulerGeneration += 1;
    this.scheduledFlush = null;
    scheduled?.handle.cancel();
  }

  private clearPending(): void {
    this.pendingByTurn.clear();
    this.totalPendingEvents = 0;
    this.totalPendingChars = 0;
  }

  private hasReachedPendingLimit(pending: PendingTurn): boolean {
    return (
      pending.eventCount >= this.policy.maxBufferedEventsPerTurn ||
      pending.charCount >= this.policy.maxBufferedCharsPerTurn ||
      this.pendingByTurn.size >= this.policy.maxBufferedTurnsPerStore ||
      this.totalPendingEvents >= this.policy.maxBufferedEventsPerStore ||
      this.totalPendingChars >= this.policy.maxBufferedCharsPerStore
    );
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
      const finalChangesText =
        typeof finalText === "string" &&
        finalText.length > 0 &&
        textPartsOf(prev.parts) !== finalText;
      const usageChanges = usage !== undefined && !sameUsage(prev.usage, usage);
      if (prev.status !== "completed" || finalChangesText || usageChanges) {
        const patch: Partial<ChatMessageRecord> = {
          status: "completed",
          updatedAt: event.createdAt,
        };
        if (finalChangesText) patch.parts = [{ type: "text", text: finalText as string }];
        if (usageChanges) patch.usage = usage;
        this.patchMessage(id, patch);
      }
    }
    if (this.state.activeStreamingTurnId === event.turnId) {
      this.state = { ...this.state, activeStreamingTurnId: null };
    }
    if (this.state.turnsById[event.turnId]?.phase !== "completed") {
      this.patchTurn(event.turnId, { phase: "completed" });
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
        runId: eventRunId(event),
        turn: eventTurn(event),
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
      runId: eventRunId(event),
      turn: eventTurn(event),
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
          runId: eventRunId(event),
          turn: eventTurn(event),
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
      } else {
        const pending: ChatPendingTool = {
          id,
          threadId: event.threadId,
          turnId: event.turnId,
          runId: eventRunId(event),
          turn: eventTurn(event),
          partialInput: partialDelta,
          startedAt: event.createdAt,
        };
        this.state = {
          ...this.state,
          pendingTools: { ...this.state.pendingTools, [id]: pending },
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
    const turn = this.ensureTurn(event);
    if (turn.phase !== "completed") this.patchTurn(event.turnId, { phase: "completed" });
    if (turn.assistantMessageId) {
      const message = this.state.messagesById[turn.assistantMessageId];
      if (message) {
        const historyIndex = event.payload.historyIndex;
        const forkHistoryIndex =
          event.payload.hasToolCalls !== true &&
          typeof historyIndex === "number"
            ? historyIndex
            : undefined;
        this.patchMessage(turn.assistantMessageId, {
          status: "completed",
          updatedAt: event.createdAt,
          forkHistoryIndex,
        });
      }
    }
    if (this.state.activeStreamingTurnId === event.turnId) {
      this.state = { ...this.state, activeStreamingTurnId: null };
    }
  }

  /**
   * llm_error 的失败收口：失败 turn 的未完成 assistant 与 pending 全部丢弃，
   * 同 Store 其它 turn 仍由调用方继续 flush，避免遗留孤儿 callback/分片。
   */
  private clearFailedTurn(turnId: string): void {
    for (const [key, pending] of this.pendingByTurn) {
      if (pending.turnId === turnId) {
        this.totalPendingEvents -= pending.eventCount;
        this.totalPendingChars -= pending.charCount;
        this.pendingByTurn.delete(key);
      }
    }

    const messageIdsToDelete = Object.values(this.state.messagesById)
      .filter((record) => record.turnId === turnId && record.role === "assistant" && record.status !== "completed")
      .map((record) => record.id);
    const messageIdSet = new Set(messageIdsToDelete);
    const messagesById = { ...this.state.messagesById };
    for (const id of messageIdsToDelete) delete messagesById[id];
    const currentTurn = this.state.turnsById[turnId];
    const turnsById = currentTurn
      ? {
          ...this.state.turnsById,
          [turnId]: {
            ...currentTurn,
            phase: "failed" as const,
            assistantMessageId: null,
          },
        }
      : this.state.turnsById;
    const activeStreamingTurnId =
      this.state.activeStreamingTurnId === turnId ? null : this.state.activeStreamingTurnId;

    if (
      messageIdsToDelete.length > 0 ||
      turnsById !== this.state.turnsById ||
      activeStreamingTurnId !== this.state.activeStreamingTurnId
    ) {
      this.state = {
        ...this.state,
        messagesById,
        orderedMessageIds: this.state.orderedMessageIds.filter((id) => !messageIdSet.has(id)),
        turnsById,
        activeStreamingTurnId,
      };
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
      runId: eventRunId(event),
      turn: eventTurn(event),
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
      runId: eventRunId(event),
      turn: eventTurn(event),
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
   * history_batch_loaded 展开：把 NormalizedMessage[] 展开成 records。
   * id 使用 NormalizedMessage.id，与实时 messageId 同源以幂等去重。
   */
  private expandHistory(event: ChatEvent): void {
    const messages = event.payload.messages;
    if (!Array.isArray(messages)) return;
    const sorted = [...(messages as NormalizedMessage[])].sort(
      (a, b) => normalizedMessageMs(a, event.createdAt) - normalizedMessageMs(b, event.createdAt),
    );
    let turnIndex = 0;
    let lastRole: string | undefined;
    for (const m of sorted) {
      const roleKey = normalizedHistoryRole(m);
      if (roleKey === "user" && lastRole && lastRole !== "user") turnIndex += 1;
      const turnId = `${event.threadId}-history-${turnIndex}`;
      this.expandHistoryMessage(
        event.provider as ChatProviderKind,
        event.threadId,
        turnId,
        turnIndex,
        m,
        event.createdAt,
      );
      lastRole = roleKey;
    }
  }

  private expandHistoryMessage(
    provider: ChatProviderKind,
    threadId: string,
    turnId: string,
    turnIndex: number,
    m: NormalizedMessage,
    fallbackAt: number,
  ): void {
    const at = normalizedMessageMs(m, fallbackAt);
    const id = m.id ?? `${turnId}:${m.frame_type}:${at}`;

    if (m.frame_type === "text" && m.role === "user") {
      if (this.state.messagesById[id]) return; // 已有同源 → 幂等跳过
      this.ensureHistoryTurn(provider, threadId, turnId, turnIndex);
      const parts: ChatMessagePart[] = [{ type: "text", text: String(m.content ?? "") }];
      const attachments = attachmentsFromMetadata(m.metadata);
      if (attachments) {
        parts.push(
          ...attachments.map((attachment) => ({
            type: "attachment" as const,
            attachment,
          })),
        );
      }
      const references = conversationReferencesFromMetadata(m.metadata);
      this.writeMessage(
        id,
        {
          id,
          threadId,
          turnId,
          runId: "",
          turn: turnIndex,
          role: "user",
          provider,
          parts,
          references,
          status: "completed",
          createdAt: at,
          updatedAt: at,
        },
        true,
      );
      this.patchTurn(turnId, { userMessageId: id });
      return;
    }

    if (m.frame_type === "text") {
      const prev = this.state.messagesById[id];
      if (prev) return;
      this.ensureHistoryTurn(provider, threadId, turnId, turnIndex);
      this.writeMessage(
        id,
        {
          id,
          threadId,
          turnId,
          runId: "",
          turn: turnIndex,
          role: "assistant",
          provider,
          parts: [{ type: "text", text: String(m.content ?? "") }],
          forkHistoryIndex:
            typeof m.historyIndex === "number" ? m.historyIndex : undefined,
          status: "completed",
          createdAt: at,
          updatedAt: at,
        },
        true,
      );
      this.patchTurn(turnId, { assistantMessageId: id });
      return;
    }

    if (m.frame_type === "tool_use") {
      const callId = m.toolId ?? id;
      if (this.state.messagesById[callId]) return;
      this.ensureHistoryTurn(provider, threadId, turnId, turnIndex);
      const args =
        m.toolInput && typeof m.toolInput === "object"
          ? (m.toolInput as Record<string, unknown>)
          : {};
      const tool: ChatToolRecord = {
        id: callId,
        threadId,
        turnId,
        runId: "",
        turn: turnIndex,
        toolName: m.toolName ?? "unknown",
        status: "running",
        inputText: typeof m.toolInput === "string" ? m.toolInput : JSON.stringify(m.toolInput ?? {}),
        outputText: "",
        arguments: args,
        pending: false,
        startedAt: at,
      };
      this.state = {
        ...this.state,
        toolsById: { ...this.state.toolsById, [callId]: tool },
      };
      const turn = this.state.turnsById[turnId];
      if (turn && !turn.toolCallIds.includes(callId)) {
        this.patchTurn(turnId, { toolCallIds: [...turn.toolCallIds, callId] });
      }
      this.writeMessage(
        callId,
        {
          id: callId,
          threadId,
          turnId,
          runId: "",
          turn: turnIndex,
          role: "tool",
          provider,
          parts: [],
          status: "streaming",
          createdAt: at,
          updatedAt: at,
        },
        true,
      );
      return;
    }

    if (m.frame_type !== "tool_result") return;
    const callId = m.toolId ?? id;
    this.ensureHistoryTurn(provider, threadId, turnId, turnIndex);
    const ok = m.isError !== true;
    const prevTool = this.state.toolsById[callId];
    const outputText = typeof m.content === "string" ? m.content : JSON.stringify(m.content ?? "");
    const tool: ChatToolRecord = {
      id: callId,
      threadId,
      turnId,
      runId: "",
      turn: turnIndex,
      toolName: m.toolName ?? prevTool?.toolName ?? "unknown",
      status: ok ? "completed" : "failed",
      inputText: prevTool?.inputText ?? "",
      outputText,
      arguments: prevTool?.arguments ?? {},
      errorMessage: ok ? undefined : String(m.content ?? ""),
      resultData: null,
      pending: false,
      partialInput: prevTool?.partialInput,
      startedAt: prevTool?.startedAt ?? at,
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
    const prevMessage = this.state.messagesById[callId];
    if (prevMessage) {
      this.patchMessage(callId, {
        status: ok ? "completed" : "failed",
        updatedAt: at,
      });
      return;
    }
    // tool 也占一个 orderedMessageIds 槽位（role="tool" 的轻 record，承载顺序 + 关联）。
    this.writeMessage(
      callId,
      {
        id: callId,
        threadId,
        turnId,
        runId: "",
        turn: turnIndex,
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
    turnIndex: number,
  ): void {
    if (this.state.turnsById[turnId]) return;
    const turn: ChatTurnState = {
      id: turnId,
      threadId,
      runId: "",
      turn: turnIndex,
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

function textPartsOf(parts: ChatMessagePart[]): string {
  return parts
    .filter((part): part is Extract<ChatMessagePart, { type: "text" }> => part.type === "text")
    .map((part) => part.text)
    .join("");
}

function sameUsage(left: ChatMessageUsage | undefined, right: ChatMessageUsage): boolean {
  return left?.prompt === right.prompt && left.completion === right.completion && left.total === right.total;
}

type EventCategory = "delta" | "terminal" | "ordering" | "stream-failure" | "history" | "immediate";

/** 事件分类真源：新增有限 ChatEventKind 后 default 的 never 迫使显式决定边界语义。 */
function classifyEvent(event: ChatEvent): EventCategory {
  switch (event.kind) {
    case "assistant_message_delta":
      return "delta";
    case "assistant_message_completed":
    case "turn_completed":
      return "terminal";
    case "tool_call_started":
      return "ordering";
    case "error":
      return event.payload.errorCode === "llm_error" ? "stream-failure" : "ordering";
    case "history_batch_loaded":
      return "history";
    case "user_message":
    case "assistant_message_started":
    case "tool_call_delta":
    case "tool_call_completed":
    case "status":
      return "immediate";
    default:
      return assertNever(event.kind);
  }
}

function assertNever(value: never): never {
  throw new Error(`Unhandled ChatEventKind: ${String(value)}`);
}

function turnKey(event: ChatEvent): TurnKey {
  return `${event.threadId}\u0000${eventRunId(event)}\u0000${event.turnId}`;
}

function stringPayload(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function normalizedMessageMs(msg: NormalizedMessage, fallback: number): number {
  if (!msg.timestamp) return fallback;
  const parsed = Date.parse(msg.timestamp);
  return Number.isNaN(parsed) ? fallback : parsed;
}

function normalizedHistoryRole(msg: NormalizedMessage): string {
  if (msg.frame_type === "tool_use" || msg.frame_type === "tool_result") return "tool";
  return msg.role ?? "assistant";
}

function eventRunId(event: ChatEvent): string {
  return event.runId ?? "";
}

function eventTurn(event: ChatEvent): number | null {
  return typeof event.turn === "number" ? event.turn : null;
}

function conversationReferencesFromMetadata(
  metadata: Record<string, unknown> | undefined,
): ConversationReferenceDTO[] | undefined {
  if (!metadata || typeof metadata !== "object") return undefined;
  return conversationReferencesFromValue(metadata.conversation_references);
}

function attachmentsFromMetadata(
  metadata: Record<string, unknown> | undefined,
): UserInputAttachment[] | undefined {
  if (!metadata || typeof metadata !== "object") return undefined;
  const raw = metadata.attachments;
  if (!Array.isArray(raw) || raw.length === 0) return undefined;
  const attachments = raw.filter(isUserInputAttachment);
  return attachments.length > 0 ? attachments : undefined;
}

function isUserInputAttachment(value: unknown): value is UserInputAttachment {
  if (!value || typeof value !== "object") return false;
  const attachment = value as Partial<UserInputAttachment>;
  return (
    typeof attachment.asset_id === "string" &&
    (attachment.kind === "image" ||
      attachment.kind === "video" ||
      attachment.kind === "file") &&
    typeof attachment.mime_type === "string" &&
    typeof attachment.size_bytes === "number" &&
    typeof attachment.preview_url === "string" &&
    (attachment.status === "ready" ||
      attachment.status === "processing" ||
      attachment.status === "failed")
  );
}

function conversationReferencesFromValue(
  raw: unknown,
): ConversationReferenceDTO[] | undefined {
  if (!Array.isArray(raw) || raw.length === 0) return undefined;
  const refs = raw.filter(isConversationReferenceDTO);
  return refs.length > 0 ? refs : undefined;
}

function isConversationReferenceDTO(value: unknown): value is ConversationReferenceDTO {
  if (!value || typeof value !== "object") return false;
  const ref = value as Partial<ConversationReferenceDTO>;
  return (
    typeof ref.id === "string" &&
    typeof ref.kind === "string" &&
    typeof ref.ref === "string" &&
    typeof ref.label === "string" &&
    typeof ref.activation === "string"
  );
}
