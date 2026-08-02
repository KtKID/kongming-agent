/**
 * message-runtime-v0.1 · ChatManager（#3 骨架）
 *
 * 对外统一暴露 send / loadHistory / ingestFrame / interrupt / checkSessionStatus，
 * 内部负责 pending 新会话首发创建 + provider 分发，把差异下沉到 ChatProvider，
 * 把时间线归并下沉到 ChatTimelineStore。
 *
 * 解耦设计（transport 通过依赖注入留接缝）：
 * - `resolveHandle(kind, threadId)`：拿到该频道的 `NetworkHandle`。真实实现由 #5 视图
 *   接入时提供（包装现有 NetworkManager / CodexSocket），ChatManager
 *   不直连任何具体 socket。
 * - `ensureThread(request)`：pending 新会话首发时创建真实 thread 并返回 threadId。
 *   真实实现包装现有 threads store 的 pending→create 语义。
 * - `timelineFor(threadId)`：取该 thread 的时间线状态机（每 thread 一个）。
 *
 * 这样 ChatManager 只编排「创建时机 + provider 路由 + 事件灌入」，不感知 socket / store 细节。
 */
import type {
  ChatManagerApi,
  ChatProvider,
  ChatTimelineStoreApi,
  SendRequest,
  HistoryLoadRequest,
  InterruptRequest,
  ChoiceSubmitRequest,
  SessionStatusRequest,
  SessionStatus,
  RawFrameEnvelope,
  NetworkHandle,
  ChatProviderKind,
  ChatEvent,
} from "@/chat/types";
import { getChatProvider } from "@/chat/providers";
import { flushChatDeltaLog, logChat, logChatDelta } from "@/chat/logger";
import { useThreadDispatchStore } from "@/stores/threadDispatch";

export interface ChatManagerDeps {
  /** 取指定频道 + thread 的发送句柄；真实实现包装现有传输层（#5 注入）。 */
  resolveHandle(kind: ChatProviderKind, threadId: string): NetworkHandle;
  /**
   * 首发创建：pending 新会话在首条消息发送时创建真实 thread，返回最终 threadId。
   * 非 pending（已有真实 thread）时原样返回 request 里的 threadId。
   */
  ensureThread(request: SendRequest): Promise<string>;
  /** 取该 thread 的时间线状态机（每 thread 一个实例）。 */
  timelineFor(threadId: string): ChatTimelineStoreApi;
  /** 可选：取 provider 实现，默认用内置 registry。便于测试注入 mock。 */
  getProvider?(kind: ChatProviderKind): ChatProvider;
}

export class ChatManager implements ChatManagerApi {
  constructor(private readonly deps: ChatManagerDeps) {}

  private provider(kind: ChatProviderKind): ChatProvider {
    return (this.deps.getProvider ?? getChatProvider)(kind);
  }

  async sendMessage(request: SendRequest): Promise<void> {
    const kind = request.provider.provider;
    // 审计：进入发送入口时的完整请求。
    logChat("send", "sendMessage.in", { provider: kind, request });
    // 1) 首发创建：pending 新会话在此创建真实 thread（视图不再自己判断创建时机）。
    const threadId = await this.deps.ensureThread(request);
    logChat("send", "sendMessage.ensureThread", {
      provider: kind,
      requestedThreadId: request.provider.threadId,
      resolvedThreadId: threadId,
      created: threadId !== request.provider.threadId,
    });
    // 2) threadId 可能从 pending 占位变成真实 id，回填到 provider 选项后再分发。
    const resolved: SendRequest = {
      ...request,
      provider: { ...request.provider, threadId },
    };
    // 3) transport dispatch 是短暂交互态，和服务端 running 投影分开保存。
    const handle = this.deps.resolveHandle(kind, threadId);
    logChat("send", "sendMessage.out", { provider: kind, threadId, request: resolved });
    useThreadDispatchStore.getState().begin(threadId);
    try {
      await this.provider(kind).send(handle, resolved);
      useThreadDispatchStore.getState().succeed(threadId);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      useThreadDispatchStore.getState().fail(threadId, message);
      throw err;
    }

    // 4) 非 generic transport 确认发送后再写用户消息，避免失败留下虚假气泡。
    if (kind !== "generic") {
      const { text, attachments, references } = request.common;
      this.deps.timelineFor(threadId).applyEvent({
        kind: "user_message",
        provider: kind,
        threadId,
        turnId: `${threadId}-turn-${Date.now()}`,
        createdAt: Date.now(),
        payload: { text, attachments, references },
      });
      logChat("send", "sendMessage.seedUser", { provider: kind, threadId });
    }
  }

  async loadHistory(request: HistoryLoadRequest): Promise<void> {
    logChat("call", "loadHistory.in", { provider: request.provider, request });
    const batch = await this.provider(request.provider).loadHistory(request);
    logChat("result", "loadHistory.batch", {
      provider: request.provider,
      threadId: request.threadId,
      eventCount: batch.events.length,
      hasMore: batch.hasMore,
    });
    this.deps.timelineFor(request.threadId).applyHistory(batch);
  }

  ingestFrame(envelope: RawFrameEnvelope): void {
    const events = this.provider(envelope.channel).mapInboundFrame(envelope);
    const deltaEvents = events.filter(isAssistantDelta);
    const normalEvents = events.filter((event) => !isAssistantDelta(event));
    if (deltaEvents.length === 0) {
      logChat("recv", "ingestFrame.in", {
        channel: envelope.channel,
        threadId: envelope.threadId,
        connectionId: envelope.connectionId,
        frame: envelope.frame,
      });
    } else {
      // 原始 delta frame 也会携带正文。只进入 turn 级摘要器，避免每帧产生日志对象。
      for (const event of deltaEvents) {
        logChatDelta({
          threadId: event.threadId,
          runId: event.runId,
          turnId: event.turnId,
          content: typeof event.payload.delta === "string" ? event.payload.delta : undefined,
          reasoning: typeof event.payload.reasoningDelta === "string" ? event.payload.reasoningDelta : undefined,
        });
      }
    }
    // 非 delta event 保留完整结构化审计；delta 已由摘要器单独处理。
    if (normalEvents.length > 0 || events.length === 0) {
      logChat("recv", "ingestFrame.events", {
        channel: envelope.channel,
        count: events.length,
        events: normalEvents,
      });
    }
    for (const event of events) {
      flushDeltaLogAtBoundary(event);
      this.deps.timelineFor(event.threadId).applyEvent(event);
    }
  }

  async submitChoice(request: ChoiceSubmitRequest): Promise<void> {
    logChat("send", "submitChoice", { provider: request.provider, request });
    const handle = this.deps.resolveHandle(request.provider, request.threadId);
    const provider = this.provider(request.provider);
    if (typeof provider.submitChoice !== "function") {
      throw new Error(`[ChatManager] provider does not support choice submit: ${request.provider}`);
    }
    await provider.submitChoice(handle, request);
  }

  async interrupt(request: InterruptRequest): Promise<void> {
    logChat("send", "interrupt", { provider: request.provider, request });
    const handle = this.deps.resolveHandle(request.provider, request.threadId);
    await this.provider(request.provider).interrupt(handle, request);
  }

  async checkSessionStatus(
    request: SessionStatusRequest,
  ): Promise<SessionStatus> {
    logChat("call", "checkSessionStatus.in", { provider: request.provider, request });
    const status = await this.provider(request.provider).checkSessionStatus(request);
    logChat("result", "checkSessionStatus.out", { provider: request.provider, status });
    return status;
  }
}

function isAssistantDelta(event: ChatEvent): boolean {
  return event.kind === "assistant_message_delta";
}

function flushDeltaLogAtBoundary(event: ChatEvent): void {
  if (event.kind === "assistant_message_completed" || event.kind === "turn_completed") {
    flushChatDeltaLog(event.threadId, event.runId, event.turnId, "terminal");
    return;
  }
  if (event.kind === "error" && event.payload.errorCode === "llm_error") {
    flushChatDeltaLog(event.threadId, event.runId, event.turnId, "stream-error");
  }
}
