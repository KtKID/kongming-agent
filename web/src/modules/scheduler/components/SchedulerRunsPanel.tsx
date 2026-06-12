/**
 * 运行记录面板
 */

import { Loader2 } from "lucide-react";
import { useSchedulerStore } from "../store";
import { selectSelectedRuns } from "../selectors";

const runStatusLabel: Record<string, string> = {
  running: "运行中",
  success: "成功",
  failed: "失败",
  silent: "静默",
  cancelled: "已取消",
};

const runStatusColor: Record<string, string> = {
  running: "text-blue-600",
  success: "text-green-600",
  failed: "text-red-600",
  silent: "text-gray-500",
  cancelled: "text-gray-500",
};

export function SchedulerRunsPanel() {
  const runs = useSchedulerStore(selectSelectedRuns);
  const isLoading = useSchedulerStore((s) => s.isLoadingRuns);

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
        <div
          key={run.runId}
          className="flex items-center gap-2 rounded-md border border-border px-2.5 py-1.5 text-xs"
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
        </div>
      ))}
    </div>
  );
}
