import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, Outlet, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { History, ListChecks, Workflow } from "lucide-react";
import { ClaudeCodeView } from "@/components/ClaudeCodeView";
import { CodexView } from "@/components/CodexView";
import { Composer, type ReasoningEffort } from "@/components/Composer";
import { FileDrawer } from "@/components/FileDrawer";
import { GenericEmptyThreadView } from "@/components/GenericEmptyThreadView";
import { LeftSidebar } from "@/components/LeftSidebar";
import { MessageList } from "@/components/MessageList";
import { ModelSwitcher } from "@/components/ModelSwitcher";
import { ThreadTaskProgressPopover } from "@/components/ThreadTaskProgressPopover";
import { Button } from "@/components/ui/button";
import { WhiteboardPanel, type WhiteboardCardItem } from "@/components/WhiteboardPanel";
import { WorkspaceDock, type WorkspaceDockTab } from "@/components/WorkspaceDock";
import { WorkspaceFilesPanel } from "@/components/WorkspaceFilesPanel";
import { WorkspaceGitPanel } from "@/components/WorkspaceGitPanel";
import { WorkspaceShellPanel } from "@/components/WorkspaceShellPanel";
import {
  useRegisterWebShellRailItems,
  type WebShellRailItem,
} from "@/components/web-shell-rail";
import { ThreadPermissionsManager } from "@/features/thread-permissions";
import {
  AutoApprovalModeSelector,
  type AutoApprovalSocket,
  useAutoApprovalStore,
} from "@/features/auto-approval";
import { useChatLayout } from "@/hooks/useChatLayout";
import {
  useClientConfig,
  type ClientRuntimeConfig,
} from "@/hooks/useClientConfig";
import { buildWhiteboardCardDraft, type WhiteboardCardKind } from "@/lib/whiteboard-card-templates";
import { cn } from "@/lib/utils";
import { ChatManager } from "@/chat/ChatManager";
import { makeNetworkHandle, getTimelineStore, useChatTimeline, makeCronTimelineKey } from "@/chat/runtimeWiring";
import { toViewModel, toGenericRenderItems } from "@/chat/ChatRenderAdapter";
import type { RawFrameEnvelope } from "@/chat/types";
import { ChoiceManager, type ChoiceState } from "@/modules/choice/ChoiceManager";
import { ChoicePanel } from "@/modules/choice/ChoicePanel";
import {
  PendingInputQueueManager,
  type PendingInputQueueState,
} from "@/modules/pending-input/PendingInputQueueManager";
import { PendingInputQueuePanel } from "@/modules/pending-input/PendingInputQueuePanel";
import { networkManager } from "@/network";
import type { ChannelHandle, ChannelKind, SocketState } from "@/network";
import type {
  CardScope,
  ChoiceRequestFrame,
  ChoiceSubmitFrame,
  ConversationReferenceDTO,
  AutoApprovalStateFrame,
  ErrorFrame,
  PendingInputChangedFrame,
  PendingInputSnapshotFrame,
  PendingInputStartedFrame,
  UserInputAttachment,
  WSFrameC2S,
} from "@/protocol";
import { useChatStore } from "@/stores/chat";
import { useConnectionStatusStore } from "@/stores/connectionStatus";
import { useThreadsStore } from "@/stores/threads";
import { useModelProvidersStore } from "@/modules/model-providers/store";
import { ThreadCronRunsPopover } from "@/modules/scheduler/components/ThreadCronRunsPopover";
import { listTaskRuns, loadRunMessages } from "@/modules/scheduler/api";
import { useSchedulerStore } from "@/modules/scheduler/store";
import { useThreadRunning } from "@/hooks/useThreadRunning";
import { useThreadDispatchStore } from "@/stores/threadDispatch";
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
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const threadId = params.thread_id;
  const cronTaskId = searchParams.get("taskId") || null;
  const cronRunId = searchParams.get("runId") || null;
  const cronTimelineId =
    threadId && cronTaskId && cronRunId
      ? makeCronTimelineKey(threadId, cronRunId)
      : undefined;
  const isCronRunContext = Boolean(threadId && cronTaskId && cronRunId);
  const thread = useThreadsStore((s) =>
    threadId ? s.threads.find((item) => item.id === threadId) : undefined,
  );
  const pendingNewSession = useThreadsStore((s) => s.pendingNewSession);
  const setPendingNewSession = useThreadsStore((s) => s.setPendingNewSession);
  const setThreadReasoningSelection = useThreadsStore(
    (s) => s.setThreadReasoningSelection,
  );
  const savedReasoningSelection = useThreadsStore((s) =>
    threadId ? s.reasoningSelectionByThread[threadId] : undefined,
  );
  const updateThreadPreset = useThreadsStore((s) => s.updateThreadPreset);
  const forkThread = useThreadsStore((s) => s.forkThread);
  const modelFamilies = useModelProvidersStore((s) => s.modelFamilies);
  const loadModelFamilies = useModelProvidersStore((s) => s.loadModelFamilies);
  const activeModelFamily = useMemo(
    () => modelFamilies.find((family) => family.presetId === thread?.preset_id),
    [modelFamilies, thread?.preset_id],
  );
  const backendKind = thread?.backend_kind ?? "generic_chat";
  const isClaudeCode = backendKind === "claude_code";
  const isCodex = backendKind === "codex";
  const scheduledTaskId =
    thread &&
    (thread.thread_kind === "scheduled_task" ||
      thread.source_kind === "scheduled_task")
      ? thread.source_id ?? ""
      : "";
  const isGenericPendingBlank =
    !threadId && pendingNewSession?.backendKind === "generic_chat";

  const genericWsId = thread && !isClaudeCode && !isCodex ? threadId : undefined;
  const genericConnectionKind: ChannelKind = isCronRunContext ? "cron-run" : "generic";
  const genericConnectionId =
    isCronRunContext && cronTaskId && cronRunId
      ? `${cronTaskId}:${cronRunId}`
      : genericWsId;
  const genericTimelineTargetId = cronTimelineId ?? genericWsId;
  const clientConfig = useClientConfig();
  const heartbeatConfig = clientConfig?.heartbeat;
  const [genericHandle, setGenericHandle] = useState<ChannelHandle | null>(null);
  const [genericChannelState, setGenericChannelState] =
    useState<SocketState>("closed");
  const [choiceState, setChoiceState] = useState<ChoiceState | null>(null);
  const [choiceSubmitting, setChoiceSubmitting] = useState(false);
  const [forkingHistoryIndex, setForkingHistoryIndex] = useState<number | null>(
    null,
  );
  const [pendingInputState, setPendingInputState] = useState<PendingInputQueueState>(() =>
    PendingInputQueueManager.empty(threadId ?? null),
  );
  const [restoreDraftToken, setRestoreDraftToken] = useState<number | null>(null);
  const choiceSubmittingRef = useRef(choiceSubmitting);
  choiceSubmittingRef.current = choiceSubmitting;
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

  const [isLeftSidebarOpen, setIsLeftSidebarOpen] = useState(!isCompactLayout);
  const [mountedWorkspaceTabs, setMountedWorkspaceTabs] = useState<
    Partial<Record<WorkspaceDockTab, boolean>>
  >({ chat: true });

  // chat-receive-side-unify #5：appendUser 退役。generic_chat 的用户气泡由
  // pending-input.started 携带后端确认后的 PendingInputDTO 进入时间线；底栏 token
  // 仍由 appendUsage 喂养（useChatStore.usageByThread 是 StatusLine 数据源）。
  const fetchThreadUsage = useChatStore((s) => s.fetchThreadUsage);
  const appendUsage = useChatStore((s) => s.appendUsage);
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
  const selectSchedulerRun = useSchedulerStore((s) => s.selectRun);
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
  const isDispatching = useThreadDispatchStore(
    (state) =>
      threadId
        ? state.byThreadId[threadId]?.phase === "dispatching"
        : false,
  );

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
    if (!cronTaskId || !cronRunId) return;
    selectSchedulerRun(cronTaskId, cronRunId);
  }, [cronTaskId, cronRunId, selectSchedulerRun]);

  useEffect(() => {
    if (!threadId || !scheduledTaskId || cronTaskId || cronRunId) return;
    let cancelled = false;
    void listTaskRuns(scheduledTaskId, 1)
      .then((runs) => {
        if (cancelled) return;
        const latest = runs[runs.length - 1];
        if (!latest?.runId) return;
        navigate(
          `/chat/${threadId}?${new URLSearchParams({
            taskId: latest.taskId || scheduledTaskId,
            runId: latest.runId,
          }).toString()}`,
          { replace: true },
        );
      })
      .catch((err) => {
        if (cancelled) return;
        toast.error(`加载定时任务运行记录失败：${String(err)}`);
      });
    return () => {
      cancelled = true;
    };
  }, [
    cronRunId,
    cronTaskId,
    navigate,
    scheduledTaskId,
    threadId,
  ]);

  useEffect(() => {
    if (!cronTaskId || !cronRunId || !cronTimelineId) return;
    let cancelled = false;
    void loadRunMessages(cronTaskId, cronRunId)
      .then((batch) => {
        if (cancelled) return;
        const timelineStore = getTimelineStore(cronTimelineId);
        timelineStore.resetThread(cronTimelineId);
        timelineStore.applyHistory({
          threadId: cronTimelineId,
          provider: "generic",
          events: [
            {
              kind: "history_batch_loaded",
              provider: "generic",
              threadId: cronTimelineId,
              turnId: `${cronTimelineId}:history`,
              createdAt: Date.now(),
              payload: { messages: batch.messages },
            },
          ],
          hasMore: false,
        });
      })
      .catch((err) => {
        if (cancelled) return;
        toast.error(`加载定时任务执行历史失败：${String(err)}`);
      });
    return () => {
      cancelled = true;
    };
  }, [cronTaskId, cronRunId, cronTimelineId]);

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
    setPendingInputState(PendingInputQueueManager.empty(threadId ?? null));
    setRestoreDraftToken(null);
  }, [threadId]);

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
    if (!genericConnectionId) {
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
      handle = networkManager.openChannel(genericConnectionKind, genericConnectionId);
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
    genericConnectionId,
    genericConnectionKind,
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
    if (!genericTimelineTargetId || !genericHandle) return;
    const off = genericHandle.onMessage((frame) => {
      if (
        typeof frame === "object" &&
        frame !== null &&
        "frame_type" in frame &&
        frame.frame_type === "auto_approval_state"
      ) {
        useAutoApprovalStore
          .getState()
          .applyStateFrame(frame as AutoApprovalStateFrame);
        return;
      }
      const manager = chatManagerRef.current;
      if (!manager) return;
      const inboundFrameType =
        typeof frame === "object" && frame !== null && "frame_type" in frame
          ? frame.frame_type
          : undefined;
      let timelineThreadId = genericTimelineTargetId;
      if (inboundFrameType === "cron.message.appended") {
        const cronFrame = frame as {
          thread_id?: unknown;
          task_id?: unknown;
          run_id?: unknown;
        };
        const parentThreadId =
          typeof cronFrame.thread_id === "string" ? cronFrame.thread_id : genericTimelineTargetId;
        const taskId = typeof cronFrame.task_id === "string" ? cronFrame.task_id : "";
        const runId = typeof cronFrame.run_id === "string" ? cronFrame.run_id : "";
        if (parentThreadId !== genericTimelineTargetId || !taskId || !runId) return;
        timelineThreadId = makeCronTimelineKey(parentThreadId, runId);
        if (cronRunId !== runId || cronTaskId !== taskId) {
          navigate(
            `/chat/${parentThreadId}?${new URLSearchParams({
              taskId,
              runId,
            }).toString()}`,
            { replace: true },
          );
        }
      }
      // 主链路：原始帧灌入统一状态机（user/assistant/tool/notice/error/usage record）。
      const envelope: RawFrameEnvelope = {
        connectionId: genericHandle.connId,
        channel: "generic",
        threadId: timelineThreadId,
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
        case "pending-input.snapshot":
          setPendingInputState((prev) =>
            PendingInputQueueManager.applySnapshot(
              prev,
              frame as PendingInputSnapshotFrame,
            ),
          );
          break;
        case "pending-input.changed":
          setPendingInputState((prev) =>
            PendingInputQueueManager.applyChanged(prev, frame as PendingInputChangedFrame),
          );
          break;
        case "pending-input.started":
          setPendingInputState((prev) =>
            PendingInputQueueManager.applyStarted(prev, frame as PendingInputStartedFrame),
          );
          break;
        case "choice.request":
          setChoiceState(ChoiceManager.receive(frame as ChoiceRequestFrame));
          setChoiceSubmitting(false);
          break;
        case "turn.start":
          if (choiceSubmittingRef.current) {
            setChoiceState(null);
            setChoiceSubmitting(false);
          }
          break;
        case "error":
          // 横幅 record 由 provider 翻成 error record；这里补 toast（旧链路语义）。
          {
            const errorFrame = frame as ErrorFrame;
            const message = String(errorFrame.message ?? "");
            toast.error(message);
            if (errorFrame.reason === "pending_input_queue_full") {
              setPendingInputState((prev) =>
                PendingInputQueueManager.withError(prev, message),
              );
              setRestoreDraftToken((token) => (token ?? 0) + 1);
            }
          }
          if (choiceSubmittingRef.current) {
            setChoiceSubmitting(false);
          }
          break;
        case "cell.evicted":
          // thread cell 回收只提示，不清时间线。TimelineStore 的私有 pending 由自身
          // 生命周期释放；已提交消息保留，重连后的 thread.history 会回放最新真源。
          toast.warning(
            `cell 已回收（${String((frame as { reason?: unknown }).reason ?? "")}）：${
              String((frame as { message?: unknown }).message ?? "")
            }`,
          );
          break;
        case "usage":
          // 底栏 StatusLine 读 useChatStore.usageByThread，与气泡页脚（时间线 record）
          // 是两个展示位；这里保留 appendUsage 副作用，保证底栏 token 不回归。
          appendUsage(genericTimelineTargetId, frame as Parameters<typeof appendUsage>[1]);
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
  }, [
    genericTimelineTargetId,
    genericHandle,
    appendUsage,
    cronTaskId,
    cronRunId,
    navigate,
  ]);

  // chat-receive-side-unify #5：generic items 投影。
  // useChatTimeline 只订 generic threadId（claude/codex 时 genericWsId=undefined
  // → 回退稳定空 store，不影响）。组件侧 useMemo 投影，store getSnapshot 保持纯净。
  const activeTimelineId = genericTimelineTargetId;
  const timelineState = useChatTimeline(activeTimelineId);
  const timelineView = useMemo(() => toViewModel(timelineState), [timelineState]);
  const genericItems = useMemo(
    () => toGenericRenderItems(timelineView),
    [timelineView],
  );
  const canForkAssistantReply =
    Boolean(threadId) &&
    !isCronRunContext &&
    backendKind === "generic_chat" &&
    (thread?.thread_kind ?? "chat") === "chat" &&
    !(thread?.claude_thread_id || thread?.codex_thread_id);
  const onForkAssistant = useCallback(
    async (historyIndex: number) => {
      if (!threadId || forkingHistoryIndex !== null) return;
      setForkingHistoryIndex(historyIndex);
      try {
        const forked = await forkThread(threadId, historyIndex);
        toast.success("已从该回复创建分支");
        navigate(`/chat/${forked.id}`);
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        toast.error(`分叉失败：${message}`);
      } finally {
        setForkingHistoryIndex(null);
      }
    },
    [forkThread, forkingHistoryIndex, navigate, threadId],
  );

  const onSend = async (
    text: string,
    reasoningEffort: ReasoningEffort | null,
    attachments?: UserInputAttachment[],
    references?: ConversationReferenceDTO[],
  ) => {
    if (
      !activeTimelineId ||
      !chatManager ||
      genericChannelState !== "open"
    ) {
      return false;
    }
    if (pendingInputState.items.length >= pendingInputState.maxItems) {
      const message = `待发送队列已满（最多 ${pendingInputState.maxItems} 条）。`;
      setPendingInputState((prev) =>
        PendingInputQueueManager.withError(prev, message),
      );
      toast.error(message);
      return false;
    }
    try {
      await chatManager.sendMessage({
        common: { text, reasoningEffort, attachments, references },
        provider: {
          provider: "generic",
          threadId: activeTimelineId,
          presetId: thread?.preset_id ?? null,
          modelFamilyId: activeModelFamily?.familyId ?? null,
        },
      });
      return true;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      toast.error(`发送失败：${message}`);
      return false;
    }
  };

  const onChoiceSubmit = async (frame: ChoiceSubmitFrame) => {
    if (!threadId || !chatManager || genericChannelState !== "open") {
      toast.error("选择提交失败：连接尚未就绪。");
      return;
    }
    try {
      await chatManager.submitChoice({
        provider: "generic",
        threadId,
        frame,
      });
      setChoiceSubmitting(true);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      toast.error(`选择提交失败：${message}`);
      setChoiceSubmitting(false);
    }
  };

  const sendPendingInputFrame = (frame: WSFrameC2S) => {
    if (!genericHandle || genericChannelState !== "open") {
      toast.error("待发送队列操作失败：连接尚未就绪。");
      return;
    }
    genericHandle.send(frame);
  };

  const onPendingInputUpdate = (id: string, content: string) => {
    sendPendingInputFrame(PendingInputQueueManager.buildUpdateFrame(id, content));
  };

  const onPendingInputCancel = (id: string) => {
    sendPendingInputFrame(PendingInputQueueManager.buildCancelFrame(id));
  };

  const onPendingInputSendNow = (id: string) => {
    sendPendingInputFrame(PendingInputQueueManager.buildSendNowFrame(id));
  };

  const onPendingInputReorder = (orderedIds: string[]) => {
    sendPendingInputFrame(PendingInputQueueManager.buildReorderFrame(orderedIds));
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
  const railThreadItems = useMemo<WebShellRailItem[]>(() => {
    if (!threadId) return [];
    const items: WebShellRailItem[] = [
      {
        id: "thread-task-progress",
        scope: "thread",
        priority: "p0",
        label: "任务进度",
        icon: ListChecks,
        available: true,
        render: ({ className, iconClassName, label }) => (
          <ThreadTaskProgressPopover
            threadId={threadId}
            panelClassName="left-[calc(100%+0.75rem)] right-auto top-0"
            trigger={({ open, disabled, onClick }) => (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className={className}
                aria-label={label}
                aria-expanded={open}
                disabled={disabled}
                data-testid="web-shell-rail-item-thread-task-progress"
                onClick={onClick}
              >
                <ListChecks className={iconClassName} />
              </Button>
            )}
          />
        ),
      },
    ];
    if (scheduledTaskId) {
      items.unshift({
        id: "thread-cron-runs",
        scope: "thread",
        priority: "p0",
        label: "运行记录",
        icon: History,
        available: true,
        render: ({ className, iconClassName, label }) => (
          <ThreadCronRunsPopover
            threadId={threadId}
            taskId={scheduledTaskId}
            activeRunId={cronRunId}
            timezone={clientConfig?.timezone}
            panelClassName="left-[calc(100%+0.75rem)] right-auto top-0"
            trigger={({ open, disabled, onClick }) => (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className={className}
                aria-label={label}
                aria-expanded={open}
                disabled={disabled}
                data-testid="web-shell-rail-item-thread-cron-runs"
                onClick={onClick}
              >
                <History className={iconClassName} />
              </Button>
            )}
          />
        ),
      });
    }
    return items;
  }, [clientConfig?.timezone, cronRunId, scheduledTaskId, threadId]);
  useRegisterWebShellRailItems("chat-thread-tools", railThreadItems);
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
        <ThreadPermissionsManager threadId={threadId} />
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
                {scheduledTaskId ? (
                  <ThreadCronRunsPopover
                    threadId={threadId}
                    taskId={scheduledTaskId}
                    activeRunId={cronRunId}
                    timezone={clientConfig?.timezone}
                  />
                ) : null}
                <ThreadTaskProgressPopover threadId={threadId} />
                <WorkflowViewerEntryLink threadId={threadId} />
              </div>
            ) : null}
            {threadId && useCompactToolbar ? (
              <div
                className="flex shrink-0 gap-2 border-b border-border/60 px-3 py-2"
                data-testid="chat-thread-toolbar"
              >
                {scheduledTaskId ? (
                  <ThreadCronRunsPopover
                    threadId={threadId}
                    taskId={scheduledTaskId}
                    activeRunId={cronRunId}
                    timezone={clientConfig?.timezone}
                    className="flex-1 justify-center"
                    mobileMode={isMobileLayout}
                  />
                ) : null}
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
                  onCreated={(createdThread, reasoningEffort) => {
                    setThreadReasoningSelection(
                      createdThread.id,
                      createdThread.preset_id,
                      reasoningEffort,
                    );
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
                      <div className="flex min-h-0 flex-1 flex-col">
                        <div
                          className="min-h-0 flex-1"
                          data-testid="fork-lineage-message-viewport"
                        >
                          <MessageList
                            threadId={activeTimelineId}
                            items={genericItems}
                            timezone={clientConfig?.timezone}
                            onForkAssistant={
                              canForkAssistantReply
                                ? onForkAssistant
                                : undefined
                            }
                            forkingHistoryIndex={forkingHistoryIndex}
                            forkLineage={
                              thread?.forked_from_id &&
                              typeof thread.forked_from_history_index === "number"
                                ? {
                                    parentThreadId: thread.forked_from_id,
                                    historyIndex: thread.forked_from_history_index,
                                  }
                                : null
                            }
                          />
                        </div>
                      </div>
                      {choiceState ? (
                        <ChoicePanel
                          state={choiceState}
                          disabled={
                            choiceSubmitting ||
                            !threadId ||
                            !genericHandle ||
                            genericChannelState !== "open"
                          }
                          onChange={setChoiceState}
                          onSubmit={onChoiceSubmit}
                        />
                      ) : null}
                      <PendingInputQueuePanel
                        items={pendingInputState.items}
                        maxItems={pendingInputState.maxItems}
                        error={pendingInputState.lastError}
                        disabled={
                          !threadId ||
                          !genericHandle ||
                          genericChannelState !== "open" ||
                          isDispatching
                        }
                        onUpdate={onPendingInputUpdate}
                        onCancel={onPendingInputCancel}
                        onSendNow={onPendingInputSendNow}
                        onReorder={onPendingInputReorder}
                      />
                      <Composer
                        disabled={
                          !threadId ||
                          !genericHandle ||
                          genericChannelState !== "open" ||
                          isDispatching
                        }
                        onSubmit={onSend}
                        threadId={threadId}
                        isRunning={isRunning}
                        allowSubmitWhileRunning
                        restoreDraftToken={restoreDraftToken}
                        onInterrupt={onInterrupt}
                        reasoningOptions={activeModelFamily?.supportedReasoningEfforts}
                        defaultReasoningEffort={activeModelFamily?.defaultReasoningEffort}
                        reasoningSelectionKey={
                          activeModelFamily?.presetId ?? thread?.preset_id ?? null
                        }
                        initialReasoningEffort={
                          savedReasoningSelection &&
                          savedReasoningSelection.presetId === thread?.preset_id
                            ? savedReasoningSelection.effort
                            : undefined
                        }
                        onReasoningEffortChange={(effort) => {
                          if (!threadId || !thread?.preset_id) return;
                          setThreadReasoningSelection(
                            threadId,
                            thread.preset_id,
                            effort,
                          );
                        }}
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
                            <AutoApprovalModeSelector
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
        <Outlet />
      </div>
    </div>
  );
}

function WorkflowViewerEntryLink({
  threadId,
  className,
  label = "任务详情",
}: {
  threadId: string;
  className?: string;
  label?: string;
}) {
  return (
    <Link
      to={`/chat/${threadId}/task-detail`}
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
