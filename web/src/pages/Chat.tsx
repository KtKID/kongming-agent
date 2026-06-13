import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";
import { Workflow } from "lucide-react";
import { ApprovalDialog, type ApprovalAckSocket } from "@/components/ApprovalDialog";
import { ClaudeCodeView } from "@/components/ClaudeCodeView";
import { CodexView } from "@/components/CodexView";
import { Composer } from "@/components/Composer";
import { FileDrawer } from "@/components/FileDrawer";
import { GenericEmptyThreadView } from "@/components/GenericEmptyThreadView";
import { LeftSidebar } from "@/components/LeftSidebar";
import { MessageList } from "@/components/MessageList";
import { ModelSwitcher } from "@/components/ModelSwitcher";
import { ThreadTaskProgressPopover } from "@/components/ThreadTaskProgressPopover";
import { WhiteboardPanel, type WhiteboardCardItem } from "@/components/WhiteboardPanel";
import { WorkspaceDock, type WorkspaceDockTab } from "@/components/WorkspaceDock";
import { WorkspaceFilesPanel } from "@/components/WorkspaceFilesPanel";
import { WorkspaceGitPanel } from "@/components/WorkspaceGitPanel";
import { WorkspaceShellPanel } from "@/components/WorkspaceShellPanel";
import { AutoApprovalToggle } from "@/features/auto-approval/AutoApprovalToggle";
import type { AutoApprovalSocket } from "@/features/auto-approval/useAutoApproval";
import { useApprovalDialogStore } from "@/hooks/useApprovalDialog";
import { useChatLayout } from "@/hooks/useChatLayout";
import {
  useClientConfig,
  type ClientRuntimeConfig,
} from "@/hooks/useClientConfig";
import { buildWhiteboardCardDraft, type WhiteboardCardKind } from "@/lib/whiteboard-card-templates";
import { cn } from "@/lib/utils";
import { ChatManager } from "@/chat/ChatManager";
import { makeNetworkHandle, getTimelineStore, useChatTimeline } from "@/chat/runtimeWiring";
import { toViewModel, toGenericRenderItems } from "@/chat/ChatRenderAdapter";
import type { RawFrameEnvelope } from "@/chat/types";
import { networkManager } from "@/network";
import type { ChannelHandle, SocketState } from "@/network";
import type { CardScope } from "@/protocol";
import { useChatStore } from "@/stores/chat";
import { useConnectionStatusStore } from "@/stores/connectionStatus";
import { useThreadsStore } from "@/stores/threads";
import { useModelProvidersStore } from "@/modules/model-providers/store";
import { useThreadRunning } from "@/hooks/useThreadRunning";
import { useWhiteboardStore } from "@/stores/whiteboard";
import { useWorkspaceStore } from "@/stores/workspace";

function isResolvedHeartbeatConfig(
  config: ClientRuntimeConfig["heartbeat"] | undefined,
): config is {
  intervalMs: number;
  backgroundIntervalMs: number;
  timeoutMs: number;
  maxMissed: number;
} {
  return (
    typeof config?.intervalMs === "number" &&
    Number.isFinite(config.intervalMs) &&
    config.intervalMs > 0 &&
    typeof config.backgroundIntervalMs === "number" &&
    Number.isFinite(config.backgroundIntervalMs) &&
    config.backgroundIntervalMs > 0 &&
    typeof config.timeoutMs === "number" &&
    Number.isFinite(config.timeoutMs) &&
    config.timeoutMs > 0 &&
    typeof config.maxMissed === "number" &&
    Number.isFinite(config.maxMissed) &&
    config.maxMissed >= 1
  );
}

export function ChatPage() {
  const { isMobileLayout, isCompactLayout } = useChatLayout();
  const useCompactToolbar = isCompactLayout;

  const params = useParams<{ thread_id?: string }>();
  const navigate = useNavigate();
  const threadId = params.thread_id;
  const thread = useThreadsStore((s) =>
    threadId ? s.threads.find((item) => item.id === threadId) : undefined,
  );
  const pendingNewSession = useThreadsStore((s) => s.pendingNewSession);
  const setPendingNewSession = useThreadsStore((s) => s.setPendingNewSession);
  const updateThreadPreset = useThreadsStore((s) => s.updateThreadPreset);
  const modelFamilies = useModelProvidersStore((s) => s.modelFamilies);
  const loadModelFamilies = useModelProvidersStore((s) => s.loadModelFamilies);
  const backendKind = thread?.backend_kind ?? "generic_chat";
  const isClaudeCode = backendKind === "claude_code";
  const isCodex = backendKind === "codex";
  const isGenericPendingBlank =
    !threadId && pendingNewSession?.backendKind === "generic_chat";

  const genericWsId = thread && !isClaudeCode && !isCodex ? threadId : undefined;
  const clientConfig = useClientConfig();
  const heartbeatConfig = clientConfig?.heartbeat;
  const [genericHandle, setGenericHandle] = useState<ChannelHandle | null>(null);
  const [genericChannelState, setGenericChannelState] =
    useState<SocketState>("closed");
  const setThreadWsActive = useConnectionStatusStore((s) => s.setThreadWsActive);
  const setThreadWsState = useConnectionStatusStore((s) => s.setThreadWsState);
  const setThreadWsLatency = useConnectionStatusStore((s) => s.setThreadWsLatency);

  const autoApprovalSocket = useMemo<AutoApprovalSocket | null>(() => {
    if (!genericHandle) return null;
    return {
      send: (frame) => {
        if (genericChannelState !== "open") return false;
        genericHandle.send(frame);
        return true;
      },
    };
  }, [genericHandle, genericChannelState]);

  const approvalSocket = useMemo<ApprovalAckSocket | null>(() => {
    if (!genericHandle) return null;
    return {
      send: (frame) => {
        if (genericChannelState !== "open") return false;
        genericHandle.send(frame);
        return true;
      },
    };
  }, [genericHandle, genericChannelState]);

  const [isLeftSidebarOpen, setIsLeftSidebarOpen] = useState(!isCompactLayout);
  const [mountedWorkspaceTabs, setMountedWorkspaceTabs] = useState<
    Partial<Record<WorkspaceDockTab, boolean>>
  >({ chat: true });

  // chat-receive-side-unify #5：appendUser 退役——用户气泡改由 ChatManager.sendMessage
  // 往时间线灌 user_message 事件（见下方 onSend）。底栏 token 仍由 appendUsage 喂养
  // （useChatStore.usageByThread 是 StatusLine 数据源，与气泡页脚是两个展示位）。
  const fetchThreadUsage = useChatStore((s) => s.fetchThreadUsage);
  const appendUsage = useChatStore((s) => s.appendUsage);
  const pushApproval = useApprovalDialogStore((s) => s.push);
  const globalTitle = useWhiteboardStore((s) => s.globalTitle);
  const projectTitle = useWhiteboardStore((s) => s.projectTitle);
  const cards = useWhiteboardStore((s) => s.cards);
  const fetchBoard = useWhiteboardStore((s) => s.fetchBoard);
  const clearBoard = useWhiteboardStore((s) => s.clearBoard);
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

  const effectiveCwd = useMemo(() => {
    const threadCwd = thread?.cwd?.trim();
    if (threadCwd) return threadCwd;
    return workspaceContext?.workspace_root?.trim() ?? "";
  }, [thread?.cwd, workspaceContext?.workspace_root]);

  const whiteboardCards = useMemo<WhiteboardCardItem[]>(
    () =>
      cards.map((card) => ({
        id: card.id,
        scope: card.scope,
        title: card.title,
        category: card.category,
        content: card.content,
        collapsed: card.collapsed,
        x: card.x,
        y: card.y,
        zIndex: card.zIndex,
        height: card.height,
        updatedLabel: card.saving ? "Saving" : card.error ? "Save failed" : "Synced",
      })),
    [cards],
  );

  // chat-running-state-unify #2：generic 频道的「是否运行中」改用共享 hook
  // （后端 thread-status phase 为唯一真源）。原前端推导的 lastAssistantStreaming
  // 在对话结束后不可靠复位，会把停止按钮卡住——已删除。
  const isRunning = useThreadRunning(threadId);

  useEffect(() => {
    if (threadId) {
      void fetchBoard(threadId);
      return;
    }
    clearBoard();
  }, [clearBoard, fetchBoard, threadId]);

  useEffect(() => {
    if (!threadId) return;
    void fetchWorkspaceContext(threadId);
  }, [fetchWorkspaceContext, threadId]);

  useEffect(() => {
    if (!threadId) return;
    void fetchThreadUsage(threadId);
  }, [fetchThreadUsage, threadId]);

  useEffect(() => {
    if (backendKind !== "generic_chat") return;
    void loadModelFamilies();
  }, [backendKind, loadModelFamilies]);

  useEffect(() => {
    setMountedWorkspaceTabs({ chat: true });
  }, [threadId]);

  useEffect(() => {
    if (!threadId) return;
    setMountedWorkspaceTabs((prev) => ({ ...prev, [activeWorkspaceTab]: true }));
  }, [activeWorkspaceTab, threadId]);

  useEffect(() => {
    if (!threadId || !thread || !pendingNewSession) return;
    setPendingNewSession(null);
  }, [threadId, thread, pendingNewSession, setPendingNewSession]);

  useEffect(() => {
    if (isCompactLayout) {
      setIsLeftSidebarOpen(false);
    }
  }, [isCompactLayout]);

  const toggleLeftSidebar = () => {
    setIsLeftSidebarOpen((open) => !open);
  };

  // generic 频道走统一的 ChatManager 发送入口：视图不再手写 user.input 帧，
  // 字段组装收口到 GenericChatProvider（message-runtime-v0.1 #5）。
  const chatManager = useMemo(() => {
    if (!genericHandle) return null;
    return new ChatManager({
      resolveHandle: (_kind, _tid) =>
        makeNetworkHandle(
          genericHandle.connId,
          (frame) => {
            if (genericChannelState !== "open") return;
            genericHandle.send(frame);
          },
          () => genericHandle.close(),
        ),
      ensureThread: async (req) => req.provider.threadId,
      timelineFor: (tid) => getTimelineStore(tid),
    });
  }, [genericHandle, genericChannelState]);

  useEffect(() => {
    if (!genericWsId) {
      setGenericHandle(null);
      setGenericChannelState("closed");
      setThreadWsActive(false);
      setThreadWsState("closed");
      setThreadWsLatency(null);
      return;
    }
    if (!isResolvedHeartbeatConfig(heartbeatConfig)) {
      setGenericHandle(null);
      setGenericChannelState("closed");
      setThreadWsActive(true);
      setThreadWsState("closed");
      setThreadWsLatency(null);
      return () => {
        setThreadWsActive(false);
        setThreadWsState("closed");
        setThreadWsLatency(null);
      };
    }

    let handle: ChannelHandle;
    try {
      networkManager.configure({
        intervalMs: heartbeatConfig.intervalMs,
        backgroundIntervalMs: heartbeatConfig.backgroundIntervalMs,
        timeoutMs: heartbeatConfig.timeoutMs,
        maxMissed: heartbeatConfig.maxMissed,
      });
      handle = networkManager.openChannel("generic", genericWsId);
    } catch (err) {
      console.error("[ChatPage] generic channel open failed", err);
      setGenericHandle(null);
      setGenericChannelState("failed");
      setThreadWsActive(false);
      setThreadWsState("failed");
      setThreadWsLatency(null);
      return;
    }

    setThreadWsActive(true);
    setGenericHandle(handle);
    const offState = handle.onState((next) => {
      setGenericChannelState(next);
    });

    return () => {
      offState();
      handle.close();
      setThreadWsActive(false);
      setGenericChannelState("closed");
      setThreadWsState("closed");
      setThreadWsLatency(null);
    };
  }, [
    genericWsId,
    heartbeatConfig,
    setThreadWsActive,
    setThreadWsState,
    setThreadWsLatency,
  ]);

  // chat-receive-side-unify #5：generic 接收侧统一链路
  // socket.on(frame) → chatManager.ingestFrame → ChatTimelineStore →
  // useChatTimeline + adapter 投影 → MessageList。退役 useStreamingRender。
  //
  // 用 ref 持 chatManager，断开 useEffect 对 chatManager 的依赖，避免每次渲染
  // 重新 on/off 抖动（项目记忆：React useEffect 不稳定回调死循环）。effect 只在
  // (genericWsId, socket) 变化时重订阅。
  const chatManagerRef = useRef(chatManager);
  chatManagerRef.current = chatManager;

  useEffect(() => {
    if (!genericWsId || !genericHandle) return;
    const off = genericHandle.onMessage((frame) => {
      const manager = chatManagerRef.current;
      if (!manager) return;
      // 主链路：原始帧灌入统一状态机（user/assistant/tool/notice/error/usage record）。
      const envelope: RawFrameEnvelope = {
        connectionId: genericHandle.connId,
        channel: "generic",
        threadId: genericWsId,
        frame,
        receivedAt: Date.now(),
      };
      manager.ingestFrame(envelope);
      // 时间线之外的副作用（dialog 队列 / toast / 底栏 token），逐条保等价语义：
      const frameType =
        typeof frame === "object" && frame !== null && "frame_type" in frame
          ? frame.frame_type
          : undefined;
      switch (frameType) {
        case "approval.request":
          // 审批 dialog 弹窗队列不属于时间线（横幅 record 由 provider 翻成 status）。
          pushApproval(frame as Parameters<typeof pushApproval>[0]);
          break;
        case "error":
          // 横幅 record 由 provider 翻成 error record；这里补 toast（旧链路语义）。
          toast.error(String((frame as { message?: unknown }).message ?? ""));
          break;
        case "cell.evicted":
          // thread cell 回收：toast.warning + 清该 thread 的临时流式态。
          // resetThread 会清空整条时间线（含已 commit 消息），语义过狠；旧链路
          // clearBuffers 只清 streaming buffer。这里保持「只 toast，不清已落消息」
          // ——已提交消息保留，下次 thread.history 重连会带回最新真源。
          toast.warning(
            `cell 已回收（${String((frame as { reason?: unknown }).reason ?? "")}）：${
              String((frame as { message?: unknown }).message ?? "")
            }`,
          );
          break;
        case "usage":
          // 底栏 StatusLine 读 useChatStore.usageByThread，与气泡页脚（时间线 record）
          // 是两个展示位；这里保留 appendUsage 副作用，保证底栏 token 不回归。
          appendUsage(genericWsId, frame as Parameters<typeof appendUsage>[1]);
          break;
        case "run.interrupted":
          toast.info("已停止当前任务");
          break;
        default:
          break;
      }
    });
    return () => {
      off();
    };
  }, [genericWsId, genericHandle, pushApproval, appendUsage]);

  // chat-receive-side-unify #5：generic items 投影。
  // useChatTimeline 只订 generic threadId（claude/codex 时 genericWsId=undefined
  // → 回退稳定空 store，不影响）。组件侧 useMemo 投影，store getSnapshot 保持纯净。
  const timelineState = useChatTimeline(genericWsId);
  const timelineView = useMemo(() => toViewModel(timelineState), [timelineState]);
  const genericItems = useMemo(
    () => toGenericRenderItems(timelineView),
    [timelineView],
  );

  const onSend = (
    text: string,
    reasoningEffort: "low" | "medium" | "high" | null,
    attachments?: import("@/protocol").UserInputAttachment[],
  ) => {
    if (!threadId || !chatManager || genericChannelState !== "open") return;
    const currentFamily = modelFamilies.find(
      (family) => family.presetId === thread?.preset_id,
    );
    void chatManager.sendMessage({
      common: { text, reasoningEffort, attachments },
      provider: {
        provider: "generic",
        threadId,
        presetId: thread?.preset_id ?? null,
        modelFamilyId: currentFamily?.familyId ?? null,
      },
    });
  };

  const onSelectModelPreset = async (presetId: string) => {
    if (!threadId || presetId === thread?.preset_id) return;
    try {
      await updateThreadPreset(threadId, presetId);
      toast.success("模型已切换，下一次发送生效。");
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      toast.error(`模型切换失败：${message}`);
    }
  };

  // message-runtime #9：generic 打断改走 chatManager.interrupt 统一接口
  // （三频道一致），不再视图层直接 socket.send。
  const onInterrupt = () => {
    if (!threadId || !chatManager || !genericHandle) return;
    void chatManager.interrupt({ threadId, provider: "generic" });
  };

  const onCreateWhiteboardCard = async (
    scope: CardScope,
    kind: WhiteboardCardKind,
  ) => {
    const draft = buildWhiteboardCardDraft(kind, cards.length + 1);
    await createCard({
      scope,
      title: draft.title,
      category: draft.category,
      content: draft.content,
      x: 24 + cards.length * 18,
      y: 24 + cards.length * 18,
      height: draft.height,
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

  const showFilesPanel =
    Boolean(threadId) &&
    backendKind === "generic_chat" &&
    Boolean(activeWorkspaceTab === "files" || mountedWorkspaceTabs.files);
  const showGitPanel =
    Boolean(threadId) &&
    backendKind === "generic_chat" &&
    Boolean(activeWorkspaceTab === "git" || mountedWorkspaceTabs.git);
  const showShellPanel =
    Boolean(threadId) &&
    backendKind === "generic_chat" &&
    Boolean(activeWorkspaceTab === "shell" || mountedWorkspaceTabs.shell);
  const showWhiteboardPanel =
    Boolean(threadId) &&
    Boolean(activeWorkspaceTab === "whiteboard" || mountedWorkspaceTabs.whiteboard);
  const showChatPanel =
    backendKind === "generic_chat" &&
    (Boolean(threadId) || pendingNewSession == null);
  const showWorkspaceDock =
    Boolean(threadId) && !isMobileLayout && !isCompactLayout && !isGenericPendingBlank;
  const dockChatContent = threadId ? (
    <div className="space-y-3 text-sm" data-testid="dock-chat-context">
      <section className="rounded-lg border border-border/70 bg-background/45 p-3">
        <div className="mb-2 font-medium text-foreground">Thread Context</div>
        <div className="space-y-2 text-muted-foreground">
          <div className="truncate">
            <span className="text-foreground/80">Thread:</span>{" "}
            {thread?.name || threadId}
          </div>
          {effectiveCwd ? (
            <div className="truncate" title={effectiveCwd}>
              <span className="text-foreground/80">Workspace:</span> {effectiveCwd}
            </div>
          ) : null}
          <div className="truncate">
            <span className="text-foreground/80">Status:</span>{" "}
            {isRunning ? "running" : genericChannelState}
          </div>
        </div>
      </section>
      <section className="rounded-lg border border-border/70 bg-background/45 p-3">
        <div className="mb-2 font-medium text-foreground">Workflow</div>
        <WorkflowViewerEntryLink threadId={threadId} className="w-full justify-center" label="Workflow" />
      </section>
      <section className="rounded-lg border border-border/70 bg-background/45 p-3">
        <div className="mb-2 font-medium text-foreground">智能审批</div>
        <div
          className="text-xs text-muted-foreground"
          data-testid="dock-auto-approval-status"
        >
          {effectiveCwd
            ? autoApprovalSocket
              ? `已连接 · ${effectiveCwd}`
              : `等待连接 · ${effectiveCwd}`
            : "未绑定 workspace"}
        </div>
      </section>
    </div>
  ) : null;

  return (
    <div className="flex h-full min-w-0 gap-2 overflow-hidden px-2 pt-2">
      <LeftSidebar
        isOpen={isLeftSidebarOpen}
        compactMode={isCompactLayout}
        mobileMode={isMobileLayout}
        onToggleOpen={toggleLeftSidebar}
      />

      <div className="relative flex min-w-0 flex-1 flex-col gap-2 overflow-hidden">
        <div className="relative flex min-h-0 min-w-0 flex-1 gap-2 overflow-hidden">
          <div
            className={cn(
              "obsidian-panel obsidian-hairline flex min-w-0 flex-1 flex-col overflow-hidden rounded-xl",
              isCompactLayout && !isMobileLayout ? "px-[4.75rem]" : "px-0",
            )}
            data-testid="chat-main-panel"
          >
            {threadId && !useCompactToolbar ? (
              <div
                className="flex shrink-0 flex-wrap items-center justify-end gap-2 border-b border-border/60 px-3 py-2"
                data-testid="chat-thread-toolbar"
              >
                <ThreadTaskProgressPopover threadId={threadId} />
                <WorkflowViewerEntryLink threadId={threadId} />
              </div>
            ) : null}
            {threadId && useCompactToolbar ? (
              <div
                className="flex shrink-0 gap-2 border-b border-border/60 px-3 py-2"
                data-testid="chat-thread-toolbar"
              >
                <ThreadTaskProgressPopover
                  threadId={threadId}
                  className="flex-1 justify-center"
                  mobileMode={isMobileLayout}
                />
                <WorkflowViewerEntryLink threadId={threadId} className="flex-1 justify-center" />
              </div>
            ) : null}
            <div className="min-h-0 flex-1">
              {!threadId && pendingNewSession?.backendKind === "claude_code" ? (
                <ClaudeCodeView />
              ) : !threadId && pendingNewSession?.backendKind === "codex" ? (
                <CodexView />
              ) : isGenericPendingBlank ? (
                <GenericEmptyThreadView
                  onCreated={(createdThread) => {
                    navigate(`/chat/${createdThread.id}`);
                  }}
                />
              ) : isClaudeCode && threadId ? (
                <ClaudeCodeView threadId={threadId} thread={thread} />
              ) : isCodex && threadId ? (
                <CodexView threadId={threadId} thread={thread} />
              ) : (
                <div className="relative flex h-full min-h-0 flex-col overflow-hidden bg-background/10">
                  {showChatPanel ? (
                    <div
                      className={cn(
                        "flex min-h-0 flex-1 flex-col overflow-hidden",
                      )}
                    >
                      <div className="min-h-0 flex-1">
                        <MessageList
                          threadId={threadId}
                          items={genericItems}
                          timezone={clientConfig?.timezone}
                        />
                      </div>
                      <Composer
                        disabled={
                          !threadId ||
                          !genericHandle ||
                          genericChannelState !== "open" ||
                          isRunning
                        }
                        onSubmit={onSend}
                        threadId={threadId}
                        isRunning={isRunning}
                        onInterrupt={onInterrupt}
                        modelSwitcher={
                          <ModelSwitcher
                            currentPresetId={thread?.preset_id}
                            options={modelFamilies}
                            disabled={
                              !threadId ||
                              !genericHandle ||
                              genericChannelState !== "open" ||
                              isRunning
                            }
                            onSelect={onSelectModelPreset}
                          />
                        }
                        leftActions={
                          effectiveCwd && autoApprovalSocket ? (
                            <AutoApprovalToggle
                              cwd={effectiveCwd}
                              socket={autoApprovalSocket}
                            />
                          ) : null
                        }
                      />
                    </div>
                  ) : null}
                </div>
              )}
            </div>
          </div>

          {showWorkspaceDock && threadId ? (
            <WorkspaceDock
              activeTab={activeWorkspaceTab}
              onTabChange={(tab) => setActiveWorkspaceTab(threadId, tab)}
              thread={{
                id: thread?.claude_thread_id || thread?.codex_thread_id || threadId,
                title: thread?.name,
                status: isRunning ? "running" : genericChannelState,
              }}
              workspaceRoot={effectiveCwd || workspaceContext?.workspace_root || null}
              compact={isCompactLayout}
              mobile={isMobileLayout}
              chatContent={dockChatContent}
              filesContent={
                showFilesPanel ? (
                  <WorkspaceFilesPanel
                    context={workspaceContext}
                    loading={workspaceLoading}
                    openRequest={workspaceFileOpenRequest}
                  />
                ) : null
              }
              gitContent={
                showGitPanel ? (
                  <WorkspaceGitPanel
                    context={workspaceContext}
                    loading={workspaceLoading}
                    onOpenFile={(path) => {
                      requestOpenWorkspaceFile(threadId, path);
                      setActiveWorkspaceTab(threadId, "files");
                    }}
                  />
                ) : null
              }
              shellContent={
                showShellPanel ? (
                  <WorkspaceShellPanel
                    context={workspaceContext}
                    loading={workspaceLoading}
                  />
                ) : null
              }
              whiteboardContent={
                showWhiteboardPanel ? (
                  <WhiteboardPanel
                    title={projectTitle ?? globalTitle}
                    projectTitle={projectTitle}
                    cards={whiteboardCards}
                    isOpen={true}
                    embedded={true}
                    variant="dock"
                    compactMode={false}
                    mobileMode={false}
                    canCreate={true}
                    onCreateCard={onCreateWhiteboardCard}
                    onToggleCollapse={toggleCollapsed}
                    onDeleteCard={onDeleteWhiteboardCard}
                    onUpdateCard={onUpdateWhiteboardCard}
                    onUpdateCardLayout={onUpdateWhiteboardCardLayout}
                    onBringToFront={bringCardToFront}
                    emptyTitle="No whiteboard cards yet"
                    emptyDescription="Use the Dock whiteboard to organize cards for the current thread."
                  />
                ) : null
              }
            />
          ) : null}
        </div>
        <FileDrawer mobileMode={isMobileLayout} />
        {!threadId || isClaudeCode || isCodex ? null : (
          <ApprovalDialog socket={approvalSocket} />
        )}
      </div>
    </div>
  );
}

function WorkflowViewerEntryLink({
  threadId,
  className,
  label = "Workflow Viewer",
}: {
  threadId: string;
  className?: string;
  label?: string;
}) {
  return (
    <Link
      to={`/chat/${threadId}/agent-workflows`}
      className={cn(
        "inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md border border-primary/25 bg-primary/10 px-2.5 py-1.5 text-xs font-medium text-foreground shadow-sm transition-colors hover:bg-primary/15",
        className,
      )}
    >
      <Workflow className="h-3.5 w-3.5 text-primary" />
      {label}
    </Link>
  );
}
