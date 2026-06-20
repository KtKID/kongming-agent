import {
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  Circle,
  History,
  ListChecks,
  LoaderCircle,
  RefreshCw,
  X,
  XCircle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { useThreadTaskProgress } from "@/hooks/useThreadTaskProgress";
import { useThreadSubAgents } from "@/hooks/useThreadSubAgents";
import {
  type ThreadWorkflowHistoryItem,
  useThreadWorkflowHistory,
} from "@/hooks/useThreadWorkflowHistory";
import { cn } from "@/lib/utils";
import type {
  ThreadSubAgentDisplayItem,
  ThreadTaskProgressDisplayItem,
  ThreadTaskProgressSnapshot,
} from "@/protocol";

type TriggerRenderProps = {
  open: boolean;
  disabled: boolean;
  onClick: () => void;
};

interface ThreadTaskProgressPopoverProps {
  threadId?: string;
  trigger?: (props: TriggerRenderProps) => ReactNode;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  className?: string;
  panelClassName?: string;
  mobileMode?: boolean;
}

export function ThreadTaskProgressPopover({
  threadId,
  trigger,
  open,
  onOpenChange,
  className,
  panelClassName,
  mobileMode = false,
}: ThreadTaskProgressPopoverProps) {
  const [internalOpen, setInternalOpen] = useState(false);
  const actualOpen = open ?? internalOpen;
  const disabled = !threadId;
  const { viewModel, snapshot, isLoading, error, refresh } =
    useThreadTaskProgress(threadId, { enabled: Boolean(threadId) });
  const {
    items: subAgentItems,
    isLoading: isSubAgentsLoading,
    error: subAgentsError,
    refresh: refreshSubAgents,
  } = useThreadSubAgents(threadId, {
    enabled: Boolean(threadId),
  });
  const {
    items: historyItems,
    isLoading: isHistoryLoading,
    error: historyError,
    refresh: refreshHistory,
  } = useThreadWorkflowHistory(threadId, {
    enabled: Boolean(threadId) && actualOpen,
  });
  const lastThreadIdRef = useRef<string | undefined>(undefined);
  const lastProgressSignatureRef = useRef<string | null>(null);
  const lastSubAgentSignatureRef = useRef<string | null>(null);
  const progressSignature = snapshot ? taskProgressSignature(snapshot) : null;
  const subAgentSignature = threadSubAgentsSignature(subAgentItems);
  const hasActiveSubAgents = subAgentItems.some((item) => item.is_active);
  const currentWorkflowIdList = (snapshot?.tasks ?? [])
    .map((item) => item.workflow_id)
    .filter((workflowId): workflowId is string => Boolean(workflowId));
  const currentWorkflowIds = new Set(currentWorkflowIdList);
  const currentWorkflowTitle =
    historyItems.find((item) => item.workflow_id === currentWorkflowIdList[0])
      ?.title ?? null;
  const progressCountLabel = snapshot
    ? `${snapshot.counts.completed}/${snapshot.counts.total} 已完成`
    : "当前 thread";
  const currentTaskLabel = currentWorkflowTitle
    ? `${currentWorkflowTitle} · ${progressCountLabel}`
    : progressCountLabel;
  const visibleHistoryItems = historyItems.filter(
    (item) => !currentWorkflowIds.has(item.workflow_id),
  );

  const setOpen = useCallback(
    (nextOpen: boolean) => {
      if (open === undefined) setInternalOpen(nextOpen);
      onOpenChange?.(nextOpen);
    },
    [onOpenChange, open],
  );

  useEffect(() => {
    if (!threadId) {
      lastThreadIdRef.current = undefined;
      lastProgressSignatureRef.current = null;
      lastSubAgentSignatureRef.current = null;
      return;
    }

    if (lastThreadIdRef.current !== threadId) {
      lastThreadIdRef.current = threadId;
      lastProgressSignatureRef.current = progressSignature;
      lastSubAgentSignatureRef.current = subAgentSignature;
      if (
        (snapshot && shouldAutoOpenInitialSnapshot(snapshot)) ||
        hasActiveSubAgents
      ) {
        setOpen(true);
      }
      return;
    }

    const previousSignature = lastProgressSignatureRef.current;
    const previousSubAgentSignature = lastSubAgentSignatureRef.current;
    lastProgressSignatureRef.current = progressSignature;
    lastSubAgentSignatureRef.current = subAgentSignature;
    if (
      progressSignature &&
      previousSignature !== progressSignature &&
      shouldAutoOpenSnapshot(snapshot)
    ) {
      setOpen(true);
    }
    if (
      subAgentSignature &&
      previousSubAgentSignature !== subAgentSignature &&
      hasActiveSubAgents
    ) {
      setOpen(true);
    }
  }, [
    hasActiveSubAgents,
    progressSignature,
    setOpen,
    snapshot,
    subAgentSignature,
    threadId,
  ]);

  const defaultTrigger = (
    <Button
      type="button"
      variant="outline"
      size="sm"
      disabled={disabled}
      aria-expanded={actualOpen}
      aria-haspopup="dialog"
      onClick={() => setOpen(!actualOpen)}
      className={cn("gap-1.5", className)}
    >
      <ListChecks className="h-3.5 w-3.5" />
      进度
    </Button>
  );

  const triggerNode = trigger
    ? trigger({
        open: actualOpen,
        disabled,
        onClick: () => setOpen(!actualOpen),
      })
    : defaultTrigger;
  const panelNode = actualOpen ? (
    <>
      <button
        type="button"
        aria-label="关闭进度面板"
        className="fixed inset-0 z-40 cursor-default bg-transparent"
        onClick={() => setOpen(false)}
      />
      <section
        role="dialog"
        aria-label="当前 thread 任务进度"
        className={cn(
          "z-50 w-[min(22rem,calc(100vw-1.5rem))] rounded-lg border border-border/80 bg-popover/95 p-2 text-popover-foreground shadow-2xl backdrop-blur-xl",
          mobileMode
            ? "fixed left-2 right-2 top-14"
            : "absolute right-0 top-[calc(100%+0.5rem)]",
          panelClassName,
        )}
      >
        <div className="flex min-h-8 items-center justify-between gap-2 px-1 pb-2">
          <div className="min-w-0 text-sm font-semibold text-foreground">
            环境信息
          </div>
          <div className="flex items-center gap-0.5">
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label="刷新任务进度"
              className="h-7 w-7 rounded-md"
              disabled={
                disabled || isLoading || isHistoryLoading || isSubAgentsLoading
              }
              onClick={() => {
                void refresh();
                void refreshSubAgents();
                void refreshHistory();
              }}
            >
              <RefreshCw
                className={cn(
                  "h-3.5 w-3.5",
                  (isLoading || isHistoryLoading || isSubAgentsLoading) &&
                    "animate-spin",
                )}
              />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label="关闭"
              className="h-7 w-7 rounded-md"
              onClick={() => setOpen(false)}
            >
              <X className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>

        <div className="h-px bg-border/70" />

        <div className="space-y-1.5 pt-2">
          <div className="flex items-center justify-between gap-2 px-1">
            <div className="flex min-w-0 items-center gap-1.5 text-xs font-medium text-foreground">
              <ListChecks className="h-3.5 w-3.5 shrink-0 text-primary" />
              <span>长任务</span>
            </div>
            <div
              className="min-w-0 max-w-[12rem] shrink truncate text-right text-[11px] leading-3 text-muted-foreground"
              title={currentTaskLabel}
            >
              {currentTaskLabel}
            </div>
          </div>

          {error ? (
            <div
              role="alert"
              className="flex gap-2 rounded-lg border border-destructive/25 bg-destructive/10 px-2.5 py-1.5 text-xs text-destructive"
            >
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span className="min-w-0 break-words">
                读取任务进度失败：{error}
              </span>
            </div>
          ) : null}

          {isLoading && !snapshot ? (
            <div className="flex items-center gap-2 rounded-lg border border-border/70 bg-card/70 px-2.5 py-2 text-xs text-muted-foreground">
              <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
              正在读取任务进度
            </div>
          ) : viewModel.items.length === 0 && !error ? (
            <div className="rounded-lg border border-dashed border-border/80 bg-card/45 px-2.5 py-3 text-sm">
              <div className="font-medium text-foreground">
                {viewModel.empty.title}
              </div>
              {viewModel.empty.desc ? (
                <div className="mt-1 text-xs text-muted-foreground">
                  {viewModel.empty.desc}
                </div>
              ) : null}
            </div>
          ) : null}

          {viewModel.items.length > 0 ? (
            <ul className="max-h-[18rem] space-y-0.5 overflow-y-auto pr-0.5">
              {viewModel.items.map((item) => (
                <TaskProgressRow key={item.key} item={item} />
              ))}
            </ul>
          ) : null}
        </div>

        <div className="my-2 h-px bg-border/70" />

        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2 px-1">
            <div className="flex min-w-0 items-center gap-1.5 text-xs font-medium text-foreground">
              <Bot className="h-3.5 w-3.5 shrink-0 text-primary" />
              <span>子智能体</span>
            </div>
            <div className="shrink-0 text-[11px] leading-3 text-muted-foreground">
              {subAgentItems.length} 个
            </div>
          </div>

          {subAgentsError ? (
            <div
              role="alert"
              className="flex gap-2 rounded-lg border border-destructive/25 bg-destructive/10 px-2.5 py-1.5 text-xs text-destructive"
            >
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span className="min-w-0 break-words">
                读取子智能体失败：{subAgentsError}
              </span>
            </div>
          ) : null}

          {isSubAgentsLoading && subAgentItems.length === 0 ? (
            <div className="flex items-center gap-2 rounded-lg border border-border/70 bg-card/70 px-2.5 py-2 text-xs text-muted-foreground">
              <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
              正在读取子智能体
            </div>
          ) : subAgentItems.length === 0 && !subAgentsError ? (
            <div className="px-2.5 py-1.5 text-xs text-muted-foreground">
              暂无子智能体
            </div>
          ) : null}

          {subAgentItems.length > 0 ? (
            <ul className="max-h-[10rem] space-y-0.5 overflow-y-auto pr-0.5">
              {subAgentItems.map((item) => (
                <ThreadSubAgentRow key={item.key} item={item} />
              ))}
            </ul>
          ) : null}
        </div>

        <div className="my-2 h-px bg-border/70" />

        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2 px-1">
            <div className="flex min-w-0 items-center gap-1.5 text-xs font-medium text-foreground">
              <History className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              <span>历史任务</span>
            </div>
            <div className="shrink-0 text-[11px] leading-3 text-muted-foreground">
              {visibleHistoryItems.length} 个
            </div>
          </div>

          {historyError ? (
            <div
              role="alert"
              className="flex gap-2 rounded-lg border border-destructive/25 bg-destructive/10 px-2.5 py-1.5 text-xs text-destructive"
            >
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span className="min-w-0 break-words">
                读取历史任务失败：{historyError}
              </span>
            </div>
          ) : null}

          {isHistoryLoading && historyItems.length === 0 ? (
            <div className="flex items-center gap-2 rounded-lg border border-border/70 bg-card/70 px-2.5 py-2 text-xs text-muted-foreground">
              <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
              正在读取历史任务
            </div>
          ) : visibleHistoryItems.length === 0 && !historyError ? (
            <div className="rounded-lg border border-dashed border-border/80 bg-card/45 px-2.5 py-2 text-xs text-muted-foreground">
              暂无历史任务
            </div>
          ) : null}

          {visibleHistoryItems.length > 0 ? (
            <ul className="max-h-[8rem] space-y-0.5 overflow-y-auto pr-0.5">
              {visibleHistoryItems.map((item) => (
                <WorkflowHistoryRow key={item.workflow_id} item={item} />
              ))}
            </ul>
          ) : null}
        </div>
      </section>
    </>
  ) : null;

  if (mobileMode) {
    return (
      <>
        {triggerNode}
        {panelNode}
      </>
    );
  }

  return (
    <span className={cn("relative inline-flex", className)}>
      {triggerNode}
      {panelNode}
    </span>
  );
}

function ThreadSubAgentRow({ item }: { item: ThreadSubAgentDisplayItem }) {
  return (
    <li
      role="listitem"
      aria-label={item.aria_label}
      className="flex items-start gap-1.5 rounded-lg px-1.5 py-1 text-sm transition-colors hover:bg-secondary/60"
    >
      <span
        className={cn(
          "mt-0.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center",
          item.icon_variant === "running" && "text-primary",
          item.icon_variant === "success" && "text-emerald-500",
          item.icon_variant === "error" && "text-destructive",
          item.icon_variant === "pending" && "text-muted-foreground",
        )}
        data-subagent-result={item.icon_variant}
        title={item.status_label}
      >
        {item.icon_variant === "running" ? (
          <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
        ) : item.icon_variant === "success" ? (
          <CheckCircle2 className="h-3.5 w-3.5" />
        ) : item.icon_variant === "error" ? (
          <XCircle className="h-3.5 w-3.5" />
        ) : (
          <Circle className="h-3 w-3" />
        )}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate leading-4 text-foreground">
          {item.name}
        </span>
        <span className="mt-0.5 flex min-w-0 items-center gap-1.5 text-[10px] leading-3 text-muted-foreground">
          <span className="shrink-0">{item.status_label}</span>
          {item.source_label ? (
            <span className="min-w-0 truncate">{item.source_label}</span>
          ) : null}
        </span>
      </span>
    </li>
  );
}

function WorkflowHistoryRow({ item }: { item: ThreadWorkflowHistoryItem }) {
  return (
    <li
      role="listitem"
      aria-label={`${item.status_label}：${item.title}`}
      className="flex items-center gap-2 rounded-lg px-1.5 py-1 text-sm transition-colors hover:bg-secondary/60"
    >
      <span className="min-w-0 flex-1 truncate leading-4 text-foreground">
        {item.title}
      </span>
      <span
        className={cn(
          "flex h-4 w-4 shrink-0 items-center justify-center",
          workflowResultVariant(item.status) === "success" &&
            "text-emerald-500",
          workflowResultVariant(item.status) === "warning" && "text-amber-500",
          workflowResultVariant(item.status) === "error" && "text-destructive",
        )}
        data-workflow-result={workflowResultVariant(item.status)}
        title={item.status_label}
      >
        {workflowResultVariant(item.status) === "success" ? (
          <CheckCircle2 className="h-3.5 w-3.5" />
        ) : workflowResultVariant(item.status) === "error" ? (
          <XCircle className="h-3.5 w-3.5" />
        ) : (
          <AlertTriangle className="h-3.5 w-3.5" />
        )}
      </span>
    </li>
  );
}

function workflowResultVariant(
  status: string,
): "success" | "warning" | "error" {
  const normalized = status.trim().toLowerCase();
  if (normalized === "completed" || normalized === "success") {
    return "success";
  }
  if (
    normalized === "failed" ||
    normalized === "error" ||
    normalized === "cancelled" ||
    normalized === "canceled"
  ) {
    return "error";
  }
  return "warning";
}

function taskProgressSignature(snapshot: ThreadTaskProgressSnapshot): string {
  const taskSignature = snapshot.tasks
    .map((item) =>
      [
        item.orchestration_task_id,
        item.status,
        item.source_status ?? "",
        item.display_order,
        item.updated_at_ms ?? "",
      ].join(":"),
    )
    .join("|");
  return [
    snapshot.session_id,
    snapshot.source,
    snapshot.updated_at_ms,
    snapshot.counts.pending,
    snapshot.counts.in_progress,
    snapshot.counts.completed,
    snapshot.counts.total,
    taskSignature,
  ].join("#");
}

function threadSubAgentsSignature(
  items: ThreadSubAgentDisplayItem[],
): string | null {
  if (items.length === 0) return null;
  return items
    .map((item) =>
      [
        item.key,
        item.status,
        item.started_at_ms ?? "",
        item.updated_at_ms ?? "",
      ].join(":"),
    )
    .join("|");
}

function shouldAutoOpenSnapshot(
  snapshot: ThreadTaskProgressSnapshot | null,
): snapshot is ThreadTaskProgressSnapshot {
  return Boolean(
    snapshot && snapshot.source === "workflow" && snapshot.tasks.length > 0,
  );
}

function shouldAutoOpenInitialSnapshot(
  snapshot: ThreadTaskProgressSnapshot,
): boolean {
  return (
    shouldAutoOpenSnapshot(snapshot) &&
    snapshot.counts.completed < snapshot.counts.total
  );
}

function TaskProgressRow({ item }: { item: ThreadTaskProgressDisplayItem }) {
  return (
    <li
      role="listitem"
      aria-label={item.aria_label}
      className="flex items-start gap-1.5 rounded-lg px-1.5 py-1 text-sm transition-colors hover:bg-secondary/60"
    >
      <span
        className={cn(
          "mt-0.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center",
          item.icon_variant === "check_circle" && "text-emerald-500",
          item.icon_variant === "active_ring" && "text-primary",
          item.icon_variant === "ring" && "text-muted-foreground",
        )}
        data-icon-variant={item.icon_variant}
      >
        {item.icon_variant === "check_circle" ? (
          <CheckCircle2 className="h-3.5 w-3.5" />
        ) : item.icon_variant === "active_ring" ? (
          <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
        ) : (
          <Circle className="h-3 w-3" />
        )}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block break-words leading-4 text-foreground">
          {item.desc}
        </span>
        <span className="mt-0 inline-flex text-[10px] leading-3 text-muted-foreground">
          {item.status_label}
        </span>
      </span>
    </li>
  );
}
