/**
 * 任务状态 badge
 */

import { cn } from "@/lib/utils";

const stateConfig: Record<
  string,
  { label: string; className: string }
> = {
  scheduled: {
    label: "已调度",
    className: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
  },
  paused: {
    label: "已暂停",
    className:
      "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200",
  },
  running: {
    label: "运行中",
    className: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200",
  },
  completed: {
    label: "已完成",
    className:
      "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300",
  },
  failed: {
    label: "失败",
    className: "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200",
  },
};

export function SchedulerStatusBadge({ state }: { state: string }) {
  const config = stateConfig[state] ?? {
    label: state,
    className: "bg-muted text-muted-foreground",
  };

  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium",
        config.className,
      )}
    >
      {config.label}
    </span>
  );
}
