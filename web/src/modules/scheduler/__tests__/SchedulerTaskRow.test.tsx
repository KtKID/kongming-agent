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
});
