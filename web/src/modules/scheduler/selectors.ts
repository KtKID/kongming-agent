import type {
  SchedulerRunVM,
  SchedulerStoreState,
  SchedulerTaskVM,
} from "./types";

const EMPTY_RUNS: SchedulerRunVM[] = [];

let lastFilteredTasksSource: SchedulerTaskVM[] | null = null;
let lastFilteredTasksFilter: SchedulerStoreState["filter"] | null = null;
let lastFilteredTasksResult: SchedulerTaskVM[] = [];

export function selectFilteredTasks(state: SchedulerStoreState): SchedulerTaskVM[] {
  if (state.filter === "all") return state.tasks;

  if (
    lastFilteredTasksSource === state.tasks &&
    lastFilteredTasksFilter === state.filter
  ) {
    return lastFilteredTasksResult;
  }

  lastFilteredTasksSource = state.tasks;
  lastFilteredTasksFilter = state.filter;
  lastFilteredTasksResult = state.tasks.filter((t) => t.state === state.filter);
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
