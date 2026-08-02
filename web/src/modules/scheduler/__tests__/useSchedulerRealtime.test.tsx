import {
  act,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSchedulerRealtime } from "@/modules/scheduler/hooks/useSchedulerRealtime";
import { SchedulerRunsPanel } from "@/modules/scheduler/components/SchedulerRunsPanel";
import { useSchedulerStore } from "@/modules/scheduler/store";
import type { SchedulerTaskVM } from "@/modules/scheduler/types";
import type {
  CronRunDTO,
  CronRunCompletedFrame,
  CronRunFinishedFrame,
  CronRunStartedFrame,
  CronTaskDTO,
} from "@/protocol";
import { useAuthStore } from "@/stores/auth";
import { connectCronWS, disconnectCronWS, type OnCronWSMessage } from "../ws";

vi.mock("../ws", () => ({
  connectCronWS: vi.fn(),
  disconnectCronWS: vi.fn(),
}));

const realRefreshTasks = useSchedulerStore.getState().refreshTasks;
const realLoadRuns = useSchedulerStore.getState().loadRuns;

function RealtimeRunsPanel() {
  useSchedulerRealtime();
  return <SchedulerRunsPanel />;
}

function startedFrame(
  runId: string,
  taskId = "task-1",
): CronRunStartedFrame {
  return {
    frame_type: "cron.run.started",
    timestamp_ms: 1,
    task_id: taskId,
    task_name: "daily",
    run_id: runId,
    session_id: `session-${runId}`,
    thread_id: "thread-aaaaaaaaaaaa",
    scheduled_for: "2026-07-31T00:00:00+00:00",
    started_at: "2026-07-31T00:00:01+00:00",
    status: "running",
  };
}

function completedFrame(
  runId: string,
  taskId = "task-1",
): CronRunCompletedFrame {
  return {
    frame_type: "cron.run.completed",
    timestamp_ms: 3,
    task_id: taskId,
    task_name: "daily",
    run_id: runId,
    session_id: `session-${runId}`,
    thread_id: "thread-aaaaaaaaaaaa",
    final_message: "done",
    delivered_at_iso: "2026-07-31T00:00:03+00:00",
    scheduled_for: "2026-07-31T00:00:00+00:00",
    delivery_target: null,
    next_run_at: null,
    status: "completed",
  };
}

function finishedFrame(
  runId: string,
  taskId = "task-1",
): CronRunFinishedFrame {
  return {
    frame_type: "cron.run.finished",
    timestamp_ms: 2,
    task_id: taskId,
    task_name: "daily",
    run_id: runId,
    session_id: `session-${runId}`,
    thread_id: "thread-aaaaaaaaaaaa",
    scheduled_for: "2026-07-31T00:00:00+00:00",
    started_at: "2026-07-31T00:00:01+00:00",
    finished_at: "2026-07-31T00:00:02+00:00",
    status: "completed",
    final_message: "done",
    error_message: null,
    delivery_error: null,
    next_run_at: null,
  };
}

beforeEach(() => {
  vi.mocked(connectCronWS).mockReset();
  vi.mocked(disconnectCronWS).mockReset();
  useAuthStore.setState({ authenticated: true, _checked: true });
  useSchedulerStore.setState({
    isDrawerOpen: true,
    isBootstrapped: true,
    isLoadingTasks: false,
    isLoadingRuns: false,
    tasks: [],
    taskMap: {},
    runsByTaskId: { "task-1": [] },
    runtimeStatusByTaskId: {},
    liveRunIdsByTaskId: {},
    selectedRunIdByTaskId: {},
    pendingManualRunTaskId: null,
    selectedTaskId: "task-1",
    filter: "all",
    errorMessage: null,
    refreshTasks: vi.fn().mockResolvedValue(undefined),
    loadRuns: vi.fn().mockResolvedValue(undefined),
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("useSchedulerRealtime", () => {
  it("marks the selected task as running when cron run starts", () => {
    renderHook(() => useSchedulerRealtime());
    const onMessage = vi.mocked(connectCronWS).mock.calls[0]?.[0] as
      | OnCronWSMessage
      | undefined;

    onMessage?.(startedFrame("run-1"));

    expect(useSchedulerStore.getState().runtimeStatusByTaskId["task-1"]).toBe(
      "running",
    );
    expect(useSchedulerStore.getState().selectedRunIdByTaskId["task-1"]).toBe(
      "run-1",
    );
    expect(useSchedulerStore.getState().loadRuns).toHaveBeenCalledWith("task-1");
  });

  it("switches to the new run when a manual run is pending", () => {
    useSchedulerStore.setState({
      selectedRunIdByTaskId: { "task-1": "old-run" },
      pendingManualRunTaskId: "task-1",
    });
    renderHook(() => useSchedulerRealtime());
    const onMessage = vi.mocked(connectCronWS).mock.calls[0]?.[0] as
      | OnCronWSMessage
      | undefined;

    onMessage?.(startedFrame("new-run"));

    expect(useSchedulerStore.getState().selectedRunIdByTaskId["task-1"]).toBe(
      "new-run",
    );
    expect(useSchedulerStore.getState().pendingManualRunTaskId).toBeNull();
  });

  it("keeps the selected history run for background cron starts", () => {
    useSchedulerStore.setState({
      selectedRunIdByTaskId: { "task-1": "old-run" },
      pendingManualRunTaskId: null,
    });
    renderHook(() => useSchedulerRealtime());
    const onMessage = vi.mocked(connectCronWS).mock.calls[0]?.[0] as
      | OnCronWSMessage
      | undefined;

    onMessage?.(startedFrame("background-run"));

    expect(useSchedulerStore.getState().selectedRunIdByTaskId["task-1"]).toBe(
      "old-run",
    );
  });

  it("keeps live state until the manager publishes cron run finished", () => {
    renderHook(() => useSchedulerRealtime());
    const onMessage = vi.mocked(connectCronWS).mock.calls[0]?.[0] as
      | OnCronWSMessage
      | undefined;

    expect(onMessage).toBeDefined();
    useSchedulerStore.getState().markRunStarted("task-1", "run-1");
    onMessage?.(completedFrame("run-1"));

    expect(useSchedulerStore.getState().runtimeStatusByTaskId["task-1"]).toBe(
      "running",
    );
    expect(useSchedulerStore.getState().refreshTasks).not.toHaveBeenCalled();
    expect(useSchedulerStore.getState().loadRuns).not.toHaveBeenCalled();
  });

  it("refreshes durable state after cron run finishes", async () => {
    const refreshTasks = vi.fn().mockImplementation(async () => {
      useSchedulerStore.setState({
        runtimeStatusByTaskId: { "task-1": "idle" },
      });
    });
    useSchedulerStore.setState({ refreshTasks });
    renderHook(() => useSchedulerRealtime());
    const onMessage = vi.mocked(connectCronWS).mock.calls[0]?.[0] as
      | OnCronWSMessage
      | undefined;

    await act(async () => {
      useSchedulerStore.getState().markRunStarted("task-1", "run-1");
      onMessage?.(finishedFrame("run-1"));
      await Promise.resolve();
    });

    expect(useSchedulerStore.getState().refreshTasks).toHaveBeenCalledTimes(1);
    expect(useSchedulerStore.getState().loadRuns).toHaveBeenCalledWith("task-1");
    await waitFor(() => {
      expect(useSchedulerStore.getState().runtimeStatusByTaskId["task-1"]).toBe(
        "idle",
      );
    });
  });

  it("renders the terminal REST result after the manager finished frame", async () => {
    const task: SchedulerTaskVM = {
      taskId: "task-1",
      name: "daily",
      lifecycle: "scheduled",
      latestRunStatus: null,
      liveRuntimeStatus: "running",
      triggerType: "cron",
      triggerExpr: "0 9 * * *",
      nextRunAt: null,
      lastRunAt: null,
      timezone: "UTC",
      presetId: "preset-a",
      threadId: "thread-aaaaaaaaaaaa",
      createdBy: "user",
      inputText: "run",
      agentName: "default",
    };
    const terminalRunDTO = {
      run_id: "run-1",
      task_id: "task-1",
      task_name: "daily",
      session_id: "session-run-1",
      thread_id: "thread-aaaaaaaaaaaa",
      scheduled_for: "2026-07-31T00:00:00+00:00",
      started_at: "2026-07-31T00:00:01+00:00",
      finished_at: "2026-07-31T00:00:02+00:00",
      status: "completed",
      failure_reason: null,
      final_message_excerpt: "manager terminal result",
      delivery_status: "delivered",
      delivery_error: null,
    } satisfies CronRunDTO;
    const idleTaskDTO = {
      task_id: task.taskId,
      name: task.name,
      lifecycle: task.lifecycle,
      latest_run_status: "completed",
      live_runtime_status: "idle",
      trigger_type: task.triggerType,
      trigger_expr: task.triggerExpr,
      next_run_at: task.nextRunAt,
      last_run_at: terminalRunDTO.finished_at,
      timezone: "UTC",
      preset_id: task.presetId,
      thread_id: task.threadId,
      created_by: task.createdBy,
      input_text: task.inputText,
      agent_name: task.agentName,
    } satisfies CronTaskDTO;
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const url = String(input);
      if (url === "/api/cron/tasks") {
        return new Response(JSON.stringify([idleTaskDTO]), { status: 200 });
      }
      if (url === "/api/cron/tasks/task-1/runs?limit=20") {
        return new Response(JSON.stringify([terminalRunDTO]), { status: 200 });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    useSchedulerStore.setState({
      tasks: [task],
      taskMap: { [task.taskId]: task },
      selectedTaskId: task.taskId,
      runsByTaskId: { [task.taskId]: [] },
      refreshTasks: realRefreshTasks,
      loadRuns: realLoadRuns,
    });

    render(
      <MemoryRouter>
        <RealtimeRunsPanel />
      </MemoryRouter>,
    );
    const onMessage = vi.mocked(connectCronWS).mock.calls[0]?.[0] as
      | OnCronWSMessage
      | undefined;
    await act(async () => {
      useSchedulerStore.getState().markRunStarted("task-1", "run-1");
      onMessage?.(finishedFrame("run-1"));
      await Promise.resolve();
    });

    expect(await screen.findByText("manager terminal result")).toBeVisible();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/cron/tasks",
      expect.objectContaining({ method: "GET" }),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/cron/tasks/task-1/runs?limit=20",
      expect.objectContaining({ method: "GET" }),
    );
    expect(
      useSchedulerStore.getState().runsByTaskId["task-1"]?.[0]
        ?.finalMessageExcerpt,
    ).toBe("manager terminal result");
    expect(useSchedulerStore.getState().runtimeStatusByTaskId["task-1"]).toBe(
      "idle",
    );
  });

  it("keeps task running while a sibling run remains live", () => {
    renderHook(() => useSchedulerRealtime());
    const onMessage = vi.mocked(connectCronWS).mock.calls[0]?.[0] as
      | OnCronWSMessage
      | undefined;

    onMessage?.(startedFrame("run-a"));
    onMessage?.(startedFrame("run-b"));
    onMessage?.(finishedFrame("run-a"));

    expect(useSchedulerStore.getState().runtimeStatusByTaskId["task-1"]).toBe(
      "running",
    );
    expect(useSchedulerStore.getState().liveRunIdsByTaskId["task-1"]).toEqual([
      "run-b",
    ]);
  });
});
