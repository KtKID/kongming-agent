import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  AlertTriangle,
  CheckCircle2,
  History,
  LoaderCircle,
  RefreshCw,
  X,
  XCircle,
} from "lucide-react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { listTaskRuns } from "../api";
import type { SchedulerRunVM } from "../types";

interface ThreadCronRunsPopoverProps {
  threadId?: string;
  taskId?: string | null;
  activeRunId?: string | null;
  timezone?: string | null;
  trigger?: (props: {
    open: boolean;
    disabled: boolean;
    onClick: () => void;
  }) => ReactNode;
  className?: string;
  panelClassName?: string;
  mobileMode?: boolean;
}

const runStatusLabel: Record<string, string> = {
  running: "运行中",
  success: "已完成",
  completed: "已完成",
  failed: "失败",
  silent: "静默",
  inactivity_timeout: "超时",
  abandoned: "已中断",
  cancelled: "已取消",
};

export function ThreadCronRunsPopover({
  threadId,
  taskId,
  activeRunId,
  timezone,
  trigger,
  className,
  panelClassName,
  mobileMode = false,
}: ThreadCronRunsPopoverProps) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [runs, setRuns] = useState<SchedulerRunVM[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const disabled = !threadId || !taskId;
  const displayTimezone = normalizeRunTimezone(timezone);

  const loadRuns = useCallback(async () => {
    if (!taskId) return;
    setIsLoading(true);
    setError(null);
    try {
      setRuns(await listTaskRuns(taskId));
    } catch (err) {
      setError(`加载任务 ${taskId} 的运行记录失败：${errorToMessage(err)}`);
    } finally {
      setIsLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    setOpen(false);
    setRuns([]);
    setError(null);
  }, [taskId, threadId]);

  useEffect(() => {
    if (!open || disabled) return;
    void loadRuns();
  }, [disabled, loadRuns, open]);

  const visibleRuns = useMemo(
    () =>
      [...runs].sort(
        (a, b) => runTimestampMs(b) - runTimestampMs(a),
      ),
    [runs],
  );

  const openRun = (run: SchedulerRunVM) => {
    if (!threadId) return;
    setOpen(false);
    navigate(
      `/chat/${threadId}?${new URLSearchParams({
        taskId: run.taskId || taskId || "",
        runId: run.runId,
      }).toString()}`,
    );
  };

  const defaultTrigger = (
    <Button
      type="button"
      variant="outline"
      size="sm"
      disabled={disabled}
      aria-expanded={open}
      aria-haspopup="dialog"
      onClick={() => setOpen((next) => !next)}
      className={cn("gap-1.5", className)}
      title="运行记录"
    >
      <History className="h-3.5 w-3.5" />
      运行记录
    </Button>
  );
  const triggerNode = trigger
    ? trigger({
        open,
        disabled,
        onClick: () => {
          if (disabled) return;
          setOpen((next) => !next);
        },
      })
    : defaultTrigger;

  const panelNode = open ? (
    <>
      <button
        type="button"
        aria-label="关闭运行记录面板"
        className="fixed inset-0 z-40 cursor-default bg-transparent"
        onClick={() => setOpen(false)}
      />
      <section
        role="dialog"
        aria-label="定时任务运行记录"
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
            运行记录
          </div>
          <div className="flex items-center gap-0.5">
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label="刷新运行记录"
              className="h-7 w-7 rounded-md"
              disabled={disabled || isLoading}
              onClick={() => {
                void loadRuns();
              }}
            >
              <RefreshCw
                className={cn("h-3.5 w-3.5", isLoading && "animate-spin")}
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

        <div className="pt-2">
          {isLoading ? (
            <div className="flex items-center justify-center gap-2 py-5 text-xs text-muted-foreground">
              <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
              加载中
            </div>
          ) : error ? (
            <div className="rounded-md border border-destructive/30 bg-destructive/5 px-2 py-2 text-xs text-destructive">
              {error}
            </div>
          ) : visibleRuns.length === 0 ? (
            <div className="py-5 text-center text-xs text-muted-foreground">
              暂无运行记录
            </div>
          ) : (
            <ul className="max-h-72 space-y-1 overflow-y-auto pr-0.5">
              {visibleRuns.map((run) => (
                <li key={run.runId}>
                  <button
                    type="button"
                    className={cn(
                      "flex w-full items-start gap-2 rounded-md px-2 py-2 text-left text-xs transition-colors",
                      activeRunId === run.runId
                        ? "bg-primary/10 text-foreground"
                        : "hover:bg-secondary/60",
                    )}
                    aria-current={activeRunId === run.runId ? "true" : undefined}
                    onClick={() => openRun(run)}
                  >
                    <RunStatusIcon run={run} />
                    <span className="min-w-0 flex-1">
                      <span className="flex min-w-0 items-center justify-between gap-2">
                        <span className="truncate font-medium text-foreground">
                          {formatRunTime(
                            run.startedAt ?? run.scheduledFor,
                            displayTimezone,
                          )}
                        </span>
                        <span className="shrink-0 text-[11px] text-muted-foreground">
                          {runStatusLabel[run.status] ?? run.status}
                        </span>
                      </span>
                      {run.finalMessageExcerpt ? (
                        <span className="mt-0.5 block truncate text-muted-foreground">
                          {run.finalMessageExcerpt}
                        </span>
                      ) : null}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
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

function RunStatusIcon({ run }: { run: SchedulerRunVM }) {
  const visualState = getRunVisualState(run);
  if (visualState === "running") {
    return (
      <LoaderCircle
        aria-label="运行中"
        className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin text-blue-600"
      />
    );
  }
  if (visualState === "error") {
    return (
      <XCircle
        aria-label="运行错误"
        className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive"
      />
    );
  }
  if (visualState === "complete") {
    return (
      <CheckCircle2
        aria-label="运行完成"
        className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-600"
      />
    );
  }
  return (
    <AlertTriangle
      aria-label="运行有问题"
      className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500"
    />
  );
}

type RunVisualState = "complete" | "warning" | "error" | "running";

function getRunVisualState(run: SchedulerRunVM): RunVisualState {
  const status = String(run.status).toLowerCase();
  const failureReason = String(run.failureReason ?? "").toLowerCase();
  const deliveryStatus = String(run.deliveryStatus ?? "").toLowerCase();
  if (status === "running") return "running";
  if (status === "completed" || status === "success" || status === "silent") {
    return deliveryStatus === "failed" ? "error" : "complete";
  }
  if (
    failureReason === "needs_approval" ||
    status === "needs_approval" ||
    status === "waiting_approval" ||
    status === "awaiting_approval" ||
    status === "inactivity_timeout" ||
    status === "abandoned" ||
    status === "cancelled"
  ) {
    return "warning";
  }
  if (status === "failed" || status === "error") return "error";
  return "warning";
}

function runTimestampMs(run: SchedulerRunVM): number {
  const value = run.startedAt ?? run.scheduledFor ?? run.finishedAt;
  if (!value) return 0;
  const timestamp = new Date(value).getTime();
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function normalizeRunTimezone(timezone: string | null | undefined): string {
  if (!timezone) return "UTC";
  try {
    new Intl.DateTimeFormat("zh-CN", { timeZone: timezone }).format(0);
    return timezone;
  } catch {
    return "UTC";
  }
}

function errorToMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  try {
    return JSON.stringify(error);
  } catch {
    return String(error);
  }
}

interface TimezoneDateParts {
  year: number;
  month: number;
  day: number;
  hour: string;
  minute: string;
}

function datePartsInTimezone(date: Date, timezone: string): TimezoneDateParts {
  const formatter = new Intl.DateTimeFormat("zh-CN", {
    timeZone: timezone,
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  const parts = Object.fromEntries(
    formatter.formatToParts(date).map((part) => [part.type, part.value]),
  );
  return {
    year: Number(parts.year),
    month: Number(parts.month),
    day: Number(parts.day),
    hour: String(parts.hour ?? "00").padStart(2, "0"),
    minute: String(parts.minute ?? "00").padStart(2, "0"),
  };
}

function timezoneDayIndex(parts: TimezoneDateParts): number {
  return Math.floor(Date.UTC(parts.year, parts.month - 1, parts.day) / 86_400_000);
}

function formatRunTime(value: string | null, timezone: string): string {
  if (!value) return "-";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "-";
  const now = new Date();
  const runParts = datePartsInTimezone(date, timezone);
  const nowParts = datePartsInTimezone(now, timezone);
  const time = `${runParts.hour}:${runParts.minute}`;
  const dayDiff = timezoneDayIndex(nowParts) - timezoneDayIndex(runParts);
  if (dayDiff === 0) return `今天 ${time}`;
  if (dayDiff === 1) return `昨天 ${time}`;
  if (runParts.year === nowParts.year) {
    return `${runParts.month}月${runParts.day}日 ${time}`;
  }
  return `${runParts.year}年${runParts.month}月${runParts.day}日 ${time}`;
}
