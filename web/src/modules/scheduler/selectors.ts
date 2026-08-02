import type {
  SchedulerRunVM,
  SchedulerStoreState,
  SchedulerTaskVM,
} from "./types";

const EMPTY_RUNS: SchedulerRunVM[] = [];

let lastFilteredTasksSource: SchedulerTaskVM[] | null = null;
let lastFilteredTasksFilter: SchedulerStoreState["filter"] | null = null;
let lastFilteredTasksRuntimeSource: SchedulerStoreState["runtimeStatusByTaskId"] | null = null;
let lastFilteredTasksResult: SchedulerTaskVM[] = [];

export function selectFilteredTasks(state: SchedulerStoreState): SchedulerTaskVM[] {
  if (state.filter === "all") return state.tasks;

  if (
    lastFilteredTasksSource === state.tasks &&
    lastFilteredTasksFilter === state.filter &&
    lastFilteredTasksRuntimeSource === state.runtimeStatusByTaskId
  ) {
    return lastFilteredTasksResult;
  }

  lastFilteredTasksSource = state.tasks;
  lastFilteredTasksFilter = state.filter;
  lastFilteredTasksRuntimeSource = state.runtimeStatusByTaskId;
  lastFilteredTasksResult = state.tasks.filter(
    (task) => task.lifecycle === state.filter,
  );
  return lastFilteredTasksResult;
}

export function selectSelectedTask(state: SchedulerStoreState): SchedulerTaskVM | null {
  if (!state.selectedTaskId) return null;
  return state.taskMap[state.selectedTaskId] ?? null;
}

export function selectSelectedRuns(state: SchedulerStoreState) {
  if (!state.selectedTaskId) return EMPTY_RUNS;
  return state.runsByTaskId[state.selectedTaskId] ?? EMPTY_RUNS;
}

export function selectSelectedRun(state: SchedulerStoreState): SchedulerRunVM | null {
  if (!state.selectedTaskId) return null;
  const selectedRunId = state.selectedRunIdByTaskId[state.selectedTaskId];
  if (!selectedRunId) return null;
  return (
    state.runsByTaskId[state.selectedTaskId]?.find(
      (run) => run.runId === selectedRunId,
    ) ?? null
  );
}
