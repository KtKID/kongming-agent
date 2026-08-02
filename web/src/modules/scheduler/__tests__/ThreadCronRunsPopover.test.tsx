import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ThreadCronRunsPopover } from "@/modules/scheduler/components/ThreadCronRunsPopover";
import * as schedulerApi from "@/modules/scheduler/api";
import type { SchedulerRunVM } from "@/modules/scheduler/types";

function makeRun(overrides: Partial<SchedulerRunVM> = {}): SchedulerRunVM {
  return {
    runId: "run-1",
    taskId: "task-1",
    taskName: "daily",
    sessionId: "session-1",
    threadId: "thread-1",
    scheduledFor: "2026-06-15T10:00:00+08:00",
    startedAt: "2026-06-15T10:00:01+08:00",
    finishedAt: "2026-06-15T10:00:05+08:00",
    status: "completed",
    failureReason: null,
    finalMessageExcerpt: "done",
    deliveryStatus: "delivered",
    deliveryError: null,
    ...overrides,
  };
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname + location.search}</div>;
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("ThreadCronRunsPopover", () => {
  it("loads task runs and opens the selected run timeline", async () => {
    const user = userEvent.setup();
    vi.spyOn(schedulerApi, "listTaskRuns").mockResolvedValue([
      makeRun({
        runId: "run-old",
        startedAt: "2026-06-15T10:00:01+08:00",
        finalMessageExcerpt: "old answer",
      }),
      makeRun({
        runId: "run-new",
        startedAt: "2026-06-15T14:47:01+08:00",
        finalMessageExcerpt: "latest answer",
      }),
    ]);

    render(
      <MemoryRouter initialEntries={["/chat/thread-1"]}>
        <LocationProbe />
        <Routes>
          <Route
            path="/chat/:thread_id"
            element={
              <ThreadCronRunsPopover threadId="thread-1" taskId="task-1" />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "运行记录" }));

    await waitFor(() =>
      expect(schedulerApi.listTaskRuns).toHaveBeenCalledWith("task-1"),
    );
    await user.click(
      await screen.findByRole("button", { name: /latest answer/ }),
    );

    expect(screen.getByTestId("location")).toHaveTextContent(
      "/chat/thread-1?taskId=task-1&runId=run-new",
    );
  });

  it("supports a custom rail trigger and panel placement classes", async () => {
    const user = userEvent.setup();
    vi.spyOn(schedulerApi, "listTaskRuns").mockResolvedValue([]);

    render(
      <MemoryRouter initialEntries={["/chat/thread-1"]}>
        <Routes>
          <Route
            path="/chat/:thread_id"
            element={
              <ThreadCronRunsPopover
                threadId="thread-1"
                taskId="task-1"
                trigger={({ open, disabled, onClick }) => (
                  <button
                    type="button"
                    data-open={open ? "true" : "false"}
                    disabled={disabled}
                    onClick={onClick}
                  >
                    rail runs
                  </button>
                )}
                panelClassName="left-[calc(100%+0.75rem)] right-auto top-0"
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    const trigger = screen.getByRole("button", { name: "rail runs" });
    expect(trigger).toHaveAttribute("data-open", "false");

    await user.click(trigger);

    await waitFor(() =>
      expect(schedulerApi.listTaskRuns).toHaveBeenCalledWith("task-1"),
    );
    expect(trigger).toHaveAttribute("data-open", "true");
    expect(screen.getByRole("dialog", { name: "定时任务运行记录" })).toHaveClass(
      "left-[calc(100%+0.75rem)]",
      "right-auto",
      "top-0",
    );
    expect(screen.getByText("暂无运行记录")).toBeInTheDocument();
  });

  it("keeps custom trigger closed when disabled", async () => {
    const user = userEvent.setup();
    vi.spyOn(schedulerApi, "listTaskRuns").mockResolvedValue([]);

    render(
      <MemoryRouter initialEntries={["/chat/thread-1"]}>
        <Routes>
          <Route
            path="/chat/:thread_id"
            element={
              <ThreadCronRunsPopover
                threadId="thread-1"
                taskId={null}
                trigger={({ open, onClick }) => (
                  <button
                    type="button"
                    data-open={open ? "true" : "false"}
                    onClick={onClick}
                  >
                    rail runs
                  </button>
                )}
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    const trigger = screen.getByRole("button", { name: "rail runs" });

    await user.click(trigger);

    expect(trigger).toHaveAttribute("data-open", "false");
    expect(
      screen.queryByRole("dialog", { name: "定时任务运行记录" }),
    ).not.toBeInTheDocument();
    expect(schedulerApi.listTaskRuns).not.toHaveBeenCalled();
  });

  it("uses green, yellow, and red icons for run outcomes", async () => {
    const user = userEvent.setup();
    vi.spyOn(schedulerApi, "listTaskRuns").mockResolvedValue([
      makeRun({
        runId: "run-ok",
        status: "completed",
        failureReason: null,
        finalMessageExcerpt: "ok",
      }),
      makeRun({
        runId: "run-target-miss",
        status: "completed",
        failureReason: null,
        finalMessageExcerpt: "target miss",
        deliveryStatus: "delivered",
        deliveryError: "target_unreachable: thread:thread-1",
      }),
      makeRun({
        runId: "run-approval",
        status: "failed",
        failureReason: "needs_approval",
        finalMessageExcerpt: "approval",
      }),
      makeRun({
        runId: "run-error",
        status: "failed",
        failureReason: "runner_exception",
        finalMessageExcerpt: "error",
      }),
    ]);

    render(
      <MemoryRouter initialEntries={["/chat/thread-1"]}>
        <Routes>
          <Route
            path="/chat/:thread_id"
            element={
              <ThreadCronRunsPopover threadId="thread-1" taskId="task-1" />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "运行记录" }));

    expect(await screen.findAllByLabelText("运行完成")).toHaveLength(2);
    expect(screen.getByLabelText("运行有问题")).toBeInTheDocument();
    expect(screen.getByLabelText("运行错误")).toBeInTheDocument();
  });

  it("formats run times with the configured timezone", async () => {
    const user = userEvent.setup();
    vi.spyOn(schedulerApi, "listTaskRuns").mockResolvedValue([
      makeRun({
        runId: "run-la",
        startedAt: "2026-06-15T10:00:00+00:00",
        finalMessageExcerpt: "la time",
      }),
    ]);

    render(
      <MemoryRouter initialEntries={["/chat/thread-1"]}>
        <Routes>
          <Route
            path="/chat/:thread_id"
            element={
              <ThreadCronRunsPopover
                threadId="thread-1"
                taskId="task-1"
                timezone="America/Los_Angeles"
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "运行记录" }));

    expect(await screen.findByText(/03:00/)).toBeInTheDocument();
  });

  it("shows task id and message when loading runs fails", async () => {
    const user = userEvent.setup();
    vi.spyOn(schedulerApi, "listTaskRuns").mockRejectedValue(
      new Error("network down"),
    );

    render(
      <MemoryRouter initialEntries={["/chat/thread-1"]}>
        <Routes>
          <Route
            path="/chat/:thread_id"
            element={
              <ThreadCronRunsPopover threadId="thread-1" taskId="task-1" />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "运行记录" }));

    expect(
      await screen.findByText("加载任务 task-1 的运行记录失败：network down"),
    ).toBeInTheDocument();
  });
});
