import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { ManagePage } from "@/pages/Manage";
import { useCellsStore } from "@/stores/cells";

const refreshMock = vi.fn();
const stopMock = vi.fn();

beforeEach(() => {
  refreshMock.mockReset().mockResolvedValue(undefined);
  stopMock.mockReset().mockResolvedValue(undefined);
  useCellsStore.setState({
    cells: [],
    loading: false,
    refresh: refreshMock,
    stop: stopMock,
  });
});

describe("ManagePage", () => {
  it("空态显示提示 + mount 后调 refresh", async () => {
    render(
      <MemoryRouter>
        <ManagePage />
      </MemoryRouter>,
    );
    await waitFor(() => expect(refreshMock).toHaveBeenCalled());
    expect(screen.getByText(/暂无活动 cell/)).toBeInTheDocument();
  });

  it("有 cell → 渲染表格 + 停止按钮二次确认", async () => {
    useCellsStore.setState({
      cells: [
        {
          thread_id: "thread-x",
          thread_name: "demo",
          preset_id: "p1",
          created_at: 1,
          last_active_at: 2,
          pending_approval_count: 0,
          status: "running",
        },
      ],
      loading: false,
      refresh: refreshMock,
      stop: stopMock,
    });
    render(
      <MemoryRouter>
        <ManagePage />
      </MemoryRouter>,
    );
    expect(screen.getByText("thread-x")).toBeInTheDocument();
    expect(screen.getByText("demo")).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /停止/ }));
    // 确认 modal 出现
    await waitFor(() =>
      expect(screen.getByText("停止该 cell？")).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: /确认停止/ }));
    expect(stopMock).toHaveBeenCalledWith("thread-x");
  });
});
