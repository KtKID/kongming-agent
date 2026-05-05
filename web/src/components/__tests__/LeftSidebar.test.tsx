import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LeftSidebar } from "@/components/LeftSidebar";
import { useThreadsStore } from "@/stores/threads";

vi.mock("@/lib/api", () => ({
  apiPost: vi.fn().mockResolvedValue({ imported: true, thread: { id: "thread-1" } }),
}));

vi.mock("@/components/ThreadList", () => ({
  ThreadList: () => <div>thread list</div>,
}));

vi.mock("@/components/ClaudeProjectsTree", () => ({
  ClaudeProjectsTree: () => <div>claude projects</div>,
}));

describe("LeftSidebar", () => {
  beforeEach(() => {
    useThreadsStore.setState({
      threads: [],
      presets: [],
      loading: false,
      fetchThreads: vi.fn().mockResolvedValue(undefined),
      fetchPresets: vi.fn().mockResolvedValue(undefined),
      createThread: vi.fn(),
      renameThread: vi.fn(),
      deleteThread: vi.fn(),
    });
  });

  it("展开态显示收起按钮并触发回调", () => {
    const onToggleOpen = vi.fn();
    render(
      <MemoryRouter>
        <LeftSidebar isOpen={true} onToggleOpen={onToggleOpen} />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "收起左侧栏" }));
    expect(onToggleOpen).toHaveBeenCalledTimes(1);
    expect(screen.getByText("thread list")).toBeInTheDocument();
  });

  it("收起态显示展开按钮并触发回调", () => {
    const onToggleOpen = vi.fn();
    render(
      <MemoryRouter>
        <LeftSidebar isOpen={false} onToggleOpen={onToggleOpen} />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "展开左侧栏" }));
    expect(onToggleOpen).toHaveBeenCalledTimes(1);
  });

  it("手机收起态使用边缘微把手且宽度归零", () => {
    const onToggleOpen = vi.fn();
    const { container } = render(
      <MemoryRouter>
        <LeftSidebar isOpen={false} compactMode={true} mobileMode={true} onToggleOpen={onToggleOpen} />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByTestId("left-edge-handle"));
    expect(onToggleOpen).toHaveBeenCalledTimes(1);
    expect(container.querySelector("aside")?.className).toContain("w-0");
  });
});
