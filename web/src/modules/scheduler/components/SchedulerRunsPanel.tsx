/**
 * 运行记录面板
 */

import { Loader2 } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { cn } from "@/lib/utils";
import { useSchedulerStore } from "../store";
import { selectSelectedRuns, selectSelectedTask } from "../selectors";

const runStatusLabel: Record<string, string> = {
  running: "运行中",
  success: "成功",
  completed: "成功",
  failed: "失败",
  silent: "静默",
  inactivity_timeout: "超时",
  abandoned: "已中断",
  cancelled: "已取消",
};

const runStatusColor: Record<string, string> = {
  running: "text-blue-600",
  success: "text-green-600",
  completed: "text-green-600",
  failed: "text-red-600",
  silent: "text-gray-500",
  inactivity_timeout: "text-amber-600",
  abandoned: "text-amber-600",
  cancelled: "text-gray-500",
};

export function SchedulerRunsPanel() {
  const navigate = useNavigate();
  const task = useSchedulerStore(selectSelectedTask);
  const runs = useSchedulerStore(selectSelectedRuns);
  const isLoading = useSchedulerStore((s) => s.isLoadingRuns);
  const selectedRunId = useSchedulerStore((s) =>
    task?.taskId ? s.selectedRunIdByTaskId[task.taskId] : null,
  );
  const selectRun = useSchedulerStore((s) => s.selectRun);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-4">
        <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (runs.length === 0) {
    return (
      <div className="py-4 text-center text-xs text-muted-foreground">
        暂无运行记录
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1.5">
      {runs.map((run) => (
        <button
          type="button"
          key={run.runId}
          className={cn(
            "flex w-full items-center gap-2 rounded-md border px-2.5 py-1.5 text-left text-xs transition-colors",
            selectedRunId === run.runId
              ? "border-primary/50 bg-primary/10"
              : "border-border hover:bg-muted/40",
          )}
          onClick={() => {
            const threadId = run.threadId || task?.threadId || "";
            selectRun(run.taskId, run.runId);
            if (!threadId) return;
            const params = new URLSearchParams({
              taskId: run.taskId,
              runId: run.runId,
            });
            navigate(`/chat/${threadId}?${params.toString()}`);
          }}
          aria-pressed={selectedRunId === run.runId}
          title="打开本次执行"
        >
          <span className={`font-medium ${runStatusColor[run.status] ?? ""}`}>
            {runStatusLabel[run.status] ?? run.status}
          </span>
          <span className="text-muted-foreground">
            {run.scheduledFor ? new Date(run.scheduledFor).toLocaleString() : "-"}
          </span>
          {run.finalMessageExcerpt && (
            <span className="flex-1 truncate text-muted-foreground">
              {run.finalMessageExcerpt}
            </span>
          )}
        </button>
      ))}
    </div>
  );
}
