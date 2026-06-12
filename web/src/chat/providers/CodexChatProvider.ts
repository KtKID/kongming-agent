/**
 * message-runtime-v0.1 · CodexChatProvider（#2 骨架）
 *
 * 发送走 `codex-command`，打断走 `abort-session`，入站 `NormalizedMessage`
 * 复用 `normalizedMessageToEvents`。传输仍走现有 `/ws/codex`（CodexSocket）。
 *
 * 真源：`@/protocol::NormalizedMessage / SessionStatusFrame` + `lib/codex-ws.ts`
 * 的 `codex-command` 帧（含 permissionMode）。
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

export class CodexChatProvider implements ChatProvider {
  readonly provider = "codex" as const;

  async send(handle: NetworkHandle, request: SendRequest): Promise<void> {
    if (request.provider.provider !== "codex") {
      throw new Error(
        `[CodexChatProvider] 收到非 codex 的 SendRequest: ${request.provider.provider}`,
      );
    }
    const opt = request.provider;
    handle.send({
      frame_type: "codex-command",
      command: request.common.text,
      options: {
        cwd: request.common.cwd ?? undefined,
        sessionId: opt.sessionId ?? undefined,
        resume: opt.resume ?? undefined,
        model: opt.model ?? undefined,
        permissionMode: opt.permissionMode,
        // #8: reasoning 三频道贯通——把 common 的 reasoningEffort 透传到 wire；
        // codex 后端目前忽略此字段（独立后端 task），前端契约不断链。
        reasoningEffort: request.common.reasoningEffort ?? undefined,
      },
      attachments: request.common.attachments,
    });
  }

  async loadHistory(request: HistoryLoadRequest): Promise<ChatHistoryBatch> {
    // codex 历史真源：后端 src/web/codex/jsonl_history.py，端点
    // `/api/codex/sessions/{codex_thread_id}/history`。
    //
    // ⚠️ codex_thread_id ≠ threadId：它是 thread metadata 里落盘的 resume sid
    // （现状 CodexView 取 `thread.codex_thread_id`）。`HistoryLoadRequest.codexThreadId`
    // 是 codex 频道专有的正式字段，调用方（ChatManager / CodexView）从 metadata 透传。
    const codexThreadId = request.codexThreadId?.trim();
    if (!codexThreadId) {
      // 缺 codex_thread_id（thread 还没产生 resume sid）→ 无历史可拉。
      return {
        threadId: request.threadId,
        provider: "codex",
        events: [],
        hasMore: false,
      };
    }
    const { messages } = await apiGet<{ messages: NormalizedMessage[] }>(
      `/api/codex/sessions/${encodeURIComponent(codexThreadId)}/history`,
    );
    // 注意 codex jsonl 存 base64 图片而非 asset_id，历史附件回显是独立后续 task
    // （见 lib/codex-ws.ts 注释）；本轮只回放 text / tool / thinking 主路径。
    const events = messages.flatMap((m) =>
      normalizedMessageToEvents("codex", request.threadId, m, 0),
    );
    return {
      threadId: request.threadId,
      provider: "codex",
      events,
      hasMore: false,
    };
  }

  mapInboundFrame(envelope: RawFrameEnvelope): ChatEvent[] {
    const frame = envelope.frame as NormalizedMessage | SessionStatusFrame;
    const tid = envelope.threadId ?? envelope.connectionId;

    if (frame.frame_type === "session-status") {
      return [
        {
          kind: "status",
          provider: "codex",
          threadId: tid,
          turnId: frame.sessionId,
          createdAt: envelope.receivedAt,
          payload: { sessionStatus: true, isProcessing: frame.isProcessing },
        },
      ];
    }
    return normalizedMessageToEvents("codex", tid, frame, envelope.receivedAt);
  }

  async interrupt(handle: NetworkHandle, request: InterruptRequest): Promise<void> {
    if (!request.sessionId) return;
    handle.send({ frame_type: "abort-session", sessionId: request.sessionId });
  }

  async checkSessionStatus(
    request: SessionStatusRequest,
  ): Promise<SessionStatus> {
    // 同 ClaudeChatProvider：跨帧请求/响应，接口缺 handle，骨架返回占位。留 #3 拍板。
    return {
      active: false,
      message: `codex session status（thread ${request.threadId}）待 ChatManager 经 WS 查询补全（骨架占位）`,
    };
  }
}
