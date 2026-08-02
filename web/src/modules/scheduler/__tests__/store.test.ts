import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

import * as schedulerApi from "@/modules/scheduler/api";
import { useSchedulerStore } from "@/modules/scheduler/store";
import type { SchedulerRunVM, SchedulerTaskVM } from "@/modules/scheduler/types";
import { useThreadsStore } from "@/stores/threads";

function makeTask(overrides: Partial<SchedulerTaskVM> = {}): SchedulerTaskVM {
  return {
    taskId: "task-1",
    name: "每天早报",
    lifecycle: "scheduled",
    latestRunStatus: null,
    liveRuntimeStatus: "idle",
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

function makeRun(overrides: Partial<SchedulerRunVM> = {}): SchedulerRunVM {
  return {
    runId: "run-1",
    taskId: "task-1",
    taskName: "daily",
    sessionId: "session-run-1",
    threadId: "thread-bbbbbbbbbbbb",
    scheduledFor: "2026-06-15T10:00:00+08:00",
    startedAt: null,
    finishedAt: null,
    status: "completed",
    failureReason: null,
    finalMessageExcerpt: "done",
    deliveryStatus: "delivered",
    deliveryError: null,
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
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
    runtimeStatusByTaskId: {},
    liveRunIdsByTaskId: {},
    selectedRunIdByTaskId: {},
    pendingManualRunTaskId: null,
    selectedTaskId: null,
    filter: "all",
    errorMessage: null,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("scheduler store", () => {
  it("rebuilds runtime projection from the authoritative task refresh", async () => {
    vi.spyOn(schedulerApi, "listTasks").mockResolvedValue([
      makeTask({ liveRuntimeStatus: "running" }),
    ]);
    useSchedulerStore.setState({
      runtimeStatusByTaskId: { "task-1": "idle" },
      liveRunIdsByTaskId: {},
    });

    await useSchedulerStore.getState().refreshTasks();

    expect(useSchedulerStore.getState().runtimeStatusByTaskId["task-1"]).toBe(
      "running",
    );
  });

  it("discards an older task refresh that resolves after a newer request", async () => {
    const older = deferred<SchedulerTaskVM[]>();
    const newer = deferred<SchedulerTaskVM[]>();
    vi.spyOn(schedulerApi, "listTasks")
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise);

    const olderRequest = useSchedulerStore.getState().refreshTasks();
    const newerRequest = useSchedulerStore.getState().refreshTasks();
    newer.resolve([makeTask({ liveRuntimeStatus: "running" })]);
    await newerRequest;
    older.resolve([makeTask({ liveRuntimeStatus: "idle" })]);
    await olderRequest;

    expect(useSchedulerStore.getState().runtimeStatusByTaskId["task-1"]).toBe(
      "running",
    );
  });

  it("merges a WS started run into an in-flight idle task refresh", async () => {
    const response = deferred<SchedulerTaskVM[]>();
    vi.spyOn(schedulerApi, "listTasks").mockReturnValue(response.promise);

    const refresh = useSchedulerStore.getState().refreshTasks();
    useSchedulerStore.getState().markRunStarted("task-1", "run-live");
    response.resolve([makeTask({ liveRuntimeStatus: "idle" })]);
    await refresh;

    expect(useSchedulerStore.getState().runtimeStatusByTaskId["task-1"]).toBe(
      "running",
    );
    expect(useSchedulerStore.getState().taskMap["task-1"].liveRuntimeStatus).toBe(
      "running",
    );
  });

  it("keeps authoritative running when a terminal arrives for an unknown sibling", () => {
    useSchedulerStore.setState({
      runtimeStatusByTaskId: { "task-1": "running" },
      liveRunIdsByTaskId: {},
    });

    useSchedulerStore.getState().markRunFinished("task-1", "run-a");

    expect(useSchedulerStore.getState().runtimeStatusByTaskId["task-1"]).toBe(
      "running",
    );
  });

  it("keeps authoritative running when the local live set only knows the finished run", () => {
    useSchedulerStore.setState({
      runtimeStatusByTaskId: { "task-1": "running" },
      liveRunIdsByTaskId: { "task-1": ["run-a"] },
    });

    useSchedulerStore.getState().markRunFinished("task-1", "run-a");

    expect(useSchedulerStore.getState().runtimeStatusByTaskId["task-1"]).toBe(
      "running",
    );
    expect(useSchedulerStore.getState().liveRunIdsByTaskId["task-1"]).toEqual([]);
  });

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

  it("marks a task as running after runNow succeeds", async () => {
    vi.spyOn(schedulerApi, "runTaskNow").mockResolvedValue({
      run_id: "pending-run",
      status: "PENDING",
    });

    await useSchedulerStore.getState().runNow("task-1");

    expect(useSchedulerStore.getState().runtimeStatusByTaskId["task-1"]).toBe(
      "running",
    );
    expect(useSchedulerStore.getState().pendingManualRunTaskId).toBe("task-1");
  });

  it("selects the latest run after loading runs when no run is selected", async () => {
    vi.spyOn(schedulerApi, "listTaskRuns").mockResolvedValue([
      makeRun({ runId: "run-latest" }),
    ]);

    await useSchedulerStore.getState().loadRuns("task-1");

    expect(useSchedulerStore.getState().selectedRunIdByTaskId["task-1"]).toBe(
      "run-latest",
    );
  });

  it("discards an older run list that resolves after the terminal refresh", async () => {
    const older = deferred<SchedulerRunVM[]>();
    const newer = deferred<SchedulerRunVM[]>();
    vi.spyOn(schedulerApi, "listTaskRuns")
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise);

    const olderRequest = useSchedulerStore.getState().loadRuns("task-1");
    const newerRequest = useSchedulerStore.getState().loadRuns("task-1");
    newer.resolve([
      makeRun({
        runId: "run-new",
        status: "completed",
        finalMessageExcerpt: "terminal",
      }),
    ]);
    await newerRequest;
    older.resolve([
      makeRun({
        runId: "run-old",
        status: "running",
        finalMessageExcerpt: null,
      }),
    ]);
    await olderRequest;

    expect(useSchedulerStore.getState().runsByTaskId["task-1"]).toEqual([
      expect.objectContaining({
        runId: "run-new",
        status: "completed",
        finalMessageExcerpt: "terminal",
      }),
    ]);
  });

  it("clears pending manual run marker when deleting that task", async () => {
    vi.spyOn(schedulerApi, "deleteTask").mockResolvedValue(undefined);
    useSchedulerStore.setState({
      tasks: [makeTask()],
      taskMap: { "task-1": makeTask() },
      runtimeStatusByTaskId: { "task-1": "running" },
      liveRunIdsByTaskId: { "task-1": ["run-1"] },
      selectedRunIdByTaskId: { "task-1": "run-1" },
      pendingManualRunTaskId: "task-1",
    });

    await useSchedulerStore.getState().deleteTask("task-1");

    expect(useSchedulerStore.getState().pendingManualRunTaskId).toBeNull();
    expect(useSchedulerStore.getState().runtimeStatusByTaskId["task-1"]).toBeUndefined();
    expect(useSchedulerStore.getState().liveRunIdsByTaskId["task-1"]).toBeUndefined();
    expect(useSchedulerStore.getState().selectedRunIdByTaskId["task-1"]).toBeUndefined();
  });
});
