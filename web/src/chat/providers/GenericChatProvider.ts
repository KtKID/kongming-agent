/**
 * message-runtime-v0.1 · GenericChatProvider（#2 骨架）
 *
 * 把公共 `SendRequest` 翻译成 generic_chat 的 `WSFrameC2S`，把入站 `WSFrameS2C`
 * 翻译成统一 `ChatEvent`。传输仍走现有 `ThreadSocket`（本 task 不收编 transport）。
 *
 * generic 链路特点（与 claude / codex 不同）：
 * - 发送帧 = `user.input`（带 request_id / reasoning_effort / attachments）
 * - 历史 = 连接后后端**被动推送** `thread.history` 帧，不是主动 REST 拉取
 * - 无 provider session 概念 → `checkSessionStatus` 固定返回 `{ active: false }`
 *
 * 真源：`@/protocol`（WSFrameC2S/S2C）+ `02-module-breakdown.md` 三频道能力对照。
 */
import type {
  WSFrameS2C,
  HistoryMessageDTO,
} from "@/protocol";
import type {
  ChatProvider,
  SendRequest,
  HistoryLoadRequest,
  InterruptRequest,
  SessionStatusRequest,
  SessionStatus,
  RawFrameEnvelope,
  NetworkHandle,
  ChatEvent,
  ChatHistoryBatch,
} from "@/chat/types";

/** 生成一次发送的 request_id；骨架用时间戳 + 随机，#6 可换成集中式 id 工具。 */
function makeRequestId(): string {
  return `req-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}`;
}

/** generic 的 turn 归并键：优先 run_id，回退到 `${threadId}-turn-${turn}`。 */
function genericTurnId(threadId: string, runId?: string, turn?: number): string {
  if (runId) return runId;
  return `${threadId}-turn-${turn ?? 0}`;
}

export class GenericChatProvider implements ChatProvider {
  readonly provider = "generic" as const;

  async send(handle: NetworkHandle, request: SendRequest): Promise<void> {
    if (request.provider.provider !== "generic") {
      throw new Error(
        `[GenericChatProvider] 收到非 generic 的 SendRequest: ${request.provider.provider}`,
      );
    }
    const { text, reasoningEffort, attachments } = request.common;
    handle.send({
      frame_type: "user.input",
      text,
      request_id: makeRequestId(),
      reasoning_effort: reasoningEffort ?? null,
      attachments,
    });
  }

  async loadHistory(request: HistoryLoadRequest): Promise<ChatHistoryBatch> {
    // generic 历史不是主动拉取：连接 `/ws/threads/{id}` 后后端推 `thread.history` 帧，
    // 该帧由 `mapInboundFrame` 翻成 `history_batch_loaded` 事件进入时间线。
    // 这里返回空 batch，表示「无需主动 fetch」。ChatManager 不应对 generic 依赖本返回值。
    return {
      threadId: request.threadId,
      provider: "generic",
      events: [],
      hasMore: false,
    };
  }

  mapInboundFrame(envelope: RawFrameEnvelope): ChatEvent[] {
    const frame = envelope.frame as WSFrameS2C;
    // envelope 不带 threadId 时回退到 connectionId（generic 单 thread 单连接）
    const tid = envelope.threadId ?? envelope.connectionId;
    const at =
      "timestamp_ms" in frame && typeof frame.timestamp_ms === "number"
        ? frame.timestamp_ms
        : envelope.receivedAt;

    switch (frame.frame_type) {
      case "turn.start":
        return [
          this.ev("assistant_message_started", tid, genericTurnId(tid, frame.run_id, frame.turn), at, {
            turn: frame.turn,
          }),
        ];
      case "content.delta":
        return [
          this.ev("assistant_message_delta", tid, genericTurnId(tid, frame.run_id, frame.turn), at, {
            delta: frame.delta,
            seq: frame.seq,
          }),
        ];
      case "reasoning.delta":
        return [
          this.ev("assistant_message_delta", tid, genericTurnId(tid, frame.run_id, frame.turn), at, {
            reasoningDelta: frame.delta,
            seq: frame.seq,
          }),
        ];
      case "assistant.final":
        return [
          this.ev("assistant_message_completed", tid, genericTurnId(tid, frame.run_id, frame.turn), at, {
            content: frame.content,
          }),
        ];
      case "tool.call.start":
        return [
          this.evTool("tool_call_started", tid, genericTurnId(tid, frame.run_id, frame.turn), at, frame.call_id, {
            toolName: frame.tool_name,
            arguments: frame.arguments,
          }),
        ];
      case "tool.call.end":
        return [
          this.evTool("tool_call_completed", tid, genericTurnId(tid, frame.run_id, frame.turn), at, frame.call_id, {
            ok: frame.ok,
            content: frame.content,
            data: frame.data,
            errorMessage: frame.error_message,
          }),
        ];
      case "turn.end":
        return [
          this.ev("turn_completed", tid, genericTurnId(tid, frame.run_id, frame.turn), at, { turn: frame.turn }),
        ];
      case "thread.history":
        return [this.historyEvent(tid, at, frame.messages)];
      case "error":
        return [
          this.ev("error", tid, genericTurnId(tid, undefined, frame.turn), at, {
            errorCode: frame.error_code,
            message: frame.message,
          }),
        ];
      case "system.notice":
        return [
          this.ev("status", tid, genericTurnId(tid, frame.run_id), at, {
            noticeKey: frame.notice_key,
            status: frame.status,
            title: frame.title,
            message: frame.message,
          }),
        ];
      case "usage":
        // usage 帧 → status notice record（noticeKey `usage:{turn}` 同 turn 覆盖更新）。
        // 不并入 assistant_message_completed：UsageFrame.usage 是 channel-specific
        // ThreadUsage（input_tokens/output_tokens/total/last 等），跟 store 的
        // ChatMessageUsage(`{prompt,completion,total}`)/parseUsage 形态不同；再造一条
        // completed 事件还会用空 content 覆盖已累计 assistant 文本。落成 status record
        // 既让时间线持有 record（useStreamingRender 退役不回归），又把原始 ThreadUsage
        // 透传给视图层 StatusLine 渲染。
        return [
          this.ev("status", tid, genericTurnId(tid, frame.run_id, frame.turn), at, {
            noticeKey: `usage:${frame.turn}`,
            source: "usage",
            status: "success",
            usage: frame.usage,
          }),
        ];
      case "approval.request":
        // approval.request → status notice record（noticeKey `approval:{call_id}`），
        // 让内联审批横幅能在时间线渲染。审批 dialog 队列（pushApproval）是视图层
        // 职责（#5），provider 只负责翻成可落 record 的 ChatEvent。
        return [
          this.ev("status", tid, genericTurnId(tid, undefined, frame.turn), at, {
            noticeKey: `approval:${frame.call_id}`,
            source: "approval",
            status: "running",
            callId: frame.call_id,
            toolName: frame.tool_name,
            arguments: frame.arguments,
            reason: frame.reason,
            policyHint: frame.policy_hint,
            confirmToken: frame.confirm_token,
          }),
        ];
      case "run.interrupted":
        // run.interrupted（interrupt-run-v0.1 runner cancel 收尾）→ turn_completed，
        // 让状态机把该 turn 标 completed 并复位 activeStreamingTurnId（streaming=false），
        // Stop 按钮立刻隐藏。turnId 走 run_id（与实时 turn 同源）。
        return [
          this.ev("turn_completed", tid, genericTurnId(tid, frame.run_id, frame.cancelled_at_turn), at, {
            turn: frame.cancelled_at_turn,
            cancelled: true,
            cancelReason: frame.cancel_reason,
            cancelledToolCallId: frame.cancelled_tool_call_id ?? null,
          }),
        ];
      // cell.evicted / pong：toast-only 副作用与心跳，不进时间线。
      // - cell.evicted：thread 生命周期事件（视图层 toast.warning + 清 buffer，归 #5）。
      // - pong：心跳 ack，无业务语义。
      case "cell.evicted":
      case "pong":
        return [];
      default:
        return [];
    }
  }

  async interrupt(handle: NetworkHandle, request: InterruptRequest): Promise<void> {
    handle.send({ frame_type: "interrupt", run_id: request.sessionId ?? null });
  }

  async checkSessionStatus(
    request: SessionStatusRequest,
  ): Promise<SessionStatus> {
    // generic_chat 没有 provider 级 session，固定返回「无活跃 session」。
    return {
      active: false,
      message: `generic channel (thread ${request.threadId}) has no provider session`,
    };
  }

  // ---- 内部小工具：组装 ChatEvent ----

  private ev(
    kind: ChatEvent["kind"],
    threadId: string,
    turnId: string,
    createdAt: number,
    payload: Record<string, unknown>,
  ): ChatEvent {
    return { kind, provider: "generic", threadId, turnId, createdAt, payload };
  }

  private evTool(
    kind: ChatEvent["kind"],
    threadId: string,
    turnId: string,
    createdAt: number,
    toolCallId: string,
    payload: Record<string, unknown>,
  ): ChatEvent {
    return { kind, provider: "generic", threadId, turnId, toolCallId, createdAt, payload };
  }

  private historyEvent(
    threadId: string,
    createdAt: number,
    messages: HistoryMessageDTO[],
  ): ChatEvent {
    return {
      kind: "history_batch_loaded",
      provider: "generic",
      threadId,
      turnId: `${threadId}-history`,
      createdAt,
      payload: { messages },
    };
  }
}
