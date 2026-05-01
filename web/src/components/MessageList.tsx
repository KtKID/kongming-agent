import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Markdown } from "@/lib/markdown";
import { Badge } from "@/components/ui/badge";
import { ChatAvatar } from "@/components/ChatAvatar";
import { useChatStore, type ChatItem } from "@/stores/chat";
import { isDebugMode } from "@/lib/debug";
import { cn } from "@/lib/utils";

/**
 * 消息列表：按 ChatItem.kind 分类渲染。
 *
 * - user：右对齐气泡
 * - assistant：左对齐气泡 + reasoning 折叠 + Markdown
 * - tool：折叠卡片 + status badge
 * - approval：内联 banner（modal 由 ApprovalDialog 单独负责）
 * - error：醒目横幅
 *
 * Smart auto-scroll：
 * - 用户在底部（距底部 < 80px）→ 新内容自动滚到底
 * - 用户滚上去了 → 不自动滚，显示"回到底部"按钮
 * - 用户发消息 → 始终滚到底部（通过 userMsgCount 检测）
 * - 流式内容 → 持续跟随（rAF commit 改变 items 引用 → effect 重触发）
 */
const EMPTY_ITEMS: ChatItem[] = [];
const NEAR_BOTTOM_PX = 80;

export function MessageList({
  threadId,
}: {
  threadId: string | undefined;
}) {
  const items = useChatStore((s) =>
    threadId ? (s.itemsByThread[threadId] ?? EMPTY_ITEMS) : EMPTY_ITEMS,
  );
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const isNearBottomRef = useRef(true);
  const prevUserMsgCount = useRef(0);
  const [showScrollBtn, setShowScrollBtn] = useState(false);

  // --- scroll detection ---
  const checkNearBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    const near = dist < NEAR_BOTTOM_PX;
    isNearBottomRef.current = near;
    setShowScrollBtn(!near);
  }, []);

  // --- auto-scroll on content change ---
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;

    const userMsgCount = items.filter((it) => it.kind === "user").length;

    if (userMsgCount > prevUserMsgCount.current) {
      // New user message — always scroll to bottom
      el.scrollTop = el.scrollHeight;
      isNearBottomRef.current = true;
      setShowScrollBtn(false);
    } else if (isNearBottomRef.current) {
      // Auto-scroll only when user is near bottom.
      // If user scrolled up to read earlier content, respect that —
      // never force-jump during streaming or tool card insertion.
      el.scrollTop = el.scrollHeight;
    }

    prevUserMsgCount.current = userMsgCount;
  }, [items]);

  // --- reset on thread change ---
  useEffect(() => {
    prevUserMsgCount.current = 0;
    isNearBottomRef.current = true;
    setShowScrollBtn(false);
  }, [threadId]);

  // --- scroll to bottom (button click) ---
  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    isNearBottomRef.current = true;
    setShowScrollBtn(false);
  }, []);

  if (!threadId) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        在左侧选择或创建一个 thread
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        说点什么开始
      </div>
    );
  }

  return (
    <div className="relative h-full">
      <div
        ref={scrollRef}
        onScroll={checkNearBottom}
        className="h-full overflow-y-auto"
      >
        <div className="mx-auto w-full max-w-3xl p-6">
          <div className="flex flex-col gap-4">
            {items.map((it) => (
              <MessageWithDebugBadge key={it.id} item={it} />
            ))}
          </div>
        </div>
      </div>
      {showScrollBtn && (
        <button
          type="button"
          onClick={scrollToBottom}
          className="absolute bottom-4 left-1/2 -translate-x-1/2 flex items-center gap-1 rounded-full border border-border bg-background/90 px-3 py-1.5 text-xs text-muted-foreground shadow-md backdrop-blur-sm transition-colors hover:bg-secondary"
          aria-label="回到底部"
        >
          <ChevronDown className="h-3.5 w-3.5" />
          回到最新
        </button>
      )}
    </div>
  );
}

/**
 * Debug 包装：debug 开启时给每条 ChatItem 角上贴 monospace 小 badge，
 * 内容 `${id前8} │ ${runId后6} │ t${turn}`。仅 debug 模式下渲染，避免污染正常视图。
 *
 * 用途：肉眼验证 (threadId, runId, turn) 复合 key 是否正确生成；修非流式
 * 多轮覆盖 bug 后的快速回归手段。
 */
function MessageWithDebugBadge({ item }: { item: ChatItem }) {
  // 同步读一次 debug 开关；不响应式，避免不必要的 re-render
  const debug = useMemo(() => isDebugMode(), []);
  if (!debug) {
    return <Message item={item} />;
  }

  const turn =
    "turn" in item ? `t${item.turn}` : "—";
  const runId =
    "runId" in item && item.runId ? item.runId.slice(-6) : "—";
  const idHead = item.id.slice(0, 8);

  return (
    <div className="relative" data-testid="debug-badge-wrap">
      <Message item={item} />
      <div
        data-testid="debug-badge"
        className="pointer-events-none absolute right-0 top-0 z-10 rounded-bl-md rounded-tr-md border border-border bg-background/90 px-1.5 py-0.5 font-mono text-[10px] leading-tight text-muted-foreground shadow-sm backdrop-blur-sm"
      >
        {idHead} │ {runId} │ {turn}
      </div>
    </div>
  );
}

function Message({ item }: { item: ChatItem }) {
  switch (item.kind) {
    case "user":
      return (
        <div className="flex items-end justify-end gap-2">
          <div className="max-w-[80%] rounded-2xl rounded-br-md bg-accent px-4 py-2 text-sm text-accent-foreground shadow-sm">
            <Markdown text={item.content} className="leading-relaxed" />
          </div>
          <ChatAvatar role="user" />
        </div>
      );
    case "assistant": {
      // 空 assistant 帧（只发 tool_calls 无文本）不渲染头像+气泡
      if (!item.content && !item.streaming && !item.reasoning) return null;
      return (
        <div className="flex items-start gap-2">
          <ChatAvatar role="assistant" />
          <div className="min-w-0 flex-1">
            <AssistantMessage item={item} />
          </div>
        </div>
      );
    }
    case "tool":
      return <ToolCard item={item} />;
    case "approval":
      return (
        <div className="rounded-md border border-warning bg-warning/10 px-4 py-2 text-xs text-foreground">
          <div className="font-semibold">需要审批：{item.toolName}</div>
          {item.reason ? (
            <div className="text-muted-foreground">{item.reason}</div>
          ) : null}
        </div>
      );
    case "error":
      return (
        <div
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/10 px-4 py-2 text-xs text-destructive"
          data-testid="error-banner"
        >
          [{item.errorCode}] {item.message}
        </div>
      );
    default: {
      const _exhaustive: never = item;
      void _exhaustive;
      return null;
    }
  }
}

function AssistantMessage({
  item,
}: {
  item: Extract<ChatItem, { kind: "assistant" }>;
}) {
  const [reasoningOpen, setReasoningOpen] = useState(false);
  // v0.1.6：assistant 只发 tool_calls 时 content 为空字符串（语义"无文本输出"）。
  // 既无 content、又不在流式中、也没 reasoning 时整个气泡不渲染——避免在用户消息
  // 后跟一个空白框（以前更糟：后端 str(None) → "None" 字面，已在 ws.py 修；
  // 这里再加一道防御兜底）。
  const hasContent = Boolean(item.content);
  const hasReasoning = Boolean(item.reasoning);
  if (!hasContent && !item.streaming && !hasReasoning) return null;
  return (
    <div className="flex flex-col gap-2">
      {hasReasoning ? (
        <button
          type="button"
          onClick={() => setReasoningOpen((v) => !v)}
          className="inline-flex w-fit items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-secondary"
        >
          {reasoningOpen ? (
            <ChevronDown className="h-3 w-3" />
          ) : (
            <ChevronRight className="h-3 w-3" />
          )}
          reasoning
        </button>
      ) : null}
      {reasoningOpen && hasReasoning ? (
        <pre className="whitespace-pre-wrap rounded-md border border-border bg-muted p-3 text-xs text-muted-foreground">
          {item.reasoning}
        </pre>
      ) : null}
      {hasContent || item.streaming ? (
        <div className="rounded-2xl rounded-bl-md bg-card px-4 py-2 text-sm shadow-sm">
          <Markdown text={item.content} />
          {item.streaming ? (
            <span
              aria-label="streaming"
              className="ml-1 inline-block h-4 w-1 animate-pulse bg-accent align-middle"
            />
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function ToolCard({
  item,
}: {
  item: Extract<ChatItem, { kind: "tool" }>;
}) {
  const [open, setOpen] = useState(false);
  const status =
    item.ok === null
      ? "running"
      : item.ok
        ? "ok"
        : "fail";
  const variant =
    status === "running"
      ? "secondary"
      : status === "ok"
        ? "success"
        : "destructive";
  return (
    <div className="rounded-md border border-border bg-card px-4 py-2 text-xs">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2"
      >
        {open ? (
          <ChevronDown className="h-3 w-3" />
        ) : (
          <ChevronRight className="h-3 w-3" />
        )}
        <span className="font-mono font-semibold">{item.toolName}</span>
        <Badge variant={variant} className={cn("ml-auto")}>
          {status}
        </Badge>
      </button>
      {open ? (
        <div className="mt-2 space-y-2">
          {/* 入参（arguments） */}
          <div>
            <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
              arguments
            </div>
            <pre className="overflow-x-auto rounded bg-muted p-2 font-mono text-[11px]">
              {JSON.stringify(item.arguments, null, 2)}
            </pre>
          </div>
          {/* 结果文本（content）；只有 tool.call.end 后才会出现 */}
          {item.result ? (
            <div>
              <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
                result
              </div>
              <pre className="max-h-80 overflow-auto rounded bg-muted p-2 font-mono text-[11px]">
                {item.result}
              </pre>
            </div>
          ) : null}
          {/* 结果结构化（data）；非 null/undefined 时显示 */}
          {item.resultData ? (
            <div>
              <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
                data
              </div>
              <pre className="max-h-60 overflow-auto rounded bg-muted p-2 font-mono text-[11px]">
                {JSON.stringify(item.resultData, null, 2)}
              </pre>
            </div>
          ) : null}
          {item.errorMessage ? (
            <div className="text-destructive">{item.errorMessage}</div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
