import { useEffect, useMemo, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  ChevronDown,
  ChevronRight,
  Circle,
  CheckCircle2,
  Pin,
  RefreshCw,
  Search,
  Unlink,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import {
  useWorkflowStore,
  type TaskSnapshot,
  type WorkflowDefinition,
  type WorkflowRun,
} from "@/stores/workflow";

/* ------------------------------------------------------------------ */
/*  Props                                                              */
/* ------------------------------------------------------------------ */

interface WorkflowDashboardProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/* ------------------------------------------------------------------ */
/*  Node 模板元数据（与后端 templates.py 对应）                         */
/* ------------------------------------------------------------------ */

const NODE_LABELS: Record<string, string> = {
  "x-spec": "Spec",
  "x-req": "Req",
  "x-dev": "Dev",
  "x-verify": "Verify",
  "x-qa-gate": "QA Gate",
  "x-fix": "Fix",
};

/* ------------------------------------------------------------------ */
/*  SpecGroup：左侧树的 spec 分组                                      */
/* ------------------------------------------------------------------ */

/**
 * 从 run + definition 推断当前所在节点：
 * 按 position 排序后，取最后一个 done 的节点 = 当前已完成到哪一步。
 * 全部 done → allDone=true。全部 not_started → 返回 null（未开始）。
 */
function getCurrentNode(
  run: WorkflowRun,
  definitions: WorkflowDefinition[],
): { label: string; allDone: boolean } | null {
  const def = definitions.find((d) => d.id === run.workflow_id);
  if (!def) return null;
  const sorted = [...def.nodes].sort((a, b) => a.position - b.position);
  let lastDone: typeof sorted[0] | null = null;
  let allDone = true;
  for (const node of sorted) {
    if ((run.node_states[node.id] ?? "not_started") === "done") {
      lastDone = node;
    } else {
      allDone = false;
    }
  }
  if (allDone) return { label: "", allDone: true };
  if (!lastDone) return null;
  return { label: NODE_LABELS[lastDone.template_id] ?? lastDone.template_id, allDone: false };
}

function SpecGroup({
  label,
  tasks,
  selectedTaskId,
  onSelect,
  onTogglePin,
  runs,
  definitions,
}: {
  label: string;
  tasks: TaskSnapshot[];
  selectedTaskId: string | null;
  onSelect: (id: string) => void;
  onTogglePin: (id: string) => void;
  runs: WorkflowRun[];
  definitions: WorkflowDefinition[];
}) {
  const [collapsed, setCollapsed] = useState(false);
  const runsByTask = useMemo(() => {
    const m = new Map<string, WorkflowRun>();
    for (const r of runs) m.set(r.task_id, r);
    return m;
  }, [runs]);

  // pinned 排前面，其余保持原有顺序
  const sortedTasks = useMemo(
    () => [...tasks].sort((a, b) => (a.pinned === b.pinned ? 0 : a.pinned ? -1 : 1)),
    [tasks],
  );

  return (
    <div className="mb-1">
      <button
        className="flex w-full items-center gap-1.5 px-3 py-1.5 text-xs font-semibold text-muted-foreground hover:bg-accent/40 rounded"
        onClick={() => setCollapsed(!collapsed)}
      >
        {collapsed ? (
          <ChevronRight className="h-3 w-3" />
        ) : (
          <ChevronDown className="h-3 w-3" />
        )}
        <span className="truncate">{label}</span>
        <span className="ml-auto text-[10px] tabular-nums opacity-60">
          {tasks.length}
        </span>
      </button>
      {!collapsed &&
        sortedTasks.map((t) => {
          const run = runsByTask.get(t.task_id);
          const currentNode = run ? getCurrentNode(run, definitions) : null;
          return (
            <button
              key={t.task_id}
              className={cn(
                "group flex w-full items-center gap-2 px-5 py-1.5 text-xs rounded transition-colors",
                t.task_id === selectedTaskId
                  ? "border-l-2 border-primary bg-accent/40 text-accent-foreground"
                  : "text-foreground/80 hover:bg-accent/30",
              )}
              onClick={() => onSelect(t.task_id)}
            >
              <span
                role="button"
                className={cn(
                  "shrink-0 transition-opacity",
                  t.pinned
                    ? "opacity-100 text-primary"
                    : "opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-primary",
                )}
                onClick={(e) => {
                  e.stopPropagation();
                  onTogglePin(t.task_id);
                }}
              >
                <Pin className="h-3 w-3" />
              </span>
              <span className="truncate flex-1 text-left">{t.name}</span>
              {run ? (
                currentNode?.allDone ? (
                  <span className="shrink-0 flex items-center gap-1 text-[10px] text-emerald-600 dark:text-emerald-400">
                    <CheckCircle2 className="h-3 w-3" />
                    完成
                  </span>
                ) : currentNode ? (
                  <span className="shrink-0 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                    {currentNode.label}
                  </span>
                ) : null
              ) : (
                <Unlink className="h-3 w-3 shrink-0 text-muted-foreground/50" />
              )}
            </button>
          );
        })}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  NodeFlow：右侧的水平节点状态图                                      */
/* ------------------------------------------------------------------ */

function NodeFlow({
  run,
  definition,
}: {
  run: WorkflowRun;
  definition: WorkflowDefinition;
}) {
  const sortedNodes = useMemo(
    () => [...definition.nodes].sort((a, b) => a.position - b.position),
    [definition.nodes],
  );

  return (
    <div className="flex items-center gap-1 flex-wrap">
      {sortedNodes.map((node, i) => {
        const status = run.node_states[node.id] ?? "not_started";
        const isDone = status === "done";
        const label = NODE_LABELS[node.template_id] ?? node.template_id;
        return (
          <div key={node.id} className="flex items-center gap-1">
            {i > 0 && (
              <div className="w-4 h-px bg-border" />
            )}
            <div
              className={cn(
                "flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium border transition-colors",
                isDone
                  ? "bg-emerald-50 border-emerald-300 text-emerald-700 dark:bg-emerald-950 dark:border-emerald-700 dark:text-emerald-300"
                  : "bg-muted/50 border-border text-muted-foreground",
              )}
            >
              {isDone ? (
                <CheckCircle2 className="h-3.5 w-3.5" />
              ) : (
                <Circle className="h-3.5 w-3.5" />
              )}
              {label}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Main：WorkflowDashboard                                            */
/* ------------------------------------------------------------------ */

export function WorkflowDashboard({ open, onOpenChange }: WorkflowDashboardProps) {
  const {
    definitions,
    runs,
    scannerStatus,
    loading,
    selectedTaskId,
    fetchAll,
    triggerScan,
    selectTask,
    togglePin,
    startPolling,
    stopPolling,
  } = useWorkflowStore();

  // fetch on open + start/stop polling
  useEffect(() => {
    if (open) {
      fetchAll();
      startPolling();
    } else {
      stopPolling();
    }
    return () => stopPolling();
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  // 按 spec_ref 分组 tasks
  const specGroups = useMemo(() => {
    if (!scannerStatus) return [];
    const groups = new Map<string, { label: string; tasks: TaskSnapshot[] }>();
    for (const task of scannerStatus.tasks) {
      const key = task.spec_ref ?? "__unlinked__";
      if (!groups.has(key)) {
        const spec = scannerStatus.specs.find((s) => s.spec_id === task.spec_ref);
        groups.set(key, {
          label: key === "__unlinked__" ? "未关联" : spec?.name ?? key,
          tasks: [],
        });
      }
      groups.get(key)!.tasks.push(task);
    }
    // 未关联排最后
    const entries = [...groups.entries()].sort((a, b) => {
      if (a[0] === "__unlinked__") return 1;
      if (b[0] === "__unlinked__") return -1;
      return a[1].label.localeCompare(b[1].label);
    });
    return entries;
  }, [scannerStatus]);

  // 搜索
  const [searchQuery, setSearchQuery] = useState("");
  const normalizedQuery = searchQuery.trim().toLowerCase();

  const filteredGroups = useMemo(() => {
    if (!normalizedQuery) return specGroups;
    return specGroups
      .map(([key, group]) => {
        const specMatched = group.label.toLowerCase().includes(normalizedQuery);
        const matchedTasks = group.tasks.filter(
          (t) =>
            t.name.toLowerCase().includes(normalizedQuery) ||
            t.task_id.toLowerCase().includes(normalizedQuery),
        );
        if (!specMatched && matchedTasks.length === 0) return null;
        return [key, {
          label: group.label,
          tasks: matchedTasks.length > 0 ? matchedTasks : group.tasks,
        }] as (typeof specGroups)[number];
      })
      .filter((x): x is NonNullable<typeof x> => x !== null);
  }, [specGroups, normalizedQuery]);

  // 选中 task 的 run 和 definition
  const selectedTask = scannerStatus?.tasks.find(
    (t) => t.task_id === selectedTaskId,
  );
  const selectedRun = runs.find(
    (r) => r.task_id === selectedTaskId && r.status !== "completed",
  );
  const selectedDef = selectedRun
    ? definitions.find((d) => d.id === selectedRun.workflow_id)
    : null;

  // 自动选中第一个 task
  useEffect(() => {
    if (open && !selectedTaskId && scannerStatus?.tasks.length) {
      selectTask(scannerStatus.tasks[0].task_id);
    }
  }, [open, selectedTaskId, scannerStatus]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[90vw] w-[90vw] h-[80vh] flex flex-col gap-0 p-0">
        <DialogHeader className="shrink-0 px-6 pt-5 pb-3 border-b border-border/60">
          <div className="flex items-center justify-between">
            <DialogTitle className="text-base font-semibold">
              工作流看板
            </DialogTitle>
            <div className="flex items-center gap-2">
              {scannerStatus?.last_scan_at && (
                <span className="text-[10px] text-muted-foreground tabular-nums">
                  上次扫描{" "}
                  {new Date(scannerStatus.last_scan_at).toLocaleTimeString()}
                </span>
              )}
              <Button
                variant="ghost"
                size="sm"
                onClick={triggerScan}
                disabled={loading}
                className="h-7 px-2 text-xs"
              >
                <RefreshCw
                  className={cn("h-3 w-3 mr-1", loading && "animate-spin")}
                />
                扫描
              </Button>
            </div>
          </div>
        </DialogHeader>

        <div className="flex flex-1 min-h-0">
          {/* 左侧：搜索 + spec → task 树 */}
          <aside className="w-[280px] shrink-0 border-r border-border/60 flex flex-col">
            <div className="shrink-0 px-2 pt-2 pb-1">
              <div className="relative">
                <Search className="text-muted-foreground pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2" />
                <Input
                  type="text"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  placeholder="搜索 task..."
                  className="h-8 rounded-lg pl-8 pr-8 text-xs"
                />
                {searchQuery && (
                  <button
                    type="button"
                    onClick={() => setSearchQuery("")}
                    className="text-muted-foreground hover:text-foreground absolute right-2 top-1/2 -translate-y-1/2 rounded p-0.5 transition-colors"
                  >
                    <X className="h-3 w-3" />
                  </button>
                )}
              </div>
            </div>
            <div className="flex-1 min-h-0 overflow-y-auto py-1">
              {filteredGroups.length === 0 ? (
                <div className="px-4 py-8 text-center text-xs text-muted-foreground">
                  {loading ? "扫描中..." : normalizedQuery ? "无匹配结果" : "未发现任何 task"}
                </div>
              ) : (
                filteredGroups.map(([key, group]) => (
                  <SpecGroup
                    key={key}
                    label={group.label}
                    tasks={group.tasks}
                    selectedTaskId={selectedTaskId}
                    onSelect={selectTask}
                    onTogglePin={togglePin}
                    runs={runs}
                    definitions={definitions}
                  />
                ))
              )}
            </div>
          </aside>

          {/* 右侧：节点状态 */}
          <main className="flex-1 min-w-0 overflow-y-auto p-6">
            {!selectedTask ? (
              <div className="flex items-center justify-center h-full text-sm text-muted-foreground">
                选择一个 task 查看工作流进度
              </div>
            ) : (
              <div className="space-y-6">
                <div>
                  <h3 className="text-sm font-semibold mb-1">
                    {selectedTask.name}
                  </h3>
                  <p className="text-xs text-muted-foreground">
                    {selectedTask.task_id}
                    {selectedTask.spec_ref && (
                      <span className="ml-2 opacity-60">
                        spec: {selectedTask.spec_ref}
                      </span>
                    )}
                  </p>
                </div>

                {selectedRun && selectedDef ? (
                  <div className="space-y-4">
                    <div>
                      <p className="text-xs text-muted-foreground mb-3">
                        模板：{selectedDef.name}
                      </p>
                      <NodeFlow run={selectedRun} definition={selectedDef} />
                    </div>
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    等待下一次扫描自动绑定...
                  </p>
                )}

                {/* 阶段产物一览 */}
                <div>
                  <p className="text-xs font-medium text-muted-foreground mb-2">
                    阶段产物检测
                  </p>
                  <div className="grid grid-cols-3 gap-2">
                    {Object.entries(selectedTask.stages).map(([stage, status]) => (
                      <div
                        key={stage}
                        className={cn(
                          "flex items-center gap-1.5 rounded px-2.5 py-1 text-xs border",
                          status === "done"
                            ? "bg-emerald-50 border-emerald-200 text-emerald-700 dark:bg-emerald-950 dark:border-emerald-800 dark:text-emerald-300"
                            : "bg-muted/30 border-border/60 text-muted-foreground",
                        )}
                      >
                        {status === "done" ? (
                          <CheckCircle2 className="h-3 w-3" />
                        ) : (
                          <Circle className="h-3 w-3" />
                        )}
                        {NODE_LABELS[stage] ?? stage}
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </main>
        </div>
      </DialogContent>
    </Dialog>
  );
}
