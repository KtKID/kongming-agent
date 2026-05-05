import { useEffect, useRef, useState } from "react";
import { ChatMessageItem, MessageViewport } from "@/components/MessageList";
import { Composer } from "@/components/Composer";
import {
  ClaudeApprovalDialog,
  type ClaudeApprovalRequest,
} from "@/components/ClaudeApprovalDialog";
import { useClaudeCodeWS } from "@/hooks/useClaudeCodeWS";
import { apiGet } from "@/lib/api";
import type { NormalizedMessage, ThreadMetadataDTO } from "@/protocol";
import type { ChatItem as GenericChatItem } from "@/stores/chat";

type ClaudeMetaItem =
  | { kind: "status"; content?: unknown; id: string }
  | {
      kind: "complete";
      tokenBudget?: Record<string, unknown>;
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
    item.kind === "approval" ||
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
    switch (msg.kind) {
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
  threadId: string;
  thread?: ThreadMetadataDTO;
}

export function ClaudeCodeView({ threadId, thread }: Props) {
  const { socket, state } = useClaudeCodeWS(threadId);
  const [items, setItems] = useState<ClaudeRenderItem[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [pendingApproval, setPendingApproval] =
    useState<ClaudeApprovalRequest | null>(null);
  const streamIdRef = useRef<string | null>(null);
  const hadStreamThisTurnRef = useRef(false);
  const turnSeqRef = useRef(0);

  const nextTurn = () => {
    turnSeqRef.current += 1;
    return turnSeqRef.current;
  };

  useEffect(() => {
    setItems([]);
    setPendingApproval(null);
    setHistoryLoading(false);
    streamIdRef.current = null;
    hadStreamThisTurnRef.current = false;
    turnSeqRef.current = 0;
  }, [threadId]);

  const sdkSessionId = thread?.sdk_session_id ?? "";
  useEffect(() => {
    if (!sdkSessionId) return;
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
  }, [threadId, sdkSessionId]);

  useEffect(() => {
    if (!socket) return;
    const off = socket.on((frame) => {
      if ("type" in frame && frame.type === "session-status") {
        return;
      }

      const msg = frame as NormalizedMessage;
      switch (msg.kind) {
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
              runId: "claude-live",
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
              tokenBudget: msg.tokenBudget,
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
              errorCode: "claude_code",
              timestampMs: Date.now(),
            },
          ]);
          streamIdRef.current = null;
          return;
        }
        case "permission_request": {
          if (typeof msg.requestId === "string" && typeof msg.toolName === "string") {
            setPendingApproval({
              requestId: msg.requestId,
              toolName: msg.toolName,
              toolInput: msg.toolInput ?? msg.input,
            });
          }
          return;
        }
        case "permission_cancelled": {
          if (
            pendingApproval &&
            typeof msg.requestId === "string" &&
            pendingApproval.requestId === msg.requestId
          ) {
            setPendingApproval(null);
          }
          return;
        }
        case "session_created":
        case "interactive_prompt":
        case "task_notification": {
          return;
        }
      }
    });
    return off;
  }, [pendingApproval, socket, threadId]);

  const onSend = (text: string) => {
    if (!socket || state !== "open") return;
    setItems((prev) => [
      ...prev,
      {
        id: newId(),
        kind: "user",
        threadId,
        content: text,
        timestampMs: Date.now(),
      },
    ]);
    socket.send({
      type: "claude-command",
      command: text,
      options: {},
    });
  };

  const onApprovalResolve = (resp: {
    requestId: string;
    allow: boolean;
    message?: string;
    rememberEntry?: string;
  }) => {
    if (socket && state === "open") {
      socket.send({
        type: "claude-permission-response",
        requestId: resp.requestId,
        allow: resp.allow,
        ...(resp.message ? { message: resp.message } : {}),
        ...(resp.rememberEntry ? { rememberEntry: resp.rememberEntry } : {}),
      });
    }
    setPendingApproval(null);
  };

  return (
    <div
      className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden"
      data-testid="claude-code-layout"
    >
      <div className="min-h-0 flex-1 overflow-hidden" data-testid="claude-code-viewport">
        <MessageViewport
          items={items}
          emptyText={
            historyLoading
              ? "加载历史中..."
              : `Claude Code 会话已就绪（${state}）。发送一条消息开始。`
          }
          resetKey={threadId}
          getUserMessageCount={(list) =>
            list.filter(
              (item) => isGenericChatItem(item) && item.kind === "user",
            ).length
          }
          renderItem={(item) =>
            isGenericChatItem(item) ? (
              <ChatMessageItem key={item.id} item={item} />
            ) : (
              <ClaudeMetaRow key={item.id} item={item} />
            )
          }
        />
      </div>
      <Composer
        disabled={state !== "open"}
        onSubmit={(text) => onSend(text)}
        threadId={threadId}
      />
      <ClaudeApprovalDialog
        open={!!pendingApproval}
        request={pendingApproval}
        onResolve={onApprovalResolve}
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
      const summary = item.tokenBudget
        ? Object.entries(item.tokenBudget)
            .map(([key, value]) => `${key}=${stringifyContent(value)}`)
            .join(" · ")
        : "";
      return (
        <div className="self-center rounded border px-3 py-1 text-xs text-muted-foreground">
          {item.aborted ? "对话已中止" : "对话结束"}
          {summary ? ` · ${summary}` : ""}
        </div>
      );
    }
  }
}
