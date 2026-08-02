import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { ChatMessageItem, MessageViewport } from "@/components/MessageList";
import { Composer, type SubmittedDraft } from "@/components/Composer";
import { useClaudeCodeWS } from "@/hooks/useClaudeCodeWS";
import { ChatManager } from "@/chat/ChatManager";
import { makeNetworkHandle, getTimelineStore } from "@/chat/runtimeWiring";
import { useThreadRunning } from "@/hooks/useThreadRunning";
import { apiGet } from "@/lib/api";
import type {
  NormalizedMessage,
  SystemNoticeFrame,
  ThreadMetadataDTO,
  ClaudeCodeC2SFrame,
  AutoApprovalStateFrame,
} from "@/protocol";
import type { ChatItem as GenericChatItem } from "@/stores/chat";
import { useEvolutionStore } from "@/stores/evolution";
import {
  AutoApprovalModeSelector,
  useAutoApprovalStore,
} from "@/features/auto-approval";
import { useItemsWithFileSummary } from "@/hooks/useItemsWithFileSummary";
import { ModifiedFilesSummary } from "@/components/ModifiedFilesSummary";
import {
  useThreadsStore,
  type InitialMessageDraft,
} from "@/stores/threads";
import { useThreadDispatchStore } from "@/stores/threadDispatch";

type ClaudeMetaItem =
  | { kind: "status"; content?: unknown; id: string }
  | {
      kind: "complete";
      aborted?: boolean;
      id: string;
    };

type ClaudeRenderItem = GenericChatItem | ClaudeMetaItem;

let _gid = 0;
const newId = () => `cc-${Date.now()}-${++_gid}`;

const stringifyContent = (v: unknown): string => {
  if (typeof v === "string") return v;
  if (v == null) return "";
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
};

function toTimestampMs(timestamp?: string): number {
  if (!timestamp) return Date.now();
  const ms = Date.parse(timestamp);
  return Number.isFinite(ms) ? ms : Date.now();
}

function asRecord(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  if (value === undefined) return {};
  return { input: value };
}

function splitToolResult(
  value: unknown,
): Pick<Extract<GenericChatItem, { kind: "tool" }>, "result" | "resultData"> {
  if (value == null) return {};
  if (typeof value === "string") return { result: value };
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return { resultData: value as Record<string, unknown> };
  }
  return { result: stringifyContent(value) };
}

function isGenericChatItem(item: ClaudeRenderItem): item is GenericChatItem {
  return (
    item.kind === "user" ||
    item.kind === "assistant" ||
    item.kind === "tool" ||
    item.kind === "error"
  );
}

function findLastToolIndex(items: ClaudeRenderItem[], toolId?: string): number {
  if (!toolId) return -1;
  for (let i = items.length - 1; i >= 0; i -= 1) {
    const item = items[i];
    if (
      item &&
      isGenericChatItem(item) &&
      item.kind === "tool" &&
      item.callId === toolId
    ) {
      return i;
    }
  }
  return -1;
}

function convertHistoryToChatItems(
  threadId: string,
  messages: NormalizedMessage[],
): ClaudeRenderItem[] {
  const items: ClaudeRenderItem[] = [];
  let n = 0;
  const hid = () => `cc-history-${++n}`;
  const nextTurn = () => n;

  for (const msg of messages) {
    switch (msg.frame_type) {
      case "text": {
        const text = stringifyContent(msg.content);
        if (!text.trim()) break;
        if ((msg.role ?? "assistant") === "user") {
          items.push({
            id: hid(),
            kind: "user",
            threadId,
            content: text,
            timestampMs: toTimestampMs(msg.timestamp),
          });
        } else {
          items.push({
            id: hid(),
            kind: "assistant",
            threadId,
            turn: nextTurn(),
            runId: "claude-history",
            content: text,
            reasoning: "",
            timestampMs: toTimestampMs(msg.timestamp),
            streaming: false,
          });
        }
        break;
      }
      case "thinking": {
        items.push({
          id: hid(),
          kind: "assistant",
          threadId,
          turn: nextTurn(),
          runId: "claude-history",
          content: "",
          reasoning: stringifyContent(msg.content),
          timestampMs: toTimestampMs(msg.timestamp),
          streaming: false,
        });
        break;
      }
      case "tool_use": {
        items.push({
          id: hid(),
          kind: "tool",
          threadId,
          turn: nextTurn(),
          runId: "claude-history",
          toolName: msg.toolName ?? "(未知工具)",
          callId: msg.toolId ?? hid(),
          arguments: asRecord(msg.toolInput ?? msg.input),
          ok: null,
          timestampMs: toTimestampMs(msg.timestamp),
        });
        break;
      }
      case "tool_result": {
        const idx = findLastToolIndex(items, msg.toolId);
        const patch = splitToolResult(msg.content);
        if (idx >= 0) {
          const current = items[idx];
          if (isGenericChatItem(current) && current.kind === "tool") {
            items[idx] = {
              ...current,
              ok: !(msg.isError ?? false),
              ...patch,
            };
          }
        } else {
          items.push({
            id: hid(),
            kind: "tool",
            threadId,
            turn: nextTurn(),
            runId: "claude-history",
            toolName: msg.toolId ?? "tool_result",
            callId: msg.toolId ?? hid(),
            arguments: {},
            ok: !(msg.isError ?? false),
            timestampMs: toTimestampMs(msg.timestamp),
            ...patch,
          });
        }
        break;
      }
      default:
        break;
    }
  }

  return items;
}

interface Props {
  threadId?: string;
  thread?: ThreadMetadataDTO;
}

export function ClaudeCodeView({ threadId, thread }: Props) {
  const navigate = useNavigate();
  const [resumeSessionId, setResumeSessionId] = useState<string | null>(
    thread?.claude_thread_id?.trim() || null,
  );
  const { socket, state } = useClaudeCodeWS(threadId);
  const [items, setItems] = useState<ClaudeRenderItem[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const streamIdRef = useRef<string | null>(null);
  const hadStreamThisTurnRef = useRef(false);
  const turnSeqRef = useRef(0);
  const pendingProjectsRefreshRef = useRef(false);
  const [streamPhase, setStreamPhase] = useState<"idle" | "responding" | "thinking" | "tool_calling">("idle");
  const [streamToolName, setStreamToolName] = useState<string | undefined>();

  // fix-claude-stop-button-stuck：判断"对话是否已结束"的唯一语义真源。
  // 规则：从 items 末尾倒查，先遇 complete → 已结束（true）；
  // 先遇 user → 新一轮已开始（false）；都没遇到 → 从未结束（false）。
  // 服务于：
  //   1) Composer Stop→Send 切换（isRunning 出态严判依据）
  //   2) "对话结束" 气泡的判断同义化（避免不同 UI 用不同函数）
  // complete 帧是后端唯一可信终止信号（参见 stream_end vs complete 不对称问题）。
  const isConversationEnded = useMemo(() => {
    for (let i = items.length - 1; i >= 0; i--) {
      const it = items[i];
      if (!isGenericChatItem(it) && it.kind === "complete") return true;
      if (isGenericChatItem(it) && it.kind === "user") return false;
    }
    return false;
  }, [items]);

  // chat-running-state-unify #3：入态信号切到三频道共享 useThreadRunning
  // （后端 thread-status phase 单一真源），不再依赖前端推导的 streamPhase / items.streaming。
  // 保留 fix-claude-stop-button-stuck 的「出态严」兜底——isConversationEnded（complete
  // 帧到达即结束）无条件压 false，避免后端 phase 万一漏发 complete 导致按钮卡住。
  // streamPhase 状态本身保留（被 stream_status / stream_end 等 listener 维护，
  // 服务于 UI phase 文案 + 工具名展示），仅 isRunning 推导改用 hook。
  const threadRunning = useThreadRunning(threadId);
  const isRunning = useMemo(
    () => threadRunning && !isConversationEnded,
    [threadRunning, isConversationEnded],
  );
  const isDispatching = useThreadDispatchStore(
    (store) =>
      threadId
        ? store.byThreadId[threadId]?.phase === "dispatching"
        : false,
  );

  // interrupt-claude-channel-v0.1 #4：abort-session 帧需要的 sessionId。
  // 优先用 thread.claude_thread_id（SDK session_created 后绑定的真值）；
  // 首次发送前为空，退到 threadId 做兜底（即使后端拿到的是占位，至少能让前端
  // gate 早 return 不发空帧）。
  const sessionIdForAbort = useMemo(() => {
    const liveSid = resumeSessionId?.trim();
    if (liveSid) return liveSid;
    const realSid = thread?.claude_thread_id?.trim();
    if (realSid) return realSid;
    return threadId ?? "";
  }, [resumeSessionId, thread?.claude_thread_id, threadId]);

  // message-runtime-v0.1 #5：claude 发送走统一 ChatManager。保留下方 pending 创建
  // 时序（createThread + navigate + initialMessage buffer），仅把 claude-command 帧
  // 的组装收口到 ClaudeChatProvider，视图不再手写 wire 帧。
  // 提前到 onInterrupt 之前——#9 把 onInterrupt 改成走 chatManager.interrupt。
  const chatManager = useMemo(() => {
    if (!socket) return null;
    return new ChatManager({
      resolveHandle: (_kind, tid) =>
        makeNetworkHandle(tid, (frame) =>
          socket.send(frame as ClaudeCodeC2SFrame),
        ),
      ensureThread: async (req) => req.provider.threadId,
      timelineFor: (tid) => getTimelineStore(tid),
    });
  }, [socket]);

  // interrupt-claude-channel-v0.1 #5：onInterrupt 含两道幂等防御。
  // 1) isRunning=false 早 return（gate）—— 没在 running 不发帧；
  // 2) 300ms 节流 —— 误连点 / 双击只发一次 abort-session。
  // 节流时间记 ref，避免 deps 变化导致 useCallback 重建。
  // message-runtime #9：发帧改走 chatManager.interrupt 统一接口
  // （三频道一致），不再视图层直接 socket.send。UX 决策（gate / 节流 / sessionId
  // 计算）仍留视图层，发送路由下沉到 ChatManager → ClaudeChatProvider.interrupt。
  const lastInterruptAtRef = useRef<number>(0);
  const onInterrupt = useCallback(() => {
    if (!isRunning) return;
    const now = Date.now();
    if (now - lastInterruptAtRef.current < 300) return;
    lastInterruptAtRef.current = now;
    if (!sessionIdForAbort) return;
    if (!threadId || !chatManager) return;
    void chatManager
      .interrupt({ threadId, provider: "claude", sessionId: sessionIdForAbort })
      .catch((err) => {
        console.error("[ClaudeCodeView] interrupt failed", err);
      });
  }, [isRunning, sessionIdForAbort, threadId, chatManager]);

  // --- evolution store register/unregister ---
  useEffect(() => {
    if (!threadId) return;
    useEvolutionStore.getState().register(threadId);
    return () => useEvolutionStore.getState().unregister(threadId);
  }, [threadId]);

  // --- store selectors (#6 #7) ---
  const pendingNewSession = useThreadsStore((s) => s.pendingNewSession);
  const setPendingNewSession = useThreadsStore((s) => s.setPendingNewSession);
  const initialMessage = useThreadsStore((s) => s.initialMessage);
  const setInitialMessage = useThreadsStore((s) => s.setInitialMessage);
  const [failedInitialDraft, setFailedInitialDraft] =
    useState<SubmittedDraft | null>(null);
  const initialSendKeyRef = useRef<InitialMessageDraft | null>(null);
  const createThread = useThreadsStore((s) => s.createThread);
  const fetchThreads = useThreadsStore((s) => s.fetchThreads);
  const triggerClaudeProjectsRefresh = useThreadsStore(
    (s) => s.triggerClaudeProjectsRefresh,
  );

  const isPending =
    !threadId && pendingNewSession?.backendKind === "claude_code";

  useEffect(() => {
    setResumeSessionId(thread?.claude_thread_id?.trim() || null);
  }, [threadId, thread?.claude_thread_id]);

  const nextTurn = () => {
    turnSeqRef.current += 1;
    return turnSeqRef.current;
  };

  useEffect(() => {
    setItems([]);
    setHistoryLoading(false);
    streamIdRef.current = null;
    hadStreamThisTurnRef.current = false;
    turnSeqRef.current = 0;
    setStreamPhase("idle");
    setStreamToolName(undefined);
  }, [threadId]);

  // --- history loading (skip when pending) ---
  const claudeThreadId = thread?.claude_thread_id ?? "";
  useEffect(() => {
    if (!threadId || !claudeThreadId) return;
    let cancelled = false;
    setHistoryLoading(true);
    apiGet<{ messages: NormalizedMessage[] }>(
      `/api/threads/${threadId}/claude_history`,
    )
      .then((r) => {
        if (cancelled) return;
        const historyItems = convertHistoryToChatItems(threadId, r.messages);
        const maxTurn = historyItems.reduce((acc, item) => {
          if (
            isGenericChatItem(item) &&
            (item.kind === "assistant" || item.kind === "tool")
          ) {
            return Math.max(acc, item.turn);
          }
          return acc;
        }, 0);
        turnSeqRef.current = Math.max(turnSeqRef.current, maxTurn);
        setItems((prev) => [...historyItems, ...prev]);
        setHistoryLoading(false);
      })
      .catch((err) => {
        if (cancelled) return;
        console.error("[ClaudeCodeView] failed to load history", err);
        setHistoryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [threadId, claudeThreadId]);

  // --- WS message listener (skip when pending / no socket) ---
  useEffect(() => {
    if (!socket || !threadId) return;
    const off = socket.on((frame) => {
      if ("frame_type" in frame && frame.frame_type === "session-status") {
        if (!frame.isProcessing) {
          setItems((prev) =>
            prev.map((item) =>
              isGenericChatItem(item) &&
              item.id === streamIdRef.current &&
              item.kind === "assistant"
                ? { ...item, streaming: false }
                : item,
            ),
          );
          streamIdRef.current = null;
          hadStreamThisTurnRef.current = false;
          setStreamPhase("idle");
          setStreamToolName(undefined);
        }
        return;
      }

      // smart-approval-v1: auto_approval_state 帧不是 NormalizedMessage，先拦
      if (
        "frame_type" in frame &&
        (frame as { frame_type: string }).frame_type === "auto_approval_state"
      ) {
        useAutoApprovalStore
          .getState()
          .applyStateFrame(frame as unknown as AutoApprovalStateFrame);
        return;
      }

      // system.notice 帧不是 NormalizedMessage，在 cast 前拦截
      // 注意：不侵入 claude 频道的 items 对话流，不进入 claude SDK
      if (
        "frame_type" in frame &&
        (frame as { frame_type: string }).frame_type === "system.notice"
      ) {
        const noticeFrame = frame as unknown as SystemNoticeFrame;
        // evolution store 更新状态 + 触发 toast 通知（独立于对话流）
        useEvolutionStore.getState().handleFrame(threadId, noticeFrame);
        return;
      }

      const msg = frame as NormalizedMessage;
      if (typeof msg.sessionId === "string" && msg.sessionId.trim()) {
        setResumeSessionId(msg.sessionId.trim());
      }
      switch (msg.frame_type) {
        case "text": {
          if (pendingProjectsRefreshRef.current) {
            pendingProjectsRefreshRef.current = false;
            triggerClaudeProjectsRefresh();
          }
          const role = msg.role ?? "assistant";
          if (role === "assistant" && hadStreamThisTurnRef.current) {
            return;
          }
          const content = stringifyContent(msg.content);
          setItems((prev) => [
            ...prev,
            role === "user"
              ? {
                  id: newId(),
                  kind: "user",
                  threadId,
                  content,
                  timestampMs: Date.now(),
                }
              : {
                  id: newId(),
                  kind: "assistant",
                  threadId,
                  turn: nextTurn(),
                  runId: "claude-live",
                  content,
                  reasoning: "",
                  timestampMs: Date.now(),
                  streaming: false,
                },
          ]);
          streamIdRef.current = null;
          return;
        }
        case "thinking": {
          setItems((prev) => [
            ...prev,
            {
              id: newId(),
              kind: "assistant",
              threadId,
              turn: nextTurn(),
              runId: "claude-live",
              content: "",
              reasoning: stringifyContent(msg.content),
              timestampMs: Date.now(),
              streaming: false,
            },
          ]);
          streamIdRef.current = null;
          return;
        }
        case "stream_delta": {
          if (pendingProjectsRefreshRef.current) {
            pendingProjectsRefreshRef.current = false;
            triggerClaudeProjectsRefresh();
          }
          const delta = stringifyContent(msg.content);

          // input_json delta: 累积到最近的 pending ToolItem，不污染 assistant.content
          if (msg.deltaType === "input_json") {
            const targetToolId =
              typeof msg.toolId === "string" ? msg.toolId : null;
            setItems((prev) => {
              let matchedIdx = -1;
              for (let i = prev.length - 1; i >= 0; i--) {
                const item = prev[i];
                if (
                  isGenericChatItem(item) &&
                  item.kind === "tool" &&
                  item.pending === true
                ) {
                  if (targetToolId == null || item.callId === targetToolId) {
                    matchedIdx = i;
                    break;
                  }
                }
              }
              if (matchedIdx < 0) return prev;
              return prev.map((item, idx) =>
                idx === matchedIdx &&
                isGenericChatItem(item) &&
                item.kind === "tool"
                  ? {
                      ...item,
                      partialInput: (item.partialInput ?? "") + delta,
                    }
                  : item,
              );
            });
            return;
          }

          // text / thinking delta: 累积到 assistant ChatItem
          hadStreamThisTurnRef.current = true;
          setItems((prev) => {
            const sid = streamIdRef.current;
            if (sid) {
              return prev.map((item) =>
                isGenericChatItem(item) &&
                item.id === sid &&
                item.kind === "assistant"
                  ? { ...item, content: item.content + delta, streaming: true }
                  : item,
              );
            }
            const id = newId();
            streamIdRef.current = id;
            return [
              ...prev,
              {
                id,
                kind: "assistant",
                threadId,
                turn: nextTurn(),
                runId: "claude-live",
                content: delta,
                reasoning: "",
                timestampMs: Date.now(),
                streaming: true,
              },
            ];
          });
          return;
        }
        case "stream_end": {
          setItems((prev) =>
            prev.map((item) =>
              isGenericChatItem(item) &&
              item.id === streamIdRef.current &&
              item.kind === "assistant"
                ? { ...item, streaming: false }
                : item,
            ),
          );
          streamIdRef.current = null;
          // fix-claude-stop-button-stuck：对称清理 streamPhase / streamToolName。
          // 跟 complete 分支保持一致，避免单条流结束后状态不一致（仅用于内部状态一致性，
          // isRunning 的 stop→send 切换不依赖这里——见 isConversationEnded 注释）。
          setStreamPhase("idle");
          setStreamToolName(undefined);
          return;
        }
        case "stream_status": {
          const phase = msg.phase as "responding" | "thinking" | "tool_calling" | undefined;
          if (phase) {
            setStreamPhase(phase);
            setStreamToolName(phase === "tool_calling" ? (msg.toolName ?? undefined) : undefined);
            // tool_calling block 起始：建一张 pending ToolCard 占位，
            // 后续 input_json delta 累积 partialInput，tool_use 帧到达时原地 resolve
            if (phase === "tool_calling") {
              const toolId =
                typeof msg.toolId === "string" ? msg.toolId : null;
              const toolName =
                typeof msg.toolName === "string" && msg.toolName
                  ? msg.toolName
                  : "(未知工具)";
              setItems((prev) => [
                ...prev,
                {
                  id: newId(),
                  kind: "tool",
                  threadId,
                  turn: nextTurn(),
                  runId: "claude-live",
                  toolName,
                  callId: toolId ?? newId(),
                  arguments: {},
                  ok: null,
                  partialInput: "",
                  pending: true,
                  timestampMs: Date.now(),
                },
              ]);
            }
          }
          return;
        }
        case "tool_use": {
          const toolId = typeof msg.toolId === "string" ? msg.toolId : null;
          const args = asRecord(msg.toolInput ?? msg.input);
          setItems((prev) => {
            // 优先按 callId===toolId 匹配 pending ToolItem 原地 resolve
            if (toolId) {
              const idx = prev.findIndex(
                (item) =>
                  isGenericChatItem(item) &&
                  item.kind === "tool" &&
                  item.pending === true &&
                  item.callId === toolId,
              );
              if (idx >= 0) {
                return prev.map((item, i) =>
                  i === idx && isGenericChatItem(item) && item.kind === "tool"
                    ? {
                        ...item,
                        toolName: msg.toolName ?? item.toolName,
                        arguments: args,
                        partialInput: undefined,
                        pending: false,
                      }
                    : item,
                );
              }
            }
            // 兜底：没有匹配的 pending（如非流式分支或 toolId 缺失），
            // 走原有创建新 ToolItem 逻辑
            return [
              ...prev,
              {
                id: newId(),
                kind: "tool",
                threadId,
                turn: nextTurn(),
                runId: "claude-live",
                toolName: msg.toolName ?? "(未知工具)",
                callId: toolId ?? newId(),
                arguments: args,
                ok: null,
                timestampMs: Date.now(),
              },
            ];
          });
          streamIdRef.current = null;
          return;
        }
        case "tool_result": {
          setItems((prev) => {
            const idx = findLastToolIndex(prev, msg.toolId);
            const patch = splitToolResult(msg.content);
            if (idx >= 0) {
              return prev.map((item, index) =>
                index === idx &&
                isGenericChatItem(item) &&
                item.kind === "tool"
                  ? {
                      ...item,
                      ok: !(msg.isError ?? false),
                      ...patch,
                    }
                  : item,
              );
            }
            return [
              ...prev,
              {
                id: newId(),
                kind: "tool",
                threadId,
                turn: nextTurn(),
                runId: "claude-live",
                toolName: msg.toolId ?? "tool_result",
                callId: msg.toolId ?? newId(),
                arguments: {},
                ok: !(msg.isError ?? false),
                timestampMs: Date.now(),
                ...patch,
              },
            ];
          });
          streamIdRef.current = null;
          return;
        }
        case "status": {
          setItems((prev) => [
            ...prev,
            { kind: "status", content: msg.content, id: newId() },
          ]);
          return;
        }
        case "complete": {
          if (pendingProjectsRefreshRef.current) {
            pendingProjectsRefreshRef.current = false;
            triggerClaudeProjectsRefresh();
          }
          setItems((prev) => [
            ...prev.map((item) =>
              isGenericChatItem(item) &&
              item.id === streamIdRef.current &&
              item.kind === "assistant"
                ? { ...item, streaming: false }
                : item,
            ),
            {
              kind: "complete",
              aborted: msg.aborted,
              id: newId(),
            },
          ]);
          streamIdRef.current = null;
          hadStreamThisTurnRef.current = false;
          setStreamPhase("idle");
          setStreamToolName(undefined);
          return;
        }
        case "error": {
          setItems((prev) => [
            ...prev,
            {
              id: newId(),
              kind: "error",
              threadId,
              message: msg.error ?? "(未知)",
              errorCode: "claude_code",
              timestampMs: Date.now(),
            },
          ]);
          streamIdRef.current = null;
          setStreamPhase("idle");
          setStreamToolName(undefined);
          return;
        }
        case "session_created": {
          if (typeof msg.newSessionId === "string" && msg.newSessionId.trim()) {
            setResumeSessionId(msg.newSessionId.trim());
          }
          pendingProjectsRefreshRef.current = true;
          return;
        }
        case "interactive_prompt":
        case "task_notification": {
          return;
        }
      }
    });
    return off;
  }, [socket, threadId, triggerClaudeProjectsRefresh]);

  // --- #7: initialMessage buffer — auto-send after WS open ---
  useEffect(() => {
    if (
      !threadId ||
      !socket ||
      state !== "open" ||
      !initialMessage ||
      !chatManager
    ) {
      return;
    }
    const pendingMessage = initialMessage;
    if (initialSendKeyRef.current === pendingMessage) return;
    initialSendKeyRef.current = pendingMessage;
    setFailedInitialDraft(null);
    setInitialMessage(null);
    void chatManager
      .sendMessage({
        common: {
          text: pendingMessage.text,
          attachments: pendingMessage.attachments,
          reasoningEffort: pendingMessage.reasoningEffort,
          references: pendingMessage.references,
        },
        provider: { provider: "claude", threadId },
      })
      .then(() => {
        setItems((prev) => [
          ...prev,
          {
            id: newId(),
            kind: "user",
            threadId,
            content: pendingMessage.restoreDraft.text,
            timestampMs: Date.now(),
            ...(pendingMessage.attachments &&
            pendingMessage.attachments.length > 0
              ? { attachments: pendingMessage.attachments }
              : {}),
          },
        ]);
        void fetchThreads();
      })
      .catch((err: unknown) => {
        const message = err instanceof Error ? err.message : String(err);
        setFailedInitialDraft(pendingMessage.restoreDraft);
        toast.error(`发送失败：${message}`);
      });
  }, [threadId, socket, state, initialMessage, setInitialMessage, fetchThreads, chatManager]);

  // --- #6: onSend with pending-mode create-thread flow ---
  // claude-code-channel-image-paste: 加 attachments 参数 — 把 Composer
  // 粘贴的图片 ref 透传给 WS claude-command 帧，后端 _attachment_prefix 拼
  // ``@<abs_path>`` 进 prompt 头部走 SDK FileReadTool 自动读图。
  // message-runtime #8: reasoningEffort 三频道贯通 — 不再丢 Composer 的 reasoning。
  //   ClaudeChatProvider 把 reasoningEffort 透传到 wire 帧 options.reasoningEffort，
  //   后端目前不消费（待后端 task），但前端契约层不再断链。
  const onSend = async (
    text: string,
    attachments?: import("@/protocol").UserInputAttachment[],
    reasoningEffort?: import("@/chat/types").ReasoningEffort | null,
    references?: import("@/protocol").ConversationReferenceDTO[],
    submittedDraft?: SubmittedDraft,
  ) => {
    // #5/#6: pending mode — create thread first
    if (isPending) {
      const pending = pendingNewSession;
      if (!pending) return;
      try {
        const created = await createThread(
          pending.projectName,
          "",
          "claude_code",
          pending.cwd,
        );
        setPendingNewSession(null);
        setInitialMessage({
          text,
          reasoningEffort: reasoningEffort ?? null,
          attachments,
          references,
          restoreDraft: submittedDraft ?? {
            text,
            reasoningEffort: reasoningEffort ?? null,
            attachments: attachments ?? [],
            references: references ?? [],
          },
        });
        navigate(`/chat/${created.id}`, { replace: true });
        return true;
      } catch (err: unknown) {
        // #8: error handling — toast + keep input
        const msg =
          err instanceof Error ? err.message : String(err);
        toast.error(`创建会话失败：${msg}`);
        return false;
      }
    }

    // normal mode — send via WS
    if (!threadId) return;
    if (!socket || state !== "open") return;
    if (!chatManager) return false;
    try {
      await chatManager.sendMessage({
        common: { text, attachments, reasoningEffort, references },
        provider: { provider: "claude", threadId },
      });
      setItems((prev) => [
        ...prev,
        {
          id: newId(),
          kind: "user",
          threadId,
          content: text,
          timestampMs: Date.now(),
          ...(attachments && attachments.length > 0 ? { attachments } : {}),
        },
      ]);
      setFailedInitialDraft(null);
      return true;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      toast.error(`发送失败：${message}`);
      return false;
    }
  };

  // 按 user 消息分界插入文件汇总（通用 hook，三频道共用）
  const renderItems = useItemsWithFileSummary(items, threadId,
    (it: ClaudeRenderItem) => isGenericChatItem(it) && it.kind === "user");

  return (
    <div
      className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden"
      data-testid="claude-code-layout"
    >
      <div className="min-h-0 flex-1 overflow-hidden" data-testid="claude-code-viewport">
        <MessageViewport
          items={renderItems}
          emptyText={
            isPending
              ? "输入消息开始新的 Claude Code 会话"
              : historyLoading
                ? "加载历史中..."
                : `Claude Code 会话已就绪（${state}）。发送一条消息开始。`
          }
          resetKey={threadId ?? "__pending__"}
          getUserMessageCount={(list) =>
            list.filter(
              (item) => "kind" in item && item.kind === "user",
            ).length
          }
          renderItem={(item) =>
            "kind" in item && item.kind === "files-summary" ? (
              <ModifiedFilesSummary key={item.id} files={item.files} threadId={item.threadId} />
            ) : isGenericChatItem(item as ClaudeRenderItem) ? (
              <ChatMessageItem key={item.id} item={item as GenericChatItem} />
            ) : (
              <ClaudeMetaRow key={item.id} item={item as ClaudeMetaItem} />
            )
          }
        />
      </div>
      {streamPhase !== "idle" && (
        <div className="flex items-center gap-2 px-4 py-1.5 text-xs text-muted-foreground">
          <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
          {streamPhase === "thinking"
            ? "思考中..."
            : streamPhase === "tool_calling"
              ? `调用工具: ${streamToolName ?? ""}...`
              : "生成中..."}
        </div>
      )}
      <Composer
        disabled={
          isPending ? false : state !== "open" || isRunning || isDispatching
        }
        onSubmit={(text, reasoning, attachments, references, submittedDraft) =>
          onSend(text, attachments, reasoning, references, submittedDraft)
        }
        draftSeed={failedInitialDraft}
        threadId={threadId}
        isRunning={isRunning}
        onInterrupt={onInterrupt}
        leftActions={
          /* smart-approval-v1: 只在已 bound cwd + socket 时挂；其他场景不渲染 */
          thread?.cwd && socket ? (
            <AutoApprovalModeSelector cwd={thread.cwd} socket={socket} />
          ) : null
        }
      />
    </div>
  );
}

function ClaudeMetaRow({ item }: { item: ClaudeMetaItem }) {
  switch (item.kind) {
    case "status":
      return (
        <div className="self-center text-xs text-muted-foreground">
          {stringifyContent(item.content)}
        </div>
      );
    case "complete": {
      return (
        <div className="self-center rounded border px-3 py-1 text-xs text-muted-foreground">
          {item.aborted ? "对话已中止" : "对话结束"}
        </div>
      );
    }
  }
}
