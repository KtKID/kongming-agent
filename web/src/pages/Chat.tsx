import { useEffect, useMemo } from "react";
import { useParams } from "react-router-dom";
import { ThreadList } from "@/components/ThreadList";
import { MessageList } from "@/components/MessageList";
import { Composer } from "@/components/Composer";
import { ApprovalDialog } from "@/components/ApprovalDialog";
import {
  WhiteboardPanel,
  type WhiteboardCardItem,
} from "@/components/WhiteboardPanel";
import { useWS } from "@/hooks/useWS";
import { useStreamingRender } from "@/hooks/useStreamingRender";
import { useChatStore } from "@/stores/chat";
import { useWhiteboardStore } from "@/stores/whiteboard";

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
  const boardTitle = useWhiteboardStore((s) => s.boardTitle);
  const cards = useWhiteboardStore((s) => s.cards);
  const fetchBoard = useWhiteboardStore((s) => s.fetchBoard);
  const createCard = useWhiteboardStore((s) => s.createCard);
  const updateCardContentLocal = useWhiteboardStore((s) => s.updateCardContentLocal);
  const updateCardMetaLocal = useWhiteboardStore((s) => s.updateCardMetaLocal);
  const updateCardLayoutLocal = useWhiteboardStore((s) => s.updateCardLayoutLocal);
  const bringCardToFront = useWhiteboardStore((s) => s.bringCardToFront);
  const toggleCollapsed = useWhiteboardStore((s) => s.toggleCollapsed);
  const deleteCard = useWhiteboardStore((s) => s.deleteCard);

  const items = useChatStore((s) =>
    threadId ? (s.itemsByThread[threadId] ?? null) : null,
  );
  const whiteboardCards = useMemo<WhiteboardCardItem[]>(
    () =>
      cards.map((card) => ({
        id: card.id,
        title: card.title,
        category: card.category,
        content: card.content,
        collapsed: card.collapsed,
        x: card.x,
        y: card.y,
        zIndex: card.zIndex,
        height: card.height,
        updatedLabel: card.saving
          ? "保存中"
          : card.error
            ? "保存失败"
            : "已同步",
      })),
    [cards],
  );
  const lastAssistantStreaming = useMemo(() => {
    if (!items) return false;
    for (let i = items.length - 1; i >= 0; i--) {
      const it = items[i]!;
      if (it.kind === "assistant") return it.streaming;
    }
    return false;
  }, [items]);

  useEffect(() => {
    void fetchBoard();
  }, [fetchBoard]);

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

  const onCreateWhiteboardCard = async () => {
    await createCard({
      title: `新卡片 ${cards.length + 1}`,
      category: "note",
      content: [
        "# 新建卡片",
        "",
        "- [ ] 这里是 workspace 级 markdown 文件",
        "- [ ] 可以继续补待办、细节或笔记",
        "",
        "> 这张卡片会保存在当前 workspace 的 whiteboard/cards 目录",
      ].join("\n"),
      x: 24 + cards.length * 18,
      y: 24 + cards.length * 18,
      height: 280,
    });
  };

  const onUpdateWhiteboardCard = (
    cardId: string,
    patch: Partial<WhiteboardCardItem>,
  ) => {
    if (typeof patch.title === "string" || typeof patch.category === "string") {
      updateCardMetaLocal(cardId, {
        ...(typeof patch.title === "string" ? { title: patch.title } : {}),
        ...(typeof patch.category === "string" ? { category: patch.category } : {}),
      });
    }
    if (typeof patch.content === "string") {
      updateCardContentLocal(cardId, patch.content);
    }
  };

  const onToggleWhiteboardCard = (cardId: string) => {
    toggleCollapsed(cardId);
  };

  const onDeleteWhiteboardCard = async (cardId: string) => {
    await deleteCard(cardId);
  };

  const onUpdateWhiteboardCardLayout = (
    cardId: string,
    patch: Partial<Pick<WhiteboardCardItem, "x" | "y" | "height" | "zIndex">>,
  ) => {
    updateCardLayoutLocal(cardId, {
      ...(typeof patch.x === "number" ? { x: patch.x } : {}),
      ...(typeof patch.y === "number" ? { y: patch.y } : {}),
      ...(typeof patch.height === "number" ? { height: patch.height } : {}),
      ...(typeof patch.zIndex === "number" ? { zIndex: patch.zIndex } : {}),
    });
  };

  return (
    <div className="flex h-full min-w-0">
      <ThreadList />
      <div className="flex min-w-0 flex-1 overflow-hidden bg-background">
        <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <div className="min-h-0 flex-1">
            <MessageList threadId={threadId} />
          </div>
          <Composer
            disabled={!threadId || !socket || lastAssistantStreaming}
            onSubmit={onSend}
            threadId={threadId}
          />
        </div>
        <WhiteboardPanel
          title={boardTitle}
          cards={threadId ? whiteboardCards : []}
          canCreate={Boolean(threadId)}
          onCreateCard={threadId ? onCreateWhiteboardCard : undefined}
          onToggleCollapse={onToggleWhiteboardCard}
          onDeleteCard={onDeleteWhiteboardCard}
          onUpdateCard={onUpdateWhiteboardCard}
          onUpdateCardLayout={onUpdateWhiteboardCardLayout}
          onBringToFront={bringCardToFront}
          emptyTitle={threadId ? "还没有白板卡片" : "先打开一个会话"}
          emptyDescription={
            threadId
              ? "卡片内容会落到当前 workspace；右侧区域是 markdown 便签白板。"
              : "白板是 workspace 级区域。先在左侧进入一个 thread，再开始摆放卡片。"
          }
        />
      </div>
      <ApprovalDialog socket={socket} />
    </div>
  );
}
