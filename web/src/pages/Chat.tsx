import { useMemo } from "react";
import { useParams } from "react-router-dom";
import { ThreadList } from "@/components/ThreadList";
import { MessageList } from "@/components/MessageList";
import { Composer } from "@/components/Composer";
import { ApprovalDialog } from "@/components/ApprovalDialog";
import { useWS } from "@/hooks/useWS";
import { useStreamingRender } from "@/hooks/useStreamingRender";
import { useChatStore } from "@/stores/chat";

/**
 * Chat 页：
 * - 左 24rem ThreadList
 * - 右上 MessageList（滚动）
 * - 右下 Composer（固定底部）
 * - 全局 ApprovalDialog（监听 pending）
 *
 * useParams<thread_id> 切换右侧；ws 在 hook 内部按 threadId 重建。
 */
export function ChatPage() {
  const params = useParams<{ thread_id?: string }>();
  const threadId = params.thread_id;
  const { socket } = useWS(threadId);
  useStreamingRender(threadId, socket);

  const appendUser = useChatStore((s) => s.appendUser);

  const items = useChatStore((s) =>
    threadId ? (s.itemsByThread[threadId] ?? null) : null,
  );
  const lastAssistantStreaming = useMemo(() => {
    if (!items) return false;
    for (let i = items.length - 1; i >= 0; i--) {
      const it = items[i]!;
      if (it.kind === "assistant") return it.streaming;
    }
    return false;
  }, [items]);

  const onSend = (text: string, reasoningEffort: "low" | "medium" | "high" | null) => {
    if (!threadId || !socket) return;
    appendUser(threadId, text);
    socket.send({
      kind: "user.input",
      text,
      request_id: `req-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      ...(reasoningEffort ? { reasoning_effort: reasoningEffort } : {}),
    });
  };

  return (
    <div className="flex h-full">
      <ThreadList />
      <div className="flex flex-1 flex-col overflow-hidden">
        <div className="min-h-0 flex-1">
          <MessageList threadId={threadId} />
        </div>
        <Composer
          disabled={!threadId || !socket || lastAssistantStreaming}
          onSubmit={onSend}
          threadId={threadId}
        />
      </div>
      <ApprovalDialog socket={socket} />
    </div>
  );
}
