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
  SessionStatusRequest,
  SessionStatus,
  RawFrameEnvelope,
  NetworkHandle,
  ChatProviderKind,
} from "@/chat/types";
import { getChatProvider } from "@/chat/providers";
import { logChat } from "@/chat/logger";
import { markThreadRunning } from "@/chat/runtimeWiring";

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
    // 2) 前置入态：预设 thread-status phase=responding，让 isRunning 立即 true。
    //    chat-running-state-unify #6：三频道一处实现，解决「发送→LLM 回复前
    //    无法打断」。后端真实 phase（responding→complete/error/idle）会幂等覆盖。
    markThreadRunning(threadId);
    logChat("send", "sendMessage.markRunning", { provider: kind, threadId });
    // 2.5) 用户消息前置入态：往时间线灌一条 user_message 事件，让用户气泡立即
    //      上屏（取代视图层旧的 appendUser 乐观写入）。turnId 用时间戳保唯一，
    //      避免与 assistant run_id turn / 其它 user turn 撞键；store 的
    //      user_message 分支会把 text + attachments 落成 record（缩略图零回归）。
    const { text, attachments } = request.common;
    this.deps.timelineFor(threadId).applyEvent({
      kind: "user_message",
      provider: kind,
      threadId,
      turnId: `${threadId}-turn-${Date.now()}`,
      createdAt: Date.now(),
      payload: { text, attachments },
    });
    logChat("send", "sendMessage.seedUser", { provider: kind, threadId });
    // 3) threadId 可能从 pending 占位变成真实 id，回填到 provider 选项后再分发。
    const resolved: SendRequest = {
      ...request,
      provider: { ...request.provider, threadId },
    };
    // 4) provider 翻译成频道 wire frame 并发送。
    const handle = this.deps.resolveHandle(kind, threadId);
    // 审计：实际下发给 provider 的完整请求（provider 内部再翻成 wire frame）。
    logChat("send", "sendMessage.out", { provider: kind, threadId, request: resolved });
    await this.provider(kind).send(handle, resolved);
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
    // 审计：入站原始帧（完整）。
    logChat("recv", "ingestFrame.in", {
      channel: envelope.channel,
      threadId: envelope.threadId,
      connectionId: envelope.connectionId,
      frame: envelope.frame,
    });
    const events = this.provider(envelope.channel).mapInboundFrame(envelope);
    // 审计：翻译出的统一事件（完整），count=0 也记录，便于发现「帧没被翻译」的盲区。
    logChat("recv", "ingestFrame.events", {
      channel: envelope.channel,
      count: events.length,
      events,
    });
    for (const event of events) {
      this.deps.timelineFor(event.threadId).applyEvent(event);
    }
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
