import {
  Fragment,
  createContext,
  useCallback,
  useContext,
  useEffect,
  memo,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Copy,
  FileText,
  GitFork,
  LoaderCircle,
  Sparkles,
  Sigma,
  XCircle,
} from "lucide-react";
import { EvolutionDecisionModal } from "@/components/EvolutionDecisionModal";
import { ImageLightbox } from "@/components/ImageLightbox";
import { Markdown } from "@/lib/markdown";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ChatAvatar } from "@/components/ChatAvatar";
import { useChatStore, type ChatItem } from "@/stores/chat";
import type { ConversationReferenceDTO, UserInputAttachment } from "@/protocol";
import { isDebugMode } from "@/lib/debug";
import { cn } from "@/lib/utils";
import { useWorkspaceStore } from "@/stores/workspace";
import { ModifiedFilesSummary } from "@/components/ModifiedFilesSummary";
import { useItemsWithFileSummary } from "@/hooks/useItemsWithFileSummary";
import { ConversationReferenceManager } from "@/modules/conversation-references/ConversationReferenceManager";

/**
 * 消息列表：按 ChatItem.kind 分类渲染。
 *
 * - user：右对齐气泡
 * - assistant：左对齐气泡 + reasoning 折叠 + Markdown
 * - tool：折叠卡片 + status badge
 * - system：时间线系统提示卡片
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

type ForkLineage = {
  parentThreadId: string;
  historyIndex: number;
};

function ForkLineageNavigation({
  parentThreadId,
  historyIndex,
}: ForkLineage) {
  return (
    <div
      className="flex items-center gap-4 py-3"
      data-history-index={historyIndex}
      data-testid="fork-lineage-navigation"
    >
      <div aria-hidden="true" className="h-px flex-1 bg-border/70" />
      <Link
        to={`/chat/${parentThreadId}`}
        className="inline-flex items-center gap-2 text-sm font-medium text-primary transition-colors hover:text-primary/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      >
        <GitFork aria-hidden="true" className="h-4 w-4" />
        续接自任务
      </Link>
      <div aria-hidden="true" className="h-px flex-1 bg-border/70" />
    </div>
  );
}

/**
 * Lightbox 打开回调上下文。
 *
 * 真源放在 `MessageList` 顶层（一份 state），通过 context 注入到深层
 * `MessageContent` user 分支；避免每条消息独立 useState 或一路 prop drilling。
 * Provider 缺失时回退为 noop，方便在 Storybook / 单测里渲染 `ChatMessageItem`。
 */
const LightboxContext = createContext<(src: string, alt?: string) => void>(
  () => {},
);

interface MessageViewportProps<T> {
  items: T[];
  emptyText: string;
  renderItem: (item: T, index: number) => ReactNode;
  getUserMessageCount?: (items: T[]) => number;
  resetKey?: string | number;
}

export function MessageViewport<T>({
  items,
  emptyText,
  renderItem,
  getUserMessageCount,
  resetKey,
}: MessageViewportProps<T>) {
  const countUsers = useCallback(
    (list: T[]) =>
      getUserMessageCount ? getUserMessageCount(list) : 0,
    [getUserMessageCount],
  );
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const isNearBottomRef = useRef(true);
  const prevUserMsgCount = useRef(0);
  const [showScrollBtn, setShowScrollBtn] = useState(false);

  const checkNearBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    const near = dist < NEAR_BOTTOM_PX;
    isNearBottomRef.current = near;
    setShowScrollBtn(!near);
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;

    const userMsgCount = countUsers(items);

    if (userMsgCount > prevUserMsgCount.current) {
      el.scrollTop = el.scrollHeight;
      isNearBottomRef.current = true;
      setShowScrollBtn(false);
    } else if (isNearBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    }

    prevUserMsgCount.current = userMsgCount;
  }, [countUsers, items]);

  useEffect(() => {
    prevUserMsgCount.current = 0;
    isNearBottomRef.current = true;
    setShowScrollBtn(false);
  }, [resetKey]);

  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    isNearBottomRef.current = true;
    setShowScrollBtn(false);
  }, []);

  if (items.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <div className="obsidian-panel-soft max-w-md rounded-[1.6rem] px-6 py-8 text-center text-sm text-muted-foreground">
          {emptyText}
        </div>
      </div>
    );
  }

  return (
    <div className="relative h-full">
      <div
        ref={scrollRef}
        onScroll={checkNearBottom}
        className="h-full overflow-y-auto scrollbar-overlay"
      >
        <div data-testid="message-viewport-content" className="w-full p-4">
          <div className="flex flex-col gap-4">
            {items.map((item, index) => renderItem(item, index))}
          </div>
        </div>
      </div>
      {showScrollBtn && (
        <button
          type="button"
          onClick={scrollToBottom}
          className="absolute bottom-4 left-1/2 flex -translate-x-1/2 items-center gap-1 rounded-full border border-border/80 bg-card/90 px-3 py-1.5 text-xs text-muted-foreground shadow-glass backdrop-blur-xl transition-colors hover:bg-secondary"
          aria-label="回到底部"
        >
          <ChevronDown className="h-3.5 w-3.5" />
          回到最新
        </button>
      )}
    </div>
  );
}

export function MessageList({
  threadId,
  items: injectedItems,
  timezone,
  onForkAssistant,
  forkingHistoryIndex,
  forkLineage = null,
}: {
  threadId: string | undefined;
  /**
   * chat-receive-side-unify #5：可选注入的渲染清单。
   *
   * 传了就用注入的（generic 频道走 ChatTimelineStore→adapter 投影），
   * 没传退回 `useChatStore` 读取（claude / codex 等其它调用方现状不变）。
   * 注入态下 useChatStore selector 仍订阅但返回稳定 EMPTY_ITEMS 引用（threadId
   * 在注入侧的 itemsByThread 不再写入），不会引入额外重渲染。
   */
  items?: ChatItem[];
  timezone?: string;
  onForkAssistant?: (historyIndex: number) => void;
  forkingHistoryIndex?: number | null;
  forkLineage?: ForkLineage | null;
}) {
  const storeItems = useChatStore((s) =>
    threadId ? (s.itemsByThread[threadId] ?? EMPTY_ITEMS) : EMPTY_ITEMS,
  );
  const items = injectedItems ?? storeItems;
  const [lightbox, setLightbox] = useState<{ src: string; alt?: string } | null>(
    null,
  );
  const openLightbox = useCallback((src: string, alt?: string) => {
    setLightbox({ src, alt });
  }, []);
  const closeLightbox = useCallback(() => setLightbox(null), []);

  // 通用 hook：按 user 消息分界插入文件汇总
  const renderItems = useItemsWithFileSummary(items, threadId, (it) => it.kind === "user");

  if (!threadId) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <div className="obsidian-panel-soft max-w-md rounded-[1.6rem] px-6 py-8 text-center text-sm text-muted-foreground">
          在左侧选择或创建一个 thread
        </div>
      </div>
    );
  }

  return (
    <LightboxContext.Provider value={openLightbox}>
      <MessageViewport
        items={renderItems}
        emptyText="说点什么开始"
        resetKey={threadId}
        getUserMessageCount={(list) =>
          list.filter((it) => it.kind === "user").length
        }
        renderItem={(item) => (
          <Fragment key={item.id}>
            {item.kind === "files-summary" ? (
              <ModifiedFilesSummary
                files={item.files}
                threadId={item.threadId}
              />
            ) : (
              <ChatMessageItem
                item={item}
                timezone={timezone}
                onForkAssistant={onForkAssistant}
                forkingHistoryIndex={forkingHistoryIndex}
              />
            )}
            {forkLineage !== null &&
            item.kind === "assistant" &&
            item.forkHistoryIndex === forkLineage.historyIndex ? (
              <ForkLineageNavigation {...forkLineage} />
            ) : null}
          </Fragment>
        )}
      />
      <ImageLightbox
        src={lightbox?.src ?? null}
        alt={lightbox?.alt}
        onClose={closeLightbox}
      />
    </LightboxContext.Provider>
  );
}

/**
 * Debug 包装：debug 开启时给每条 ChatItem 角上贴 monospace 小 badge，
 * 内容 `${id前8} │ ${runId后6} │ t${turn}`。仅 debug 模式下渲染，避免污染正常视图。
 *
 * 用途：肉眼验证 (threadId, runId, turn) 复合 key 是否正确生成；修非流式
 * 多轮覆盖 bug 后的快速回归手段。
 */
export function ChatMessageItem({
  item,
  timezone,
  onForkAssistant,
  forkingHistoryIndex,
}: {
  item: ChatItem;
  timezone?: string;
  onForkAssistant?: (historyIndex: number) => void;
  forkingHistoryIndex?: number | null;
}) {
  // 同步读一次 debug 开关；不响应式，避免不必要的 re-render
  const debug = useMemo(() => isDebugMode(), []);
  if (!debug) {
    return (
      <MessageContent
        item={item}
        timezone={timezone}
        onForkAssistant={onForkAssistant}
        forkingHistoryIndex={forkingHistoryIndex}
      />
    );
  }

  const turn =
    "turn" in item ? `t${item.turn}` : "—";
  const runId =
    "runId" in item && item.runId ? item.runId.slice(-6) : "—";
  const idHead = item.id.slice(0, 8);

  return (
    <div className="relative" data-testid="debug-badge-wrap">
      <MessageContent
        item={item}
        timezone={timezone}
        onForkAssistant={onForkAssistant}
        forkingHistoryIndex={forkingHistoryIndex}
      />
      <div
        data-testid="debug-badge"
        className="pointer-events-none absolute right-0 top-0 z-10 rounded-bl-md rounded-tr-md border border-border bg-background/90 px-1.5 py-0.5 font-mono text-[10px] leading-tight text-muted-foreground shadow-sm backdrop-blur-sm"
      >
        {idHead} │ {runId} │ {turn}
      </div>
    </div>
  );
}

function MessageContent({
  item,
  timezone,
  onForkAssistant,
  forkingHistoryIndex,
}: {
  item: ChatItem;
  timezone?: string;
  onForkAssistant?: (historyIndex: number) => void;
  forkingHistoryIndex?: number | null;
}) {
  switch (item.kind) {
    case "user":
      return (
        <div className="flex items-end justify-end gap-2">
          <MessageBubbleFrame
            content={item.content}
            timestampMs={item.timestampMs}
            timezone={timezone}
            align="right"
            bubbleClassName="rounded-[1.4rem] rounded-br-md border border-primary/20 bg-primary px-4 py-3 text-sm text-primary-foreground shadow-sm"
          >
            {item.attachments && item.attachments.length > 0 ? (
              <UserAttachmentThumbnails attachments={item.attachments} />
            ) : null}
            {item.references && item.references.length > 0 ? (
              <UserReferenceChips references={item.references} />
            ) : null}
            {item.content ? (
              <Markdown text={item.content} className="leading-relaxed" />
            ) : null}
            {item.deliveryStatus === "steered" ? (
              <div
                data-testid="user-message-delivery-status"
                className="mt-2 inline-flex items-center gap-1.5 text-[11px] font-medium text-primary-foreground/75"
              >
                <CheckCircle2 className="h-3 w-3" />
                <span>已插队</span>
              </div>
            ) : null}
          </MessageBubbleFrame>
          <ChatAvatar role="user" />
        </div>
      );
    case "assistant": {
      // 空 assistant 帧（只发 tool_calls 无文本）不渲染头像+气泡
      if (!item.content && !item.reasoning && !item.usage) return null;
      return (
        <div className="flex items-start gap-2">
          <ChatAvatar role="assistant" />
          <div className="min-w-0 flex-1">
            <AssistantMessage
              item={item}
              timezone={timezone}
              onForkAssistant={onForkAssistant}
              forkingHistoryIndex={forkingHistoryIndex}
            />
          </div>
        </div>
      );
    }
    case "tool":
      return <ToolCard item={item} />;
    case "system":
      return <SystemNoticeCard item={item} />;
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

function MessageBubbleFrame({
  children,
  content,
  timestampMs,
  timezone,
  align,
  bubbleClassName,
  footerAction,
}: {
  children: ReactNode;
  content: string;
  timestampMs: number;
  timezone?: string;
  align: "left" | "right";
  bubbleClassName: string;
  footerAction?: ReactNode;
}) {
  const formattedTime = useMemo(
    () => formatMessageTime(timestampMs, timezone),
    [timestampMs, timezone],
  );
  const copyMessage = useCallback(() => {
    if (!content) return;
    void navigator.clipboard?.writeText(content);
  }, [content]);
  const isRight = align === "right";

  return (
    <div
      data-testid="message-bubble-frame"
      className={cn(
        "group flex w-full min-w-0 flex-col gap-1",
        isRight ? "items-end" : "items-start",
      )}
    >
      <div
        data-testid="message-bubble"
        className={cn("w-fit max-w-full", bubbleClassName)}
      >
        {children}
      </div>
      <div
        data-testid="message-hover-meta"
        className={cn(
          "flex h-5 items-center gap-2 text-[11px] text-muted-foreground opacity-0 transition-opacity group-focus-within:opacity-100 group-hover:opacity-100",
          isRight ? "justify-end pr-2" : "justify-start pl-2",
        )}
      >
        {content ? (
          <button
            type="button"
            onClick={copyMessage}
            className="inline-flex h-5 w-5 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            aria-label="复制消息"
            title="复制"
          >
            <Copy className="h-3.5 w-3.5" />
          </button>
        ) : null}
        {footerAction}
        <span>{formattedTime}</span>
      </div>
    </div>
  );
}

function normalizeMessageTimezone(timezone: string | undefined): string {
  if (!timezone) return "UTC";
  try {
    new Intl.DateTimeFormat("zh-CN", { timeZone: timezone }).format(0);
    return timezone;
  } catch {
    return "UTC";
  }
}

function formatMessageTime(timestampMs: number, timezone: string | undefined): string {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: normalizeMessageTimezone(timezone),
  }).format(new Date(timestampMs));
}

function UserReferenceChips({
  references,
}: {
  references: ConversationReferenceDTO[];
}) {
  if (references.length === 0) return null;
  return (
    <div
      data-testid="message-reference-strip"
      className="mb-2 flex flex-wrap gap-1.5"
    >
      {references.map((reference) => (
        <span
          key={reference.id}
          data-testid="message-reference-chip"
          className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-primary-foreground/25 bg-primary-foreground/10 px-2 py-1 text-xs font-medium text-primary-foreground"
          title={`${reference.label} - ${reference.ref}`}
        >
          <Sparkles className="h-3.5 w-3.5 shrink-0" />
          <span className="max-w-[12rem] truncate">{reference.label}</span>
          <button
            type="button"
            className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded text-primary-foreground/75 hover:bg-primary-foreground/15 hover:text-primary-foreground"
            onClick={() => copyReference(reference)}
            aria-label={`复制引用 ${reference.label}`}
            title="复制引用"
          >
            <Copy className="h-3 w-3" />
          </button>
        </span>
      ))}
    </div>
  );
}

function copyReference(reference: ConversationReferenceDTO): void {
  const clipboard = navigator.clipboard;
  if (!clipboard) return;
  void clipboard
    .writeText(ConversationReferenceManager.toClipboardText(reference))
    .catch(() => undefined);
}

/**
 * 历史用户消息内的附件缩略图（仅 image kind）。
 *
 * 与 Composer 的 `ThumbnailStrip` 不同：
 * - 数据源是 `UserInputAttachment.preview_url`（远端 /api/uploads/{asset_id}），
 *   不是 object URL；刷新后仍可恢复
 * - 点击图片调用 `LightboxContext` 打开全屏预览，避免每条消息独立 lightbox state
 */
function UserAttachmentThumbnails({
  attachments,
}: {
  attachments: UserInputAttachment[];
}) {
  const openLightbox = useContext(LightboxContext);
  // Phase 1 仅 image；其他 kind 暂时跳过（防御未来 union 扩展）
  const images = attachments.filter((a) => a.kind === "image");
  if (images.length === 0) return null;
  return (
    <div
      data-testid="message-attachment-strip"
      className="mb-2 flex flex-wrap gap-2"
    >
      {images.map((att) => (
        <button
          key={att.asset_id}
          type="button"
          data-testid="message-attachment-thumb"
          data-asset-id={att.asset_id}
          onClick={() => openLightbox(att.preview_url, att.mime_type)}
          className="block overflow-hidden rounded-lg border border-primary-foreground/20 bg-primary-foreground/5 transition-transform hover:scale-[1.02] focus:outline-none focus:ring-2 focus:ring-primary-foreground/50"
        >
          <img
            src={att.preview_url}
            alt={att.mime_type}
            loading="lazy"
            className="h-[200px] max-h-[200px] w-[200px] max-w-[200px] object-cover"
            onError={(e) => {
              // 历史消息图片 404 / 网络失败兜底：缩略图变灰 + data-error 标记。
              // 不加 retry 按钮（Phase 2），只视觉提示用户"这张图加载失败"，避免
              // 默认的破损 alt 图标看着像 bug。
              const img = e.currentTarget as HTMLImageElement;
              img.style.opacity = "0.3";
              img.setAttribute("data-error", "true");
            }}
          />
        </button>
      ))}
    </div>
  );
}

const AssistantMessage = memo(function AssistantMessage({
  item,
  timezone,
  onForkAssistant,
  forkingHistoryIndex,
}: {
  item: Extract<ChatItem, { kind: "assistant" }>;
  timezone?: string;
  onForkAssistant?: (historyIndex: number) => void;
  forkingHistoryIndex?: number | null;
}) {
  const [reasoningOpen, setReasoningOpen] = useState(false);
  // v0.1.6：assistant 只发 tool_calls 时 content 为空字符串（语义"无文本输出"）。
  // 既无 content、又没 reasoning/usage 时整个气泡不渲染，避免正文到来前出现空白框。
  const hasContent = Boolean(item.content);
  const hasReasoning = Boolean(item.reasoning);
  const hasUsage = Boolean(item.usage);
  if (!hasContent && !hasReasoning && !hasUsage) return null;
  return (
    <div className="flex flex-col gap-2">
      {hasReasoning ? (
        <button
          type="button"
          onClick={() => setReasoningOpen((v) => !v)}
          className="inline-flex w-fit items-center gap-1 rounded-xl border border-border/70 bg-card/62 px-2.5 py-1.5 text-xs text-muted-foreground shadow-sm hover:bg-secondary"
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
        <pre className="whitespace-pre-wrap rounded-2xl border border-border/70 bg-muted/72 p-3 text-xs text-muted-foreground">
          {item.reasoning}
        </pre>
      ) : null}
      {hasContent ? (
        <div className="w-full">
          <MessageBubbleFrame
            content={item.content}
            timestampMs={item.timestampMs}
            timezone={timezone}
            align="left"
            bubbleClassName="obsidian-panel-soft rounded-[1.45rem] rounded-bl-md px-4 py-3 text-sm"
            footerAction={
              typeof item.forkHistoryIndex === "number" &&
              !item.streaming &&
              onForkAssistant ? (
                <button
                  type="button"
                  onClick={() => onForkAssistant(item.forkHistoryIndex!)}
                  disabled={forkingHistoryIndex != null}
                  className="inline-flex h-5 w-5 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-wait disabled:opacity-50"
                  aria-label="从此回复分叉"
                  title="从此回复分叉"
                >
                  {forkingHistoryIndex === item.forkHistoryIndex ? (
                    <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <GitFork className="h-3.5 w-3.5" />
                  )}
                </button>
              ) : null
            }
          >
            {item.streaming ? (
              <div data-testid="streaming-assistant-text" className="whitespace-pre-wrap leading-relaxed">
                {item.content}
                <span
                  aria-label="streaming"
                  className="ml-1 inline-block h-4 w-1 animate-pulse bg-accent align-middle"
                />
              </div>
            ) : (
              <Markdown text={item.content} />
            )}
          </MessageBubbleFrame>
        </div>
      ) : null}
      {hasUsage ? (
        <div
          className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 pl-2 text-[11px] text-muted-foreground"
          data-testid="assistant-usage-footer"
        >
          <span className="inline-flex items-center gap-1">
            <ArrowUp className="h-3 w-3" />
            {fmtCompact(item.usage!.prompt)}
          </span>
          <span className="inline-flex items-center gap-1">
            <ArrowDown className="h-3 w-3" />
            {fmtCompact(item.usage!.completion)}
          </span>
          <span className="inline-flex items-center gap-1">
            <Sigma className="h-3 w-3" />
            {fmtCompact(item.usage!.total)}
          </span>
        </div>
      ) : null}
    </div>
  );
}, assistantMessageEqual);

function assistantMessageEqual(
  previous: {
    item: Extract<ChatItem, { kind: "assistant" }>;
    timezone?: string;
    onForkAssistant?: (historyIndex: number) => void;
    forkingHistoryIndex?: number | null;
  },
  next: {
    item: Extract<ChatItem, { kind: "assistant" }>;
    timezone?: string;
    onForkAssistant?: (historyIndex: number) => void;
    forkingHistoryIndex?: number | null;
  },
): boolean {
  const previousItem = previous.item;
  const nextItem = next.item;
  return (
    previous.timezone === next.timezone &&
    previous.onForkAssistant === next.onForkAssistant &&
    previous.forkingHistoryIndex === next.forkingHistoryIndex &&
    previousItem.id === nextItem.id &&
    previousItem.content === nextItem.content &&
    previousItem.reasoning === nextItem.reasoning &&
    previousItem.streaming === nextItem.streaming &&
    previousItem.forkHistoryIndex === nextItem.forkHistoryIndex &&
    previousItem.timestampMs === nextItem.timestampMs &&
    previousItem.usage?.prompt === nextItem.usage?.prompt &&
    previousItem.usage?.completion === nextItem.usage?.completion &&
    previousItem.usage?.total === nextItem.usage?.total
  );
}

function fmtCompact(value: number): string {
  if (!Number.isFinite(value)) return "0";
  if (Math.abs(value) >= 1000) {
    return `${(value / 1000).toFixed(1)}K`;
  }
  return String(value);
}

function ToolCard({
  item,
}: {
  item: Extract<ChatItem, { kind: "tool" }>;
}) {
  const [open, setOpen] = useState(false);
  const openFileDrawer = useWorkspaceStore((s) => s.openFileDrawer);
  const workspaceRoot = useWorkspaceStore((s) =>
    item.threadId ? s.contextsByThread[item.threadId]?.workspace_root : undefined
  );
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
  const canViewFile =
    item.toolName === "write_file" &&
    item.ok === true &&
    typeof item.resultData?.path === "string";
  return (
    <div className="obsidian-panel-soft rounded-[1.3rem] px-4 py-3 text-xs">
      <div className="flex w-full items-center gap-2">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="flex items-center gap-2"
        >
          {open ? (
            <ChevronDown className="h-3 w-3" />
          ) : (
            <ChevronRight className="h-3 w-3" />
          )}
          <span className="font-mono font-semibold">{item.toolName}</span>
        </button>
        {canViewFile ? (
          <button
            type="button"
            className="ml-auto flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-primary hover:bg-accent hover:text-accent-foreground transition-colors"
            onClick={() => {
              const absPath = item.resultData!.path as string;
              openFileDrawer(item.threadId, absPath, workspaceRoot ?? "");
            }}
          >
            <FileText className="h-3 w-3" />
            查看文件
          </button>
        ) : null}
        <Badge variant={variant} className={cn(canViewFile ? "ml-2" : "ml-auto")}>
          {status}
        </Badge>
      </div>
      {open ? (
        <div className="mt-2 space-y-2">
          {/* 入参（pending 模式显示 partialInput；正式模式显示 arguments） */}
          {item.pending ? (
            <div>
              <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
                构建参数中…
              </div>
              <pre
                data-testid="tool-partial-input"
                className="overflow-x-auto rounded-xl bg-muted/72 p-2.5 font-mono text-[11px]"
              >
                {item.partialInput || "(尚无内容)"}
              </pre>
            </div>
          ) : (
            <div>
              <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
                arguments
              </div>
              <pre className="overflow-x-auto rounded-xl bg-muted/72 p-2.5 font-mono text-[11px]">
                {JSON.stringify(item.arguments, null, 2)}
              </pre>
            </div>
          )}
          {/* 结果文本（content）；只有 tool.call.end 后才会出现 */}
          {item.result ? (
            <div>
              <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
                result
              </div>
              <pre className="max-h-80 overflow-auto rounded-xl bg-muted/72 p-2.5 font-mono text-[11px]">
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
              <pre className="max-h-60 overflow-auto rounded-xl bg-muted/72 p-2.5 font-mono text-[11px]">
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

function SystemNoticeCard({
  item,
}: {
  item: Extract<ChatItem, { kind: "system" }>;
}) {
  const [open, setOpen] = useState(false);
  const applyEvolutionReview = useChatStore((s) => s.applyEvolutionReview);
  const Icon = iconForSystemNotice(item.icon, item.status);
  const cardTone = toneForSystemNotice(item.status);
  const reviewId =
    item.detailsData &&
    !Array.isArray(item.detailsData) &&
    typeof item.detailsData.review_id === "string"
      ? item.detailsData.review_id
      : null;
  const canDecide =
    item.source === "self_evolution" &&
    item.status === "success" &&
    Boolean(reviewId);
  const applyStats = evolutionApplyStatsFromNotice(item);

  return (
    <>
      <div className="flex justify-center" data-testid="system-notice-wrap">
        <div
          className={cn(
            "w-full max-w-2xl rounded-[1.5rem] border px-4 py-3 shadow-glass",
            cardTone.container,
          )}
          data-testid="system-notice-card"
        >
          <div className="flex items-start gap-3">
            <div
              className={cn(
                "mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border",
                cardTone.iconWrap,
              )}
            >
              <Icon
                className={cn(
                  "h-4 w-4",
                  cardTone.icon,
                  item.status === "running" ? "animate-spin" : "",
                )}
              />
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <div className="text-sm font-semibold tracking-tight">{item.title}</div>
                  <Badge variant={badgeVariantForSystemNotice(item.status)}>
                    {labelForSystemNotice(item.status)}
                  </Badge>
                </div>
                {canDecide ? (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => setOpen(true)}
                    data-testid="evolution-open-decision"
                    className="shrink-0"
                  >
                    查看并处理
                  </Button>
                ) : null}
              </div>
              <div className="mt-1 text-sm leading-relaxed text-foreground/90">
                {item.message}
              </div>
              {applyStats.length > 0 ? (
                <div className="mt-3 flex flex-wrap gap-2">
                  {applyStats.map((stat) => (
                    <Badge key={stat.label} variant={stat.variant}>
                      {stat.label}
                    </Badge>
                  ))}
                </div>
              ) : null}
              {item.details.length > 0 ? (
                <div className="mt-3 space-y-2">
                  {item.details.map((detail, index) => (
                    <div
                      key={`${item.noticeKey}-${index}`}
                      className="rounded-xl border border-border/60 bg-background/40 px-3 py-2 text-xs leading-relaxed text-muted-foreground"
                    >
                      {detail}
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          </div>
        </div>
      </div>
      {canDecide && reviewId ? (
        <EvolutionDecisionModal
          open={open}
          onOpenChange={setOpen}
          threadId={item.threadId}
          reviewId={reviewId}
          onReviewUpdated={(review) => applyEvolutionReview(item.threadId, review)}
        />
      ) : null}
    </>
  );
}

function evolutionApplyStatsFromNotice(
  item: Extract<ChatItem, { kind: "system" }>,
): Array<{
  label: string;
  variant: "secondary" | "success" | "warning" | "destructive";
}> {
  if (
    item.source !== "self_evolution" ||
    !item.detailsData ||
    Array.isArray(item.detailsData)
  ) {
    return [];
  }

  const written = numberFromDetails(item.detailsData.applied_written_count);
  const skipped = numberFromDetails(item.detailsData.applied_skipped_count);
  const failed = numberFromDetails(item.detailsData.applied_failed_count);
  const pending = numberFromDetails(item.detailsData.applied_pending_count);

  const stats: Array<{
    label: string;
    variant: "secondary" | "success" | "warning" | "destructive";
  }> = [];

  if (written > 0) {
    stats.push({ label: `已写入 ${written}`, variant: "success" });
  }
  if (skipped > 0) {
    stats.push({ label: `已命中 ${skipped}`, variant: "warning" });
  }
  if (failed > 0) {
    stats.push({ label: `失败 ${failed}`, variant: "destructive" });
  }
  if (pending > 0) {
    stats.push({ label: `待写入 ${pending}`, variant: "secondary" });
  }

  return stats;
}

function numberFromDetails(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function labelForSystemNotice(
  status: Extract<ChatItem, { kind: "system" }>["status"],
): string {
  switch (status) {
    case "running":
      return "复盘中";
    case "success":
      return "已沉淀";
    case "warning":
      return "超时";
    case "error":
      return "未写入";
    default: {
      const _exhaustive: never = status;
      return _exhaustive;
    }
  }
}

function badgeVariantForSystemNotice(
  status: Extract<ChatItem, { kind: "system" }>["status"],
): "secondary" | "success" | "warning" | "destructive" {
  switch (status) {
    case "running":
      return "secondary";
    case "success":
      return "success";
    case "warning":
      return "warning";
    case "error":
      return "destructive";
    default: {
      const _exhaustive: never = status;
      return _exhaustive;
    }
  }
}

function iconForSystemNotice(
  icon: Extract<ChatItem, { kind: "system" }>["icon"],
  status: Extract<ChatItem, { kind: "system" }>["status"],
) {
  switch (icon) {
    case "success":
      return CheckCircle2;
    case "warning":
      return AlertTriangle;
    case "error":
      return XCircle;
    case "running":
      return LoaderCircle;
    default:
      return status === "success"
        ? CheckCircle2
        : status === "warning"
          ? AlertTriangle
          : status === "error"
            ? XCircle
            : LoaderCircle;
  }
}

function toneForSystemNotice(
  status: Extract<ChatItem, { kind: "system" }>["status"],
): {
  container: string;
  iconWrap: string;
  icon: string;
} {
  switch (status) {
    case "running":
      return {
        container: "border-border/70 bg-card/70 text-foreground",
        iconWrap: "border-border/70 bg-muted/70",
        icon: "text-muted-foreground",
      };
    case "success":
      return {
        container: "border-success/25 bg-success/10 text-foreground",
        iconWrap: "border-success/30 bg-success/15",
        icon: "text-success",
      };
    case "warning":
      return {
        container: "border-warning/25 bg-warning/10 text-foreground",
        iconWrap: "border-warning/30 bg-warning/15",
        icon: "text-warning-foreground",
      };
    case "error":
      return {
        container: "border-destructive/25 bg-destructive/10 text-foreground",
        iconWrap: "border-destructive/30 bg-destructive/15",
        icon: "text-destructive",
      };
    default: {
      const _exhaustive: never = status;
      return _exhaustive;
    }
  }
}
