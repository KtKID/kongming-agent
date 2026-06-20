import { describe, expect, it } from "vitest";

import {
  selectFilteredTasks,
  selectSelectedRuns,
} from "@/modules/scheduler/selectors";
import type {
  SchedulerRunVM,
  SchedulerStoreState,
  SchedulerTaskVM,
} from "@/modules/scheduler/types";

function makeTask(
  taskId: string,
  state: SchedulerTaskVM["state"],
): SchedulerTaskVM {
  return {
    taskId,
    name: taskId,
    enabled: true,
    state,
    triggerType: "cron",
    triggerExpr: "0 9 * * *",
    nextRunAt: null,
    lastRunAt: null,
    timezone: "Asia/Shanghai",
    presetId: "default",
    threadId: "",
    createdBy: "test",
    inputText: "",
    agentName: "default",
  };
}

function makeState(overrides: Partial<SchedulerStoreState> = {}): SchedulerStoreState {
  return {
    isDrawerOpen: false,
    isBootstrapped: false,
    isLoadingTasks: false,
    isLoadingRuns: false,
    tasks: [],
    taskMap: {},
    runsByTaskId: {},
    selectedTaskId: null,
    filter: "all",
    errorMessage: null,
    ...overrides,
  };
}

describe("scheduler selectors", () => {
  it("returns a stable empty runs array when nothing is selected", () => {
    const state = makeState();

    expect(selectSelectedRuns(state)).toBe(selectSelectedRuns(state));
  });

  it("returns a stable empty runs array when the selected task has no runs", () => {
    const task = makeTask("task-1", "scheduled");
    const state = makeState({
      tasks: [task],
      taskMap: { [task.taskId]: task },
      selectedTaskId: task.taskId,
    });

    expect(selectSelectedRuns(state)).toBe(selectSelectedRuns(state));
  });

  it("preserves the existing runs array reference", () => {
    const task = makeTask("task-1", "scheduled");
    const runs: SchedulerRunVM[] = [
      {
        runId: "run-1",
        taskId: task.taskId,
        taskName: task.name,
        scheduledFor: "2026-05-14T10:00:00+08:00",
        startedAt: null,
        finishedAt: null,
        status: "running",
        finalMessageExcerpt: null,
        deliveryStatus: "pending",
        deliveryError: null,
      },
    ];
    const state = makeState({
      tasks: [task],
      taskMap: { [task.taskId]: task },
      runsByTaskId: { [task.taskId]: runs },
      selectedTaskId: task.taskId,
    });

    expect(selectSelectedRuns(state)).toBe(runs);
  });

  it("memoizes filtered task arrays for the same snapshot", () => {
    const tasks = [makeTask("task-1", "running"), makeTask("task-2", "paused")];
    const state = makeState({ tasks, filter: "running" });

    expect(selectFilteredTasks(state)).toBe(selectFilteredTasks(state));
  });
});
