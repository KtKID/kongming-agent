/**
 * 任务列表区域容器
 *
 * v0.5.3：onEdit / onDuplicate 走 props 由 SchedulerDrawerHost 控制
 * dialog 状态（mode + initialTask），而不是 store。
 */

import { Loader2 } from "lucide-react";
import { useSchedulerStore } from "../store";
import { selectFilteredTasks } from "../selectors";
import type { SchedulerTaskVM } from "../types";
import { SchedulerTaskRow } from "./SchedulerTaskRow";

interface Props {
  onEditTask: (task: SchedulerTaskVM) => void;
  onDuplicateTask: (task: SchedulerTaskVM) => void;
}

export function SchedulerTaskList({ onEditTask, onDuplicateTask }: Props) {
  const tasks = useSchedulerStore(selectFilteredTasks);
  const isLoading = useSchedulerStore((s) => s.isLoadingTasks);
  const selectedTaskId = useSchedulerStore((s) => s.selectedTaskId);
  const runtimeStatusByTaskId = useSchedulerStore((s) => s.runtimeStatusByTaskId);
  const filter = useSchedulerStore((s) => s.filter);
  const setFilter = useSchedulerStore((s) => s.setFilter);
  const selectTask = useSchedulerStore((s) => s.selectTask);
  const pauseTask = useSchedulerStore((s) => s.pauseTask);
  const resumeTask = useSchedulerStore((s) => s.resumeTask);
  const runNow = useSchedulerStore((s) => s.runNow);
  const deleteTask = useSchedulerStore((s) => s.deleteTask);

  const filters: { key: typeof filter; label: string }[] = [
    { key: "all", label: "全部" },
    { key: "scheduled", label: "已调度" },
    { key: "paused", label: "已暂停" },
    { key: "disabled", label: "已停用" },
    { key: "exhausted", label: "已耗尽" },
  ];

  return (
    <div className="flex flex-col gap-2">
      {/* 筛选栏 */}
      <div className="flex gap-1">
        {filters.map((f) => (
          <button
            key={f.key}
            onClick={() => setFilter(f.key)}
            className={`rounded-md px-2 py-1 text-xs font-medium transition-colors ${
              filter === f.key
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:bg-secondary"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* 加载态 */}
      {isLoading ? (
        <div className="flex items-center justify-center py-8">
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </div>
      ) : tasks.length === 0 ? (
        <div className="py-8 text-center text-sm text-muted-foreground">
          暂无定时任务
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {tasks.map((task) => (
            <SchedulerTaskRow
              key={task.taskId}
              task={task}
              runtimeStatus={runtimeStatusByTaskId[task.taskId]}
              isSelected={selectedTaskId === task.taskId}
              onSelect={() => selectTask(task.taskId)}
              onPause={() => void pauseTask(task.taskId)}
              onResume={() => void resumeTask(task.taskId)}
              onRunNow={() => void runNow(task.taskId)}
              onDelete={() => void deleteTask(task.taskId)}
              onEdit={() => onEditTask(task)}
              onDuplicate={() => onDuplicateTask(task)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
