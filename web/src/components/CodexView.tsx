import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { ChatMessageItem, MessageViewport } from "@/components/MessageList";
import { Composer, type SubmittedDraft } from "@/components/Composer";
import {
  CodexPermissionModeSelector,
  type CodexPermissionMode,
} from "@/components/CodexPermissionMode";
import { useCodexWS } from "@/hooks/useCodexWS";
import { ChatManager } from "@/chat/ChatManager";
import { makeNetworkHandle, getTimelineStore } from "@/chat/runtimeWiring";
import { useThreadRunning } from "@/hooks/useThreadRunning";
import { apiGet } from "@/lib/api";
import type { NormalizedMessage, ThreadMetadataDTO } from "@/protocol";
import type { ChatItem as GenericChatItem } from "@/stores/chat";
import { useItemsWithFileSummary } from "@/hooks/useItemsWithFileSummary";
import { ModifiedFilesSummary } from "@/components/ModifiedFilesSummary";
import {
  useThreadsStore,
  type InitialMessageDraft,
} from "@/stores/threads";
import { useThreadDispatchStore } from "@/stores/threadDispatch";

type CodexMetaItem =
  | { kind: "status"; content?: unknown; id: string }
  | {
      kind: "complete";
      aborted?: boolean;
      id: string;
    };

type CodexRenderItem = GenericChatItem | CodexMetaItem;

let _gid = 0;
const newId = () => `cx-${Date.now()}-${++_gid}`;

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

function isGenericChatItem(item: CodexRenderItem): item is GenericChatItem {
  return (
    item.kind === "user" ||
    item.kind === "assistant" ||
    item.kind === "tool" ||
    item.kind === "error"
  );
}

function findLastToolIndex(items: CodexRenderItem[], toolId?: string): number {
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
): CodexRenderItem[] {
  const items: CodexRenderItem[] = [];
  let n = 0;
  const hid = () => `cx-history-${++n}`;
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
            runId: "codex-history",
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
          runId: "codex-history",
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
          runId: "codex-history",
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
            runId: "codex-history",
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

export function CodexView({ threadId, thread }: Props) {
  const navigate = useNavigate();
  const { socket, state } = useCodexWS(threadId);
  const [items, setItems] = useState<CodexRenderItem[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [permissionMode, setPermissionMode] =
    useState<CodexPermissionMode>("acceptEdits");
  // message-runtime-v0.1 #5：codex 发送走统一 ChatManager。保留 pending 创建时序，
  // 仅把 codex-command 帧的组装收口到 CodexChatProvider（permissionMode 经 provider 选项透传）。
  const chatManager = useMemo(() => {
    if (!socket) return null;
    return new ChatManager({
      resolveHandle: (_kind, tid) =>
        makeNetworkHandle(tid, (frame) =>
          socket.send(frame as Parameters<typeof socket.send>[0]),
        ),
      ensureThread: async (req) => req.provider.threadId,
      timelineFor: (tid) => getTimelineStore(tid),
    });
  }, [socket]);

  // chat-running-state-unify #4：codex 频道补 Stop 按钮（之前 Composer 没传
  // isRunning/onInterrupt，没接打断）。isRunning 走三频道共享 useThreadRunning
  // （后端 thread-status phase 真源）；abort-session 需要 sessionId——优先用
  // listener 收 session_created/任意带 sessionId 帧后更新的 live sid，否则退到
  // thread.codex_thread_id（落盘的 resume sid）。参考 ClaudeCodeView 同模式。
  const [resumeSessionId, setResumeSessionId] = useState<string | null>(null);
  const isRunning = useThreadRunning(threadId);
  const isDispatching = useThreadDispatchStore(
    (store) =>
      threadId
        ? store.byThreadId[threadId]?.phase === "dispatching"
        : false,
  );
  const sessionIdForAbort = useMemo(() => {
    const live = resumeSessionId?.trim();
    if (live) return live;
    const fallback = thread?.codex_thread_id?.trim();
    return fallback || null;
  }, [resumeSessionId, thread?.codex_thread_id]);
  // message-runtime #9：发帧改走 chatManager.interrupt 统一接口（三频道一致），
  // 不再视图层直接 socket.send。UX gate 与 sessionId 计算仍在视图层。
  const onInterrupt = useCallback(() => {
    if (!isRunning) return;                                 // 没在跑不发帧
    if (!sessionIdForAbort) return;                          // 早 return 避免发空帧
    if (!threadId || !chatManager) return;
    void chatManager
      .interrupt({ threadId, provider: "codex", sessionId: sessionIdForAbort })
      .catch((err) => {
        console.error("[CodexView] interrupt failed", err);
      });
  }, [isRunning, sessionIdForAbort, threadId, chatManager]);

  const streamIdRef = useRef<string | null>(null);
  const hadStreamThisTurnRef = useRef(false);
  const turnSeqRef = useRef(0);
  const pendingNewSession = useThreadsStore((s) => s.pendingNewSession);
  const setPendingNewSession = useThreadsStore((s) => s.setPendingNewSession);
  const initialMessage = useThreadsStore((s) => s.initialMessage);
  const setInitialMessage = useThreadsStore((s) => s.setInitialMessage);
  const [failedInitialDraft, setFailedInitialDraft] =
    useState<SubmittedDraft | null>(null);
  const initialSendKeyRef = useRef<InitialMessageDraft | null>(null);
  const createThread = useThreadsStore((s) => s.createThread);
  const fetchThreads = useThreadsStore((s) => s.fetchThreads);
  const isPending =
    !threadId && pendingNewSession?.backendKind === "codex";

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
  }, [threadId]);

  const codexThreadId = thread?.codex_thread_id ?? "";
  useEffect(() => {
    if (!threadId || !codexThreadId) return;
    let cancelled = false;
    setHistoryLoading(true);
    apiGet<{ messages: NormalizedMessage[] }>(
      `/api/codex/sessions/${encodeURIComponent(codexThreadId)}/history`,
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
        console.error("[CodexView] failed to load history", err);
        setHistoryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [threadId, codexThreadId]);

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
        provider: { provider: "codex", threadId, permissionMode },
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
  }, [
    threadId,
    socket,
    state,
    initialMessage,
    setInitialMessage,
    permissionMode,
    fetchThreads,
    chatManager,
  ]);

  useEffect(() => {
    if (!socket || !threadId) return;
    const off = socket.on((frame) => {
      if ("frame_type" in frame && frame.frame_type === "session-status") {
        return;
      }

      const msg = frame as NormalizedMessage;
      // #4：任何带 sessionId 的 NormalizedMessage 都更新 live sid，确保 abort 有最新 sessionId。
      if (typeof msg.sessionId === "string" && msg.sessionId.trim()) {
        setResumeSessionId(msg.sessionId.trim());
      }
      switch (msg.frame_type) {
        case "text": {
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
                  runId: "codex-live",
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
              runId: "codex-live",
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
          const delta = stringifyContent(msg.content);
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
                runId: "codex-live",
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
          return;
        }
        case "tool_use": {
          setItems((prev) => [
            ...prev,
            {
              id: newId(),
              kind: "tool",
              threadId,
              turn: nextTurn(),
              runId: "codex-live",
              toolName: msg.toolName ?? "(未知工具)",
              callId: msg.toolId ?? newId(),
              arguments: asRecord(msg.toolInput ?? msg.input),
              ok: null,
              timestampMs: Date.now(),
            },
          ]);
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
                runId: "codex-live",
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
              errorCode: "codex",
              timestampMs: Date.now(),
            },
          ]);
          streamIdRef.current = null;
          return;
        }
        case "session_created": {
          // SDK 创建 session 后下发真实 sid——优先级最高，强制覆盖。
          if (typeof msg.newSessionId === "string" && msg.newSessionId.trim()) {
            setResumeSessionId(msg.newSessionId.trim());
          }
          return;
        }
        case "interactive_prompt":
        case "task_notification": {
          return;
        }
      }
    });
    return off;
  }, [socket, threadId]);

  // codex-channel-image-paste §4：加 attachments 参数，把 Composer 粘贴的图片 ref
  // 透传给 WS codex-command 帧，后端 _image_cli_args 拼成 --image flag 给 codex exec。
  // 仅本轮 optimistic 缩略图（setItems user item 加 attachments 字段让气泡显示）；
  // 刷新后历史回显不闭环（Codex jsonl 存 base64 而非 asset_id，独立后续 task）。
  // message-runtime #8: reasoningEffort 三频道贯通 — 不再丢 Composer 的 reasoning。
  //   CodexChatProvider 把 reasoningEffort 透传到 wire 帧 options.reasoningEffort；
  //   codex 后端目前不消费 reasoning_effort（待后端 task），但前端契约不再断链。
  const onSend = async (
    text: string,
    attachments?: import("@/protocol").UserInputAttachment[],
    reasoningEffort?: import("@/chat/types").ReasoningEffort | null,
    references?: import("@/protocol").ConversationReferenceDTO[],
    submittedDraft?: SubmittedDraft,
  ) => {
    if (isPending) {
      const pending = pendingNewSession;
      if (!pending) return;
      try {
        const created = await createThread(
          pending.projectName,
          "",
          "codex",
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
        const msg = err instanceof Error ? err.message : String(err);
        toast.error(`创建会话失败：${msg}`);
        return false;
      }
    }

    if (!threadId) return;
    if (!socket || state !== "open") return;
    if (!chatManager) return false;
    try {
      await chatManager.sendMessage({
        common: { text, attachments, reasoningEffort, references },
        provider: { provider: "codex", threadId, permissionMode },
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
    (it: CodexRenderItem) => isGenericChatItem(it) && it.kind === "user");

  return (
    <div
      className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden"
      data-testid="codex-layout"
    >
      <div className="min-h-0 flex-1 overflow-hidden" data-testid="codex-viewport">
        <MessageViewport
          items={renderItems}
          emptyText={
            isPending
              ? "输入消息开始新的 Codex 会话"
              : historyLoading
                ? "加载历史中..."
                : `Codex 会话已就绪（${state}）。发送一条消息开始。`
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
            ) : isGenericChatItem(item) ? (
              <ChatMessageItem key={item.id} item={item} />
            ) : (
              <CodexMetaRow key={item.id} item={item} />
            )
          }
        />
      </div>
      <div className="flex items-center gap-2 border-t px-3 py-1.5">
        <CodexPermissionModeSelector
          value={permissionMode}
          onChange={setPermissionMode}
          disabled={!isPending && state !== "open"}
        />
      </div>
      <Composer
        disabled={
          (!isPending && state !== "open") || isRunning || isDispatching
        }
        isRunning={isRunning}
        onInterrupt={onInterrupt}
        onSubmit={(text, reasoning, attachments, references, submittedDraft) =>
          onSend(text, attachments, reasoning, references, submittedDraft)
        }
        draftSeed={failedInitialDraft}
        threadId={threadId}
      />
    </div>
  );
}

function CodexMetaRow({ item }: { item: CodexMetaItem }) {
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
