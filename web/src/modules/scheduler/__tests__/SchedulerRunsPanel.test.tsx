import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { SchedulerRunsPanel } from "@/modules/scheduler/components/SchedulerRunsPanel";
import { useSchedulerStore } from "@/modules/scheduler/store";
import type {
  SchedulerRunVM,
  SchedulerTaskVM,
} from "@/modules/scheduler/types";

function makeTask(): SchedulerTaskVM {
  return {
    taskId: "task-1",
    name: "daily",
    lifecycle: "scheduled",
    latestRunStatus: "completed",
    liveRuntimeStatus: "idle",
    triggerType: "cron",
    triggerExpr: "0 9 * * *",
    nextRunAt: null,
    lastRunAt: null,
    timezone: "Asia/Shanghai",
    presetId: "preset-a",
    threadId: "thread-1",
    createdBy: "user",
    inputText: "hello",
    agentName: "default",
  };
}

function makeRun(): SchedulerRunVM {
  return {
    runId: "run-1",
    taskId: "task-1",
    taskName: "daily",
    sessionId: "session-run-1",
    threadId: "thread-1",
    scheduledFor: "2026-06-15T10:00:00+08:00",
    startedAt: null,
    finishedAt: null,
    status: "completed",
    failureReason: null,
    finalMessageExcerpt: "done",
    deliveryStatus: "delivered",
    deliveryError: null,
  };
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname + location.search}</div>;
}

beforeEach(() => {
  const task = makeTask();
  const run = makeRun();
  useSchedulerStore.setState({
    isDrawerOpen: true,
    isBootstrapped: true,
    isLoadingTasks: false,
    isLoadingRuns: false,
    tasks: [task],
    taskMap: { [task.taskId]: task },
    runsByTaskId: { [task.taskId]: [run] },
    runtimeStatusByTaskId: {},
    liveRunIdsByTaskId: {},
    selectedRunIdByTaskId: {},
    pendingManualRunTaskId: null,
    selectedTaskId: task.taskId,
    filter: "all",
    errorMessage: null,
  });
});

describe("SchedulerRunsPanel", () => {
  it("selects a run and opens the bound thread with taskId and runId", async () => {
    const user = userEvent.setup();

    render(
      <MemoryRouter initialEntries={["/scheduler"]}>
        <Routes>
          <Route path="/scheduler" element={<SchedulerRunsPanel />} />
          <Route path="/chat/:thread_id" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(screen.getByTitle("打开本次执行"));

    expect(useSchedulerStore.getState().selectedRunIdByTaskId["task-1"]).toBe(
      "run-1",
    );
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/chat/thread-1?taskId=task-1&runId=run-1",
    );
  });
});
