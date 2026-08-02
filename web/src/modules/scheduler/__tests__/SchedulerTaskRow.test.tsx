import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { SchedulerTaskRow } from "@/modules/scheduler/components/SchedulerTaskRow";
import type { SchedulerTaskVM } from "@/modules/scheduler/types";

function makeTask(overrides: Partial<SchedulerTaskVM> = {}): SchedulerTaskVM {
  return {
    taskId: "task-1",
    name: "每天早报",
    lifecycle: "scheduled",
    latestRunStatus: "failed",
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

describe("SchedulerTaskRow", () => {
  it("opens the bound thread history without selecting the task row", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();

    render(
      <MemoryRouter initialEntries={["/scheduler"]}>
        <Routes>
          <Route
            path="/scheduler"
            element={
              <SchedulerTaskRow
                task={makeTask()}
                isSelected={false}
                onSelect={onSelect}
                onPause={vi.fn()}
                onResume={vi.fn()}
                onRunNow={vi.fn()}
                onDelete={vi.fn()}
                onEdit={vi.fn()}
                onDuplicate={vi.fn()}
              />
            }
          />
          <Route
            path="/chat/:thread_id"
            element={<div data-testid="chat-target" />}
          />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "打开历史" }));

    expect(onSelect).toHaveBeenCalledTimes(0);
    expect(screen.getByTestId("chat-target")).toBeInTheDocument();
  });

  it("hides the history action when the task has no thread binding", () => {
    render(
      <MemoryRouter>
        <SchedulerTaskRow
          task={makeTask({ threadId: "" })}
          isSelected={false}
          onSelect={vi.fn()}
          onPause={vi.fn()}
          onResume={vi.fn()}
          onRunNow={vi.fn()}
          onDelete={vi.fn()}
          onEdit={vi.fn()}
          onDuplicate={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.queryByRole("button", { name: "打开历史" })).toBeNull();
  });

  it("uses high-contrast colors when the task row is selected", () => {
    render(
      <MemoryRouter>
        <SchedulerTaskRow
          task={makeTask()}
          isSelected={true}
          onSelect={vi.fn()}
          onPause={vi.fn()}
          onResume={vi.fn()}
          onRunNow={vi.fn()}
          onDelete={vi.fn()}
          onEdit={vi.fn()}
          onDuplicate={vi.fn()}
        />
      </MemoryRouter>,
    );

    const card = screen.getByText("每天早报").closest(".rounded-lg");
    expect(card).toHaveClass("bg-primary", "text-primary-foreground");
    expect(screen.getByText("每天早报")).toHaveClass("text-primary-foreground");
    expect(screen.getByText("0 9 * * *").className).toContain(
      "text-primary-foreground/80",
    );
    expect(screen.getByTitle("立即执行").className).toContain(
      "text-primary-foreground",
    );
    expect(screen.getByTitle("删除").className).toContain("text-red-300");
  });

  it("shows lifecycle and latest run result as separate badges", () => {
    render(
      <MemoryRouter>
        <SchedulerTaskRow
          task={makeTask({
            lifecycle: "scheduled",
            latestRunStatus: "failed",
          })}
          isSelected={false}
          onSelect={vi.fn()}
          onPause={vi.fn()}
          onResume={vi.fn()}
          onRunNow={vi.fn()}
          onDelete={vi.fn()}
          onEdit={vi.fn()}
          onDuplicate={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("已调度")).toBeInTheDocument();
    expect(screen.getByText("失败")).toBeInTheDocument();
  });

  it("shows exhausted lifecycle and a live running badge independently", () => {
    render(
      <MemoryRouter>
        <SchedulerTaskRow
          task={makeTask({
            lifecycle: "exhausted",
            latestRunStatus: "completed",
            liveRuntimeStatus: "running",
          })}
          isSelected={false}
          onSelect={vi.fn()}
          onPause={vi.fn()}
          onResume={vi.fn()}
          onRunNow={vi.fn()}
          onDelete={vi.fn()}
          onEdit={vi.fn()}
          onDuplicate={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("已耗尽")).toBeInTheDocument();
    expect(screen.getByText("运行中")).toBeInTheDocument();
  });
});
