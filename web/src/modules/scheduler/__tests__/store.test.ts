import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

import * as schedulerApi from "@/modules/scheduler/api";
import { useSchedulerStore } from "@/modules/scheduler/store";
import type { SchedulerTaskVM } from "@/modules/scheduler/types";
import { useThreadsStore } from "@/stores/threads";

function makeTask(overrides: Partial<SchedulerTaskVM> = {}): SchedulerTaskVM {
  return {
    taskId: "task-1",
    name: "每天早报",
    enabled: true,
    state: "scheduled",
    triggerType: "cron",
    triggerExpr: "0 9 * * *",
    nextRunAt: null,
    lastRunAt: null,
    timezone: "Asia/Shanghai",
    presetId: "preset-a",
    threadId: "thread-bbbbbbbbbbbb",
    createdBy: "user",
    inputText: "总结今天",
    agentName: "default",
    ...overrides,
  };
}

beforeEach(() => {
  useSchedulerStore.setState({
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
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("scheduler store", () => {
  it("refreshes threads after createTask succeeds", async () => {
    const fetchThreads = vi.fn().mockResolvedValue(undefined);
    useThreadsStore.setState({ fetchThreads } as never);
    vi.spyOn(schedulerApi, "createTask").mockResolvedValue(makeTask());

    const result = await useSchedulerStore.getState().createTask({
      name: "每天早报",
      agent_name: "default",
      input_text: "总结今天",
      schedule_type: "cron",
      cron_expr: "0 9 * * *",
      timezone: "Asia/Shanghai",
    });

    expect(result?.threadId).toBe("thread-bbbbbbbbbbbb");
    expect(fetchThreads).toHaveBeenCalledTimes(1);
  });
});
