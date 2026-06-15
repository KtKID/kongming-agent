/**
 * 单任务行卡片
 */

import { useNavigate } from "react-router-dom";
import { Play, Pause, Trash2, Loader2, Pencil, Copy, History } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { SchedulerTaskVM } from "../types";
import { SchedulerStatusBadge } from "./SchedulerStatusBadge";

type Props = {
  task: SchedulerTaskVM;
  isSelected: boolean;
  onSelect: () => void;
  onPause: () => void;
  onResume: () => void;
  onRunNow: () => void;
  onDelete: () => void;
  onEdit: () => void;
  onDuplicate: () => void;
};

function formatRelative(iso: string | null): string {
  if (!iso) return "-";
  try {
    const date = new Date(iso);
    const now = Date.now();
    const diff = date.getTime() - now;
    if (Math.abs(diff) < 60_000) return "刚刚";
    const absDiff = Math.abs(diff);
    const mins = Math.floor(absDiff / 60_000);
    if (mins < 60) return diff > 0 ? `${mins}分钟后` : `${mins}分钟前`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return diff > 0 ? `${hours}小时后` : `${hours}小时前`;
    const days = Math.floor(hours / 24);
    return diff > 0 ? `${days}天后` : `${days}天前`;
  } catch {
    return iso;
  }
}

export function SchedulerTaskRow({
  task,
  isSelected,
  onSelect,
  onPause,
  onResume,
  onRunNow,
  onDelete,
  onEdit,
  onDuplicate,
}: Props) {
  const navigate = useNavigate();
  const isRunning = task.state === "running";
  const isPaused = task.state === "paused";

  return (
    <div
      className={cn(
        "flex flex-col gap-2 rounded-lg border p-3 cursor-pointer transition-colors",
        isSelected
          ? "border-primary bg-accent"
          : "border-border hover:bg-secondary/50",
      )}
      onClick={onSelect}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-sm font-medium">{task.name}</span>
        <SchedulerStatusBadge state={task.state} />
      </div>
      <div className="flex items-center gap-3 text-xs text-muted-foreground">
        <span>
          {task.triggerType === "cron" ? "Cron" : task.triggerType === "once" ? "一次性" : task.triggerType}
        </span>
        <span className="truncate font-mono text-[11px]">
          {task.triggerExpr}
        </span>
        <span>下次: {formatRelative(task.nextRunAt)}</span>
      </div>
      <div
        className="flex items-center gap-1"
        onClick={(e) => e.stopPropagation()}
      >
        {isRunning ? (
          <Button variant="ghost" size="sm" disabled>
            <Loader2 className="h-3 w-3 animate-spin" />
          </Button>
        ) : isPaused ? (
          <Button variant="ghost" size="sm" onClick={onResume} title="恢复">
            <Play className="h-3 w-3" />
          </Button>
        ) : (
          <>
            <Button variant="ghost" size="sm" onClick={onRunNow} title="立即执行">
              <Play className="h-3 w-3" />
            </Button>
            <Button variant="ghost" size="sm" onClick={onPause} title="暂停">
              <Pause className="h-3 w-3" />
            </Button>
          </>
        )}
        <Button variant="ghost" size="sm" onClick={onEdit} title="编辑">
          <Pencil className="h-3 w-3" />
        </Button>
        <Button variant="ghost" size="sm" onClick={onDuplicate} title="复制">
          <Copy className="h-3 w-3" />
        </Button>
        {task.threadId ? (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => navigate(`/chat/${task.threadId}`)}
            title="打开历史"
            aria-label="打开历史"
          >
            <History className="h-3 w-3" />
          </Button>
        ) : null}
        <Button
          variant="ghost"
          size="sm"
          onClick={onDelete}
          title="删除"
          className="text-destructive hover:text-destructive"
        >
          <Trash2 className="h-3 w-3" />
        </Button>
      </div>
    </div>
  );
}
