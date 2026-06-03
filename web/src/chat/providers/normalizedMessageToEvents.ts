/**
 * message-runtime-v0.1 · NormalizedMessage → ChatEvent 共享映射（#2 骨架）
 *
 * Claude 与 Codex 入站都是统一的 `NormalizedMessage`（后端 normalizer 产出），
 * 所以两条频道的 `mapInboundFrame` 复用同一份翻译，差异只在 provider 标识。
 *
 * 真源：`@/protocol::NormalizedMessage`（字段语义见其 JSDoc）。
 *
 * 骨架范围：覆盖 text / thinking / tool_use / tool_result / complete / error 主路径。
 * 待 #6 细化：stream_status / stream_delta 的增量拆分、permission_request 审批链路、
 * 精确 turn 归并（NormalizedMessage 无显式 turn 字段，骨架先用 sessionId 近似）。
 */
import type { NormalizedMessage } from "@/protocol";
import type { ChatEvent, ChatProviderKind } from "@/chat/types";

/**
 * NormalizedMessage 无显式 turn 字段，骨架用 `sessionId` 作 turn 归并近似键。
 * 缺 sessionId 时回退到 threadId（单 turn 退化）。TODO #6：接入精确 turn 边界。
 */
function turnIdOf(threadId: string, msg: NormalizedMessage): string {
  return msg.sessionId ?? threadId;
}

function toMs(msg: NormalizedMessage, fallback: number): number {
  if (!msg.timestamp) return fallback;
  const parsed = Date.parse(msg.timestamp);
  return Number.isNaN(parsed) ? fallback : parsed;
}

/** 把单条 NormalizedMessage 翻成 0..N 个 ChatEvent。 */
export function normalizedMessageToEvents(
  provider: Extract<ChatProviderKind, "claude" | "codex">,
  threadId: string,
  msg: NormalizedMessage,
  receivedAt: number,
): ChatEvent[] {
  const turnId = turnIdOf(threadId, msg);
  const createdAt = toMs(msg, receivedAt);
  const base = { provider, threadId, turnId, createdAt } as const;

  switch (msg.frame_type) {
    case "text":
      return [
        {
          ...base,
          kind: "assistant_message_delta",
          messageId: msg.id,
          payload: { delta: msg.content, role: msg.role ?? "assistant" },
        },
      ];
    case "thinking":
      return [
        {
          ...base,
          kind: "assistant_message_delta",
          messageId: msg.id,
          payload: { reasoningDelta: msg.content },
        },
      ];
    case "tool_use":
      return [
        {
          ...base,
          kind: "tool_call_started",
          toolCallId: msg.toolId,
          payload: { toolName: msg.toolName, arguments: msg.toolInput },
        },
      ];
    case "tool_result":
      return [
        {
          ...base,
          kind: "tool_call_completed",
          toolCallId: msg.toolId,
          payload: { content: msg.content, isError: msg.isError ?? false },
        },
      ];
    case "complete":
      return [
        {
          ...base,
          kind: "turn_completed",
          payload: { aborted: msg.aborted ?? false, exitCode: msg.exitCode },
        },
      ];
    case "error":
      return [{ ...base, kind: "error", payload: { message: msg.error } }];
    // session_created / stream_status / stream_delta / permission_request：
    // 骨架暂不进时间线（session 绑定与审批链路本 task 不收编）。TODO #6。
    default:
      return [];
  }
}
