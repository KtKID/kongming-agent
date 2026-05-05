import { useEffect } from "react";
import { toast } from "sonner";
import {
  appendDelta,
  clearBuffers,
  flushTurn,
  useChatStore,
} from "@/stores/chat";
import { useApprovalDialogStore } from "@/hooks/useApprovalDialog";
import type { ThreadSocket } from "@/lib/ws";

/**
 * kongming-agent v0.1.5 流式渲染 hook
 *
 * 职责：把 ThreadSocket 收到的 15 类 S2C 帧 dispatch 到 chat store / approval queue / toast。
 *
 * ## 帧 → 行为映射
 *
 * | 帧 | 行为 |
 * |---|---|
 * | thread.history     | setHistory（替换列表，不追加）|
 * | content.delta      | appendDelta(content)（rAF 累积）|
 * | reasoning.delta    | appendDelta(reasoning)|
 * | turn.start         | 无（已有 placeholder 由 delta 创建）|
 * | turn.end           | flushTurn（标 streaming=false + 清 buffer）|
 * | assistant.final    | appendAssistantFinal（覆盖 streaming 累积值）|
 * | system.notice      | appendSystemNotice（按 noticeKey + runId 原地更新）|
 * | tool.call.start    | appendToolStart（item ok=null）|
 * | tool.call.end      | appendToolEnd（写 ok / errorMessage）|
 * | approval.request   | push 到 approval 队列 + chat store appendApprovalRequest|
 * | approval.decision  | 无（UI 已通过 ack 反馈给用户）|
 * | usage              | 无（v0.1.5 不显示 token 用量）|
 * | error              | appendError 横幅 + toast.error|
 * | pong               | 无（连接活性指标）|
 * | cell.evicted       | toast.warning + clear 该 thread 的 buffer|
 *
 * ## StrictMode 安全
 *
 * - 内部用 ws.on() 注册 listener；effect cleanup 时反注册
 * - threadId 切换时 cleanup 旧 listener + 创建新 listener
 */
export function useStreamingRender(
  threadId: string | undefined,
  socket: ThreadSocket | null,
) {
  const setHistory = useChatStore((s) => s.setHistory);
  const appendAssistantFinal = useChatStore((s) => s.appendAssistantFinal);
  const appendSystemNotice = useChatStore((s) => s.appendSystemNotice);
  const appendToolStart = useChatStore((s) => s.appendToolStart);
  const appendToolEnd = useChatStore((s) => s.appendToolEnd);
  const appendApprovalRequest = useChatStore((s) => s.appendApprovalRequest);
  const appendError = useChatStore((s) => s.appendError);
  const appendUsage = useChatStore((s) => s.appendUsage);
  const pushApproval = useApprovalDialogStore((s) => s.push);

  useEffect(() => {
    if (!threadId || !socket) return;

    const off = socket.on((frame) => {
      switch (frame.kind) {
        case "thread.history":
          setHistory(threadId, frame.messages);
          break;
        case "content.delta":
          appendDelta(
            threadId,
            "content",
            frame.delta,
            frame.turn,
            frame.run_id ?? "",
          );
          break;
        case "reasoning.delta":
          appendDelta(
            threadId,
            "reasoning",
            frame.delta,
            frame.turn,
            frame.run_id ?? "",
          );
          break;
        case "turn.start":
          // 占位由首个 delta 创建；此处无操作
          break;
        case "turn.end":
          flushTurn(threadId, frame.turn, frame.run_id ?? "");
          break;
        case "assistant.final":
          appendAssistantFinal(threadId, frame);
          break;
        case "system.notice":
          appendSystemNotice(threadId, frame);
          break;
        case "tool.call.start":
          appendToolStart(threadId, frame);
          break;
        case "tool.call.end":
          appendToolEnd(threadId, frame);
          break;
        case "approval.request":
          appendApprovalRequest(threadId, frame);
          pushApproval(frame);
          break;
        case "approval.decision":
          // UI 已显式响应；不再追加项
          break;
        case "usage":
          appendUsage(threadId, frame);
          break;
        case "error":
          appendError(threadId, frame);
          toast.error(frame.message);
          break;
        case "pong":
          // 心跳 ack；不写 store
          break;
        case "cell.evicted":
          toast.warning(
            `cell 已回收（${frame.reason}）：${frame.message ?? ""}`,
          );
          clearBuffers(threadId);
          break;
        default: {
          // 穷尽性检查
          const _exhaustive: never = frame;
          void _exhaustive;
        }
      }
    });

    return () => {
      off();
    };
  }, [
    threadId,
    socket,
    setHistory,
    appendAssistantFinal,
    appendSystemNotice,
    appendToolStart,
    appendToolEnd,
    appendApprovalRequest,
    appendError,
    appendUsage,
    pushApproval,
  ]);
}
