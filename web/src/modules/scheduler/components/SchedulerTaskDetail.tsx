/**
 * 选中任务的详情区
 */

import { useSchedulerStore } from "../store";
import { selectSelectedTask } from "../selectors";
import { SchedulerRunsPanel } from "./SchedulerRunsPanel";

function formatTriggerType(triggerType: string): string {
  if (triggerType === "cron") return "Cron";
  if (triggerType === "once") return "一次性";
  return triggerType;
}

export function SchedulerTaskDetail() {
  const task = useSchedulerStore(selectSelectedTask);

  if (!task) {
    return (
      <div className="flex-1 flex items-center justify-center text-sm text-muted-foreground">
        选择一个任务查看详情
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3 border-t border-border pt-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium tracking-[0.18em] text-muted-foreground">
          任务详情
        </span>
        <span className="truncate text-[11px] text-muted-foreground">
          {task.taskId}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
        <div>
          <span className="text-muted-foreground">触发类型：</span>
          <span>{formatTriggerType(task.triggerType)}</span>
        </div>
        <div>
          <span className="text-muted-foreground">表达式：</span>
          <span className="font-mono">{task.triggerExpr}</span>
        </div>
        <div>
          <span className="text-muted-foreground">下次执行：</span>
          <span>{task.nextRunAt ? new Date(task.nextRunAt).toLocaleString() : "-"}</span>
        </div>
        <div>
          <span className="text-muted-foreground">上次执行：</span>
          <span>{task.lastRunAt ? new Date(task.lastRunAt).toLocaleString() : "-"}</span>
        </div>
      </div>

      <div className="mt-1">
        <span className="text-xs font-medium text-muted-foreground">运行记录</span>
        <SchedulerRunsPanel />
      </div>
    </div>
  );
}
