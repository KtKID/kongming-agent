import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { LeftSidebar } from "@/components/LeftSidebar";
import { MessageList } from "@/components/MessageList";
import { Composer } from "@/components/Composer";
import { ApprovalDialog } from "@/components/ApprovalDialog";
import { ClaudeCodeView } from "@/components/ClaudeCodeView";
import { CodexView } from "@/components/CodexView";
import { WorkspaceFilesPanel } from "@/components/WorkspaceFilesPanel";
import { WorkspaceGitPanel } from "@/components/WorkspaceGitPanel";
import { WorkspaceShellPanel } from "@/components/WorkspaceShellPanel";
import { WorkspaceTabs } from "@/components/WorkspaceTabs";
import {
  WhiteboardPanel,
  type WhiteboardCardItem,
} from "@/components/WhiteboardPanel";
import { useWS } from "@/hooks/useWS";
import { useStreamingRender } from "@/hooks/useStreamingRender";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/stores/chat";
import { useThreadsStore } from "@/stores/threads";
import { useWhiteboardStore } from "@/stores/whiteboard";
import { useWorkspaceStore } from "@/stores/workspace";

/**
 * Chat 页：
 * - 左 24rem ThreadList
 * - 右：根据 thread.backend_kind 分支：
 *   - generic_chat → MessageList + Composer + ApprovalDialog（v0.1.5 既有）
 *   - claude_code  → ClaudeCodeView（v0.1.6 新增；走 /ws/claude-code）
 *
 * useParams<thread_id> 切换右侧；ws 在 hook 内部按 threadId 重建。
 */
export function ChatPage() {
  const params = useParams<{ thread_id?: string }>();
  const threadId = params.thread_id;
  const thread = useThreadsStore((s) =>
    threadId ? s.threads.find((t) => t.id === threadId) : undefined,
  );
  const pendingNewClaudeSession = useThreadsStore((s) => s.pendingNewClaudeSession);
  const backendKind = thread?.backend_kind ?? "generic_chat";
  const isClaudeCode = backendKind === "claude_code";
  const isCodex = backendKind === "codex";

  // generic_chat 路径：维持原 ws + streaming 连接（不动）
  // claude_code 路径：传 undefined 让 useWS 不连，避免对错路径开 ws
  // thread 还没 fetch 完成时（store 暂为 undefined）也不连 —— 否则 backend_kind
  // 默认值 generic_chat 会触发错误的 /ws/threads/{id} 连接（claude_code thread
  // 被后端 403 拒）。fetch 完成 re-render 后会用真实 backend_kind 重新评估。
  const genericWsId = thread && !isClaudeCode && !isCodex ? threadId : undefined;
  const { socket } = useWS(genericWsId);
  useStreamingRender(genericWsId, socket);
  const [isMobileLayout, setIsMobileLayout] = useState(false);
  const [isCompactLayout, setIsCompactLayout] = useState(false);
  const [isLeftSidebarOpen, setIsLeftSidebarOpen] = useState(true);
  const [isWhiteboardOpen, setIsWhiteboardOpen] = useState(true);

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
  const fetchWorkspaceContext = useWorkspaceStore((s) => s.fetchContext);
  const setActiveWorkspaceTab = useWorkspaceStore((s) => s.setActiveTab);
  const requestOpenWorkspaceFile = useWorkspaceStore((s) => s.requestOpenFile);
  const activeWorkspaceTab = useWorkspaceStore((s) =>
    threadId ? (s.activeTabByThread[threadId] ?? "chat") : "chat",
  );
  const workspaceContext = useWorkspaceStore((s) =>
    threadId ? s.contextsByThread[threadId] : undefined,
  );
  const workspaceLoading = useWorkspaceStore((s) =>
    threadId ? Boolean(s.loadingByThread[threadId]) : false,
  );
  const workspaceFileOpenRequest = useWorkspaceStore((s) =>
    threadId ? s.fileOpenRequestByThread[threadId] : undefined,
  );

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

  useEffect(() => {
    if (!threadId) return;
    void fetchWorkspaceContext(threadId);
  }, [fetchWorkspaceContext, threadId]);

  useEffect(() => {
    const syncLayout = () => {
      const mobile = window.innerWidth < 768;
      const compact = window.innerWidth < 1080;
      setIsMobileLayout(mobile);
      setIsCompactLayout(compact);
      if (compact) {
        setIsLeftSidebarOpen(false);
        setIsWhiteboardOpen(false);
      }
    };
    syncLayout();
    window.addEventListener("resize", syncLayout);
    return () => window.removeEventListener("resize", syncLayout);
  }, []);

  const toggleLeftSidebar = () => {
    setIsLeftSidebarOpen((open) => {
      const nextOpen = !open;
      if (nextOpen && isMobileLayout) setIsWhiteboardOpen(false);
      return nextOpen;
    });
  };

  const toggleWhiteboard = () => {
    setIsWhiteboardOpen((open) => {
      const nextOpen = !open;
      if (nextOpen && isMobileLayout) setIsLeftSidebarOpen(false);
      return nextOpen;
    });
  };

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
    <div className="flex h-full min-w-0 overflow-hidden">
      <LeftSidebar
        isOpen={isLeftSidebarOpen}
        compactMode={isCompactLayout}
        mobileMode={isMobileLayout}
        onToggleOpen={toggleLeftSidebar}
      />
      <div className="relative flex min-w-0 flex-1 overflow-hidden bg-background">
        <div
          className={cn(
            "flex min-w-[18rem] flex-1 flex-col overflow-hidden",
            isCompactLayout && !isMobileLayout ? "px-[4.75rem]" : "px-0",
          )}
        >
          {threadId ? (
            <div className="border-b border-border px-4 py-3">
              <WorkspaceTabs
                active={activeWorkspaceTab}
                onChange={(tab) => setActiveWorkspaceTab(threadId, tab)}
                threadId={thread?.claude_thread_id || thread?.codex_thread_id || threadId}
              />
            </div>
          ) : null}
          <div className="min-h-0 flex-1">
            {activeWorkspaceTab === "files" ? (
              <WorkspaceFilesPanel
                context={workspaceContext}
                loading={workspaceLoading}
                openRequest={workspaceFileOpenRequest}
              />
            ) : activeWorkspaceTab === "git" ? (
              <WorkspaceGitPanel
                context={workspaceContext}
                loading={workspaceLoading}
                onOpenFile={(path) => {
                  if (!threadId) return;
                  requestOpenWorkspaceFile(threadId, path);
                  setActiveWorkspaceTab(threadId, "files");
                }}
              />
            ) : activeWorkspaceTab === "shell" ? (
              <WorkspaceShellPanel
                context={workspaceContext}
                loading={workspaceLoading}
              />
            ) : !threadId && pendingNewClaudeSession ? (
              <ClaudeCodeView />
            ) : isClaudeCode && threadId ? (
              <ClaudeCodeView threadId={threadId} thread={thread} />
            ) : isCodex && threadId ? (
              <CodexView threadId={threadId} thread={thread} />
            ) : (
              <div className="flex h-full min-h-0 flex-col overflow-hidden">
                <div className="min-h-0 flex-1">
                  <MessageList threadId={threadId} />
                </div>
                <Composer
                  disabled={!threadId || !socket || lastAssistantStreaming}
                  onSubmit={onSend}
                  threadId={threadId}
                />
              </div>
            )}
          </div>
        </div>
        <WhiteboardPanel
          title={boardTitle}
          cards={threadId ? whiteboardCards : []}
          isOpen={isWhiteboardOpen}
          compactMode={isCompactLayout}
          mobileMode={isMobileLayout}
          canCreate={Boolean(threadId)}
          onToggleOpen={toggleWhiteboard}
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
      {isClaudeCode || isCodex || activeWorkspaceTab !== "chat" ? null : (
        <ApprovalDialog socket={socket} />
      )}
    </div>
  );
}
