/**
 * message-runtime-v0.1 · ClaudeChatProvider（#2 骨架）
 *
 * 发送走 `claude-command`，打断走 `abort-session`，入站 `NormalizedMessage`
 * 复用 `normalizedMessageToEvents`。传输仍走现有 `/ws/claude-code`（NetworkManager）。
 *
 * 真源：`@/protocol::ClaudeCodeC2SFrame / NormalizedMessage / SessionStatusFrame`。
 */
import type { NormalizedMessage, SessionStatusFrame } from "@/protocol";
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
import { apiGet } from "@/lib/api";
import { normalizedMessageToEvents } from "./normalizedMessageToEvents";

export class ClaudeChatProvider implements ChatProvider {
  readonly provider = "claude" as const;

  async send(handle: NetworkHandle, request: SendRequest): Promise<void> {
    if (request.provider.provider !== "claude") {
      throw new Error(
        `[ClaudeChatProvider] 收到非 claude 的 SendRequest: ${request.provider.provider}`,
      );
    }
    const opt = request.provider;
    handle.send({
      frame_type: "claude-command",
      command: request.common.text,
      options: {
        cwd: request.common.cwd ?? undefined,
        sessionId: opt.sessionId ?? undefined,
        resume: opt.resume ?? undefined,
        model: opt.model ?? undefined,
        reasoningEffort: request.common.reasoningEffort ?? undefined,
      },
      attachments: request.common.attachments,
    });
  }

  async loadHistory(request: HistoryLoadRequest): Promise<ChatHistoryBatch> {
    // claude 历史真源：后端 src/web/claude_code/jsonl_history.py，
    // 经 REST `/api/threads/{threadId}/claude_history` 返回 NormalizedMessage[]，
    // 复用 `normalizedMessageToEvents` 翻成统一 ChatEvent（与现状 ClaudeCodeView 同端点）。
    const { messages } = await apiGet<{ messages: NormalizedMessage[] }>(
      `/api/threads/${request.threadId}/claude_history`,
    );
    const events = messages.flatMap((m) =>
      // 历史无 receivedAt：用各 NormalizedMessage 自带 timestamp，缺失回退 0。
      normalizedMessageToEvents("claude", request.threadId, m, 0),
    );
    return {
      threadId: request.threadId,
      provider: "claude",
      events,
      hasMore: false,
    };
  }

  mapInboundFrame(envelope: RawFrameEnvelope): ChatEvent[] {
    const frame = envelope.frame as NormalizedMessage | SessionStatusFrame;
    const tid = envelope.threadId ?? envelope.connectionId;

    if (frame.frame_type === "session-status") {
      // check-session-status 的应答；骨架转成 status 事件（真实活跃态由 #3 manager 消费）。
      return [
        {
          kind: "status",
          provider: "claude",
          threadId: tid,
          turnId: frame.sessionId,
          createdAt: envelope.receivedAt,
          payload: { sessionStatus: true, isProcessing: frame.isProcessing },
        },
      ];
    }
    return normalizedMessageToEvents("claude", tid, frame, envelope.receivedAt);
  }

  async interrupt(handle: NetworkHandle, request: InterruptRequest): Promise<void> {
    if (!request.sessionId) {
      // claude 打断必须带 sessionId；无则无 active session 可断，静默返回。
      return;
    }
    handle.send({ frame_type: "abort-session", sessionId: request.sessionId });
  }

  async checkSessionStatus(
    request: SessionStatusRequest,
  ): Promise<SessionStatus> {
    // 骨架待决：真实活跃态需发 `{ frame_type:"check-session-status", sessionId }`
    // 再等 `session-status` 帧回执——本接口签名缺 handle，跨帧请求/响应无法在此完成。
    // 暂返回 inactive 占位；接口是否补 handle 参数留 #3 ChatManager 拍板。
    return {
      active: false,
      message: `claude session status（thread ${request.threadId}）待 ChatManager 经 WS 查询补全（骨架占位）`,
    };
  }
}
