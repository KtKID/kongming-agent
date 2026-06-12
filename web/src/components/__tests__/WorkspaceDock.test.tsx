import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  WORKSPACE_DOCK_DEFAULT_WIDTH,
  WORKSPACE_DOCK_WIDTH_KEY,
  WorkspaceDock,
  type WorkspaceDockTab,
} from "@/components/WorkspaceDock";

// WorkspaceDock 测试覆盖右侧页签、内容切换、宽度拖拽和 localStorage 宽度恢复。
// Chat 主对话由调用方常驻，这里只验证 Dock 自身负责的右侧面板行为。

function makeMockStorage(): Storage {
  const store: Record<string, string> = {};
  return {
    getItem: (key: string) => store[key] ?? null,
    setItem: (key: string, value: string) => {
      store[key] = value;
    },
    removeItem: (key: string) => {
      delete store[key];
    },
    clear: () => {
      for (const key of Object.keys(store)) delete store[key];
    },
    key: () => null,
    get length() {
      return Object.keys(store).length;
    },
  } as Storage;
}

function renderDock(overrides: Partial<Parameters<typeof WorkspaceDock>[0]> = {}) {
  const onTabChange = vi.fn();
  const props = {
    activeTab: "chat" as WorkspaceDockTab,
    onTabChange,
    thread: { id: "thread-1", title: "Research thread", status: "running" },
    workspaceRoot: "E:/workspace/demo",
    filesContent: <div>Files content</div>,
    gitContent: <div>Git content</div>,
    shellContent: <div>Shell content</div>,
    whiteboardContent: <div>Whiteboard content</div>,
    ...overrides,
  };
  const result = render(<WorkspaceDock {...props} />);
  return { ...result, onTabChange };
}

describe("WorkspaceDock", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", makeMockStorage());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders Chat / Files / Git / Shell / Whiteboard tabs", () => {
    renderDock();

    expect(screen.getByRole("tab", { name: /Chat/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Files/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Git/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Shell/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Whiteboard/ })).toBeInTheDocument();
  });

  it("calls onTabChange when Whiteboard is selected", async () => {
    const { onTabChange } = renderDock();

    await userEvent.click(screen.getByRole("tab", { name: /Whiteboard/ }));

    expect(onTabChange).toHaveBeenCalledWith("whiteboard");
  });

  it("renders the active Whiteboard content", () => {
    renderDock({ activeTab: "whiteboard" });

    expect(screen.getByText("Whiteboard content")).toBeInTheDocument();
  });

  it("does not render a Dock title or width text", () => {
    const { container } = renderDock();

    expect(screen.queryByText("DOCK")).toBeNull();
    expect(container.textContent).not.toMatch(/\b\d+px\b/);
  });

  it("updates inline width and persists it after resize", () => {
    renderDock();
    const dock = screen.getByTestId("workspace-dock");
    const handle = screen.getByTestId("workspace-dock-resize-handle");

    expect(dock.style.width).toBe(`${WORKSPACE_DOCK_DEFAULT_WIDTH}px`);

    fireEvent.pointerDown(handle, { pointerId: 7, clientX: 500 });
    fireEvent.pointerMove(window, { pointerId: 7, clientX: 420 });
    expect(dock.style.width).toBe("440px");

    fireEvent.pointerUp(window, { pointerId: 7, clientX: 420 });
    expect(window.localStorage.getItem(WORKSPACE_DOCK_WIDTH_KEY)).toBe("440");
  });

  it("falls back to the default width when localStorage contains an invalid value", () => {
    window.localStorage.setItem(WORKSPACE_DOCK_WIDTH_KEY, "wide");

    renderDock();

    expect(screen.getByTestId("workspace-dock").style.width).toBe(
      `${WORKSPACE_DOCK_DEFAULT_WIDTH}px`,
    );
  });
});
