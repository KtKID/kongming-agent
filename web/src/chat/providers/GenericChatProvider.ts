/**
 * message-runtime-v0.1 · GenericChatProvider（#2 骨架）
 *
 * 把公共 `SendRequest` 翻译成 generic_chat 的 `WSFrameC2S`，把入站 `WSFrameS2C`
 * 翻译成统一 `ChatEvent`。传输走 `NetworkManager` 提供的 `NetworkHandle`。
 *
 * generic 链路特点（与 claude / codex 不同）：
 * - 发送帧 = `user.input`（带 request_id / reasoning_effort / attachments / references）
 * - 历史 = 连接后后端**被动推送** `thread.history` 帧，不是主动 REST 拉取
 * - 无 provider session 概念 → `checkSessionStatus` 固定返回 `{ active: false }`
 *
 * 真源：`@/protocol`（WSFrameC2S/S2C）+ `02-module-breakdown.md` 三频道能力对照。
 */
import type {
  WSFrameS2C,
  NormalizedMessage,
  SystemNoticeStatus,
} from "@/protocol";
import { makeCronTimelineKey } from "@/chat/runtimeWiring";
import type {
  ChatProvider,
  SendRequest,
  HistoryLoadRequest,
  InterruptRequest,
  ChoiceSubmitRequest,
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

interface TurnIdentity {
  turnId: string;
  runId: string;
  turn: number | null;
}

/** generic 的 turn 坐标：turnId 只做内部 key，runId/turn 作为结构化字段进入状态机。 */
function genericTurnIdentity(
  threadId: string,
  runId?: string | null,
  turn?: number | null,
): TurnIdentity {
  const normalizedRunId = runId ?? "";
  const normalizedTurn = typeof turn === "number" ? turn : null;
  if (normalizedRunId) {
    return {
      turnId: `${normalizedRunId}:turn-${normalizedTurn ?? "unknown"}`,
      runId: normalizedRunId,
      turn: normalizedTurn,
    };
  }
  return {
    turnId: `${threadId}-turn-${normalizedTurn ?? "unknown"}`,
    runId: "",
    turn: normalizedTurn,
  };
}

/** 从 pending_input.metadata 里读取数组字段，避免把非数组元数据写进 timeline。 */
function metadataArray(
  metadata: Record<string, unknown>,
  key: string,
): unknown[] | undefined {
  const value = metadata[key];
  return Array.isArray(value) ? value : undefined;
}

/** 把 wire notice 状态归一为聊天时间线使用的展示状态。 */
function normalizeSystemNoticeStatus(
  status: SystemNoticeStatus,
): "running" | "success" | "error" | "warning" {
  switch (status) {
    case "started":
      return "running";
    case "completed":
      return "success";
    case "failed":
      return "error";
    case "drain_timeout":
      return "warning";
    default: {
      const _exhaustive: never = status;
      return _exhaustive;
    }
  }
}

export class GenericChatProvider implements ChatProvider {
  readonly provider = "generic" as const;

  async send(handle: NetworkHandle, request: SendRequest): Promise<void> {
    if (request.provider.provider !== "generic") {
      throw new Error(
        `[GenericChatProvider] 收到非 generic 的 SendRequest: ${request.provider.provider}`,
      );
    }
    const { text, reasoningEffort, attachments, references } = request.common;
    handle.send({
      frame_type: "user.input",
      text,
      request_id: makeRequestId(),
      reasoning_effort: reasoningEffort ?? null,
      attachments,
      references,
    });
  }

  async submitChoice(handle: NetworkHandle, request: ChoiceSubmitRequest): Promise<void> {
    handle.send(request.frame);
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
          this.ev(
            "assistant_message_started",
            tid,
            genericTurnIdentity(tid, frame.run_id, frame.turn),
            at,
            { turn: frame.turn },
          ),
        ];
      case "content.delta":
        return [
          this.ev(
            "assistant_message_delta",
            tid,
            genericTurnIdentity(tid, frame.run_id, frame.turn),
            at,
            { delta: frame.delta, seq: frame.seq },
          ),
        ];
      case "reasoning.delta":
        return [
          this.ev(
            "assistant_message_delta",
            tid,
            genericTurnIdentity(tid, frame.run_id, frame.turn),
            at,
            { reasoningDelta: frame.delta, seq: frame.seq },
          ),
        ];
      case "assistant.final":
        return [
          this.ev(
            "assistant_message_completed",
            tid,
            genericTurnIdentity(tid, frame.run_id, frame.turn),
            at,
            { content: frame.content },
          ),
        ];
      case "tool.call.start":
        return [
          this.evTool(
            "tool_call_started",
            tid,
            genericTurnIdentity(tid, frame.run_id, frame.turn),
            at,
            frame.call_id,
            { toolName: frame.tool_name, arguments: frame.arguments },
          ),
        ];
      case "tool.call.end":
        return [
          this.evTool(
            "tool_call_completed",
            tid,
            genericTurnIdentity(tid, frame.run_id, frame.turn),
            at,
            frame.call_id,
            {
              ok: frame.ok,
              content: frame.content,
              data: frame.data,
              errorMessage: frame.error_message,
            },
          ),
        ];
      case "turn.end":
        return [
          this.ev(
            "turn_completed",
            tid,
            genericTurnIdentity(tid, frame.run_id, frame.turn),
            at,
            {
              turn: frame.turn,
              historyIndex: frame.history_index,
              hasToolCalls: frame.has_tool_calls ?? false,
            },
          ),
        ];
      case "thread.history":
        return [this.historyEvent(tid, at, frame.messages)];
      case "pending-input.steered": {
        const pending = frame.pending_input;
        const runId = frame.run_id || frame.active_run_id || "";
        return [
          this.ev(
            "user_message",
            tid,
            genericTurnIdentity(tid, runId, frame.turn ?? null),
            at,
            {
              text: pending.content,
              attachments: metadataArray(pending.metadata, "attachments"),
              references: metadataArray(pending.metadata, "references"),
              pendingInputId: pending.id,
              source: pending.source,
              deliveryStatus: "steered",
            },
            pending.id,
          ),
        ];
      }
      case "pending-input.started": {
        const pending = frame.pending_input;
        return [
          this.ev(
            "user_message",
            tid,
            genericTurnIdentity(tid, frame.run_id, null),
            at,
            {
              text: pending.content,
              attachments: metadataArray(pending.metadata, "attachments"),
              references: metadataArray(pending.metadata, "references"),
              pendingInputId: pending.id,
              source: pending.source,
            },
            pending.id,
          ),
        ];
      }
      case "cron.message.appended": {
        const parentThreadId = frame.thread_id || tid;
        const threadId = frame.run_id
          ? makeCronTimelineKey(parentThreadId, frame.run_id)
          : parentThreadId;
        return [
          this.ev(
            "assistant_message_completed",
            threadId,
            genericTurnIdentity(threadId, frame.run_id || frame.message_id, null),
            at,
            {
              content: frame.content,
              source: "cron",
              taskId: frame.task_id,
              taskName: frame.task_name,
              parentThreadId,
              sessionId: frame.session_id,
            },
            frame.message_id,
          ),
        ];
      }
      case "error":
        return [
          this.ev("error", tid, genericTurnIdentity(tid, "", frame.turn), at, {
            errorCode: frame.error_code,
            message: frame.message,
          }),
        ];
      case "system.notice":
        return [
          this.ev("status", tid, genericTurnIdentity(tid, frame.run_id, null), at, {
            noticeKey: frame.notice_key,
            source: frame.source,
            status: normalizeSystemNoticeStatus(frame.status),
            title: frame.title,
            message: frame.message,
            details: frame.details,
            icon: frame.icon,
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
          this.ev("status", tid, genericTurnIdentity(tid, frame.run_id, frame.turn), at, {
            noticeKey: `usage:${frame.turn}`,
            source: "usage",
            status: "success",
            usage: frame.usage,
          }),
        ];
      case "run.interrupted":
        // run.interrupted（interrupt-run-v0.1 runner cancel 收尾）→ turn_completed，
        // 让状态机把该 turn 标 completed 并复位 activeStreamingTurnId（streaming=false），
        // Stop 按钮立刻隐藏。turnId 走 run_id（与实时 turn 同源）。
        return [
          this.ev(
            "turn_completed",
            tid,
            genericTurnIdentity(tid, frame.run_id, frame.cancelled_at_turn),
            at,
            {
              turn: frame.cancelled_at_turn,
              cancelled: true,
              cancelReason: frame.cancel_reason,
              cancelledToolCallId: frame.cancelled_tool_call_id ?? null,
            },
          ),
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
    turn: TurnIdentity,
    createdAt: number,
    payload: Record<string, unknown>,
    messageId?: string,
  ): ChatEvent {
    return {
      kind,
      provider: "generic",
      threadId,
      turnId: turn.turnId,
      runId: turn.runId,
      turn: turn.turn,
      createdAt,
      payload,
      messageId,
    };
  }

  private evTool(
    kind: ChatEvent["kind"],
    threadId: string,
    turn: TurnIdentity,
    createdAt: number,
    toolCallId: string,
    payload: Record<string, unknown>,
  ): ChatEvent {
    return {
      kind,
      provider: "generic",
      threadId,
      turnId: turn.turnId,
      runId: turn.runId,
      turn: turn.turn,
      toolCallId,
      createdAt,
      payload,
    };
  }

  private historyEvent(
    threadId: string,
    createdAt: number,
    messages: NormalizedMessage[],
  ): ChatEvent {
    return {
      kind: "history_batch_loaded",
      provider: "generic",
      threadId,
      turnId: `${threadId}-history`,
      runId: "",
      turn: null,
      createdAt,
      payload: { messages },
    };
  }
}
