import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { describe, it, expect, beforeEach, vi } from "vitest";
import { FileDrawer } from "@/components/FileDrawer";
import { useWorkspaceStore } from "@/stores/workspace";

vi.mock("@/lib/api", () => ({
  apiGet: vi.fn().mockResolvedValue({
    name: "test.txt",
    path: "/tmp/test.txt",
    size_bytes: 5,
    is_text: true,
    too_large: false,
    content: "hello",
  }),
}));

const WIDTH_KEY = "kongming.fileDrawer.width";

function openDrawer() {
  useWorkspaceStore.setState({
    drawerFile: {
      threadId: "t1",
      relativePath: "test.txt",
      absolutePath: "/tmp/test.txt",
    },
  });
}

beforeEach(() => {
  localStorage.clear();
  useWorkspaceStore.setState({ drawerFile: null });
  Object.defineProperty(window, "innerWidth", {
    writable: true,
    configurable: true,
    value: 1440,
  });
});

describe("FileDrawer 宽度调整", () => {
  it("桌面端（mobileMode=false）渲染 resize handle", async () => {
    render(<FileDrawer mobileMode={false} />);
    openDrawer();
    await waitFor(() => {
      expect(screen.getByTestId("file-drawer-resize-handle")).toBeInTheDocument();
    });
  });

  it("移动端（mobileMode=true）不渲染 resize handle", async () => {
    render(<FileDrawer mobileMode={true} />);
    openDrawer();
    // 等 drawer 渲染出来（loading 出现就证明 sheet 已开）
    await waitFor(() => {
      expect(screen.queryByText("加载中...")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("file-drawer-resize-handle")).toBeNull();
  });

  it("移动端 className 包含 sm:max-w-none 顶掉 sheet 内置 sm:max-w-sm（640-767 视口不会被卡 384px）", async () => {
    render(<FileDrawer mobileMode={true} />);
    openDrawer();
    await waitFor(() => {
      expect(screen.queryByText("加载中...")).toBeInTheDocument();
    });
    // SheetContent 是 Radix 渲染的 dialog content；用 role + class 抓
    const sheetContent = document.querySelector(
      "[role='dialog'][data-state='open']",
    ) as HTMLElement | null;
    expect(sheetContent).not.toBeNull();
    const cls = sheetContent!.className;
    // 必须含 w-[90vw] / max-w-[90vw] / sm:max-w-none 三件套
    expect(cls).toContain("w-[90vw]");
    expect(cls).toContain("max-w-[90vw]");
    expect(cls).toContain("sm:max-w-none");
    // 不应该残留 sheet 内置的 sm:max-w-sm（tailwind-merge 应清理）
    expect(cls).not.toMatch(/\bsm:max-w-sm\b/);
  });

  it("桌面端 className 也保留 sm:max-w-none（防止 sheet 内置 cap 漏出）", async () => {
    render(<FileDrawer mobileMode={false} />);
    openDrawer();
    await waitFor(() => {
      expect(screen.queryByText("加载中...")).toBeInTheDocument();
    });
    const sheetContent = document.querySelector(
      "[role='dialog'][data-state='open']",
    ) as HTMLElement | null;
    expect(sheetContent).not.toBeNull();
    expect(sheetContent!.className).toContain("sm:max-w-none");
    expect(sheetContent!.className).not.toMatch(/\bsm:max-w-sm\b/);
  });

  it("不传 mobileMode 时默认 false（桌面端，渲染 handle）", async () => {
    render(<FileDrawer />);
    openDrawer();
    await waitFor(() => {
      expect(screen.getByTestId("file-drawer-resize-handle")).toBeInTheDocument();
    });
  });

  it("初始宽度来自 localStorage（合法值生效）", async () => {
    localStorage.setItem(WIDTH_KEY, "800");
    render(<FileDrawer mobileMode={false} />);
    openDrawer();
    await waitFor(() => {
      const handle = screen.getByTestId("file-drawer-resize-handle");
      const sheetContent = handle.parentElement as HTMLElement;
      expect(sheetContent.style.width).toBe("800px");
    });
  });

  it("非法 localStorage 值回退默认 640", async () => {
    localStorage.setItem(WIDTH_KEY, "not-a-number");
    render(<FileDrawer mobileMode={false} />);
    openDrawer();
    await waitFor(() => {
      const handle = screen.getByTestId("file-drawer-resize-handle");
      const sheetContent = handle.parentElement as HTMLElement;
      expect(sheetContent.style.width).toBe("640px");
    });
  });

  it("拖拽改变宽度并写入 localStorage", async () => {
    render(<FileDrawer mobileMode={false} />);
    openDrawer();
    const handle = await screen.findByTestId("file-drawer-resize-handle");

    act(() => {
      fireEvent.mouseDown(handle, { clientX: 800 });
      // mousemove 到 clientX=540 → 期望 width = 1440 - 540 = 900
      fireEvent.mouseMove(window, { clientX: 540 });
      fireEvent.mouseUp(window);
    });

    await waitFor(() => {
      const sheetContent = handle.parentElement as HTMLElement;
      expect(sheetContent.style.width).toBe("900px");
    });
    expect(localStorage.getItem(WIDTH_KEY)).toBe("900");
  });

  it("拖拽 clamp 到 min 360", async () => {
    render(<FileDrawer mobileMode={false} />);
    openDrawer();
    const handle = await screen.findByTestId("file-drawer-resize-handle");

    act(() => {
      fireEvent.mouseDown(handle, { clientX: 800 });
      // 拖到极右 → window.innerWidth - clientX 接近 0 → clamp 到 360
      fireEvent.mouseMove(window, { clientX: 1430 });
      fireEvent.mouseUp(window);
    });

    await waitFor(() => {
      const sheetContent = handle.parentElement as HTMLElement;
      expect(sheetContent.style.width).toBe("360px");
    });
    expect(localStorage.getItem(WIDTH_KEY)).toBe("360");
  });

  it("拖拽 clamp 到 max (innerWidth - 80)", async () => {
    render(<FileDrawer mobileMode={false} />);
    openDrawer();
    const handle = await screen.findByTestId("file-drawer-resize-handle");

    act(() => {
      fireEvent.mouseDown(handle, { clientX: 800 });
      // clientX = -100 → 期望 width = 1440 - (-100) = 1540 → clamp 到 1440-80 = 1360
      fireEvent.mouseMove(window, { clientX: -100 });
      fireEvent.mouseUp(window);
    });

    await waitFor(() => {
      const sheetContent = handle.parentElement as HTMLElement;
      expect(sheetContent.style.width).toBe("1360px");
    });
    expect(localStorage.getItem(WIDTH_KEY)).toBe("1360");
  });

  it("双击 handle 重置默认 640 且清空 localStorage", async () => {
    localStorage.setItem(WIDTH_KEY, "900");
    render(<FileDrawer mobileMode={false} />);
    openDrawer();
    const handle = await screen.findByTestId("file-drawer-resize-handle");

    act(() => {
      fireEvent.doubleClick(handle);
    });

    await waitFor(() => {
      const sheetContent = handle.parentElement as HTMLElement;
      expect(sheetContent.style.width).toBe("640px");
    });
    expect(localStorage.getItem(WIDTH_KEY)).toBeNull();
  });

  it("mobileMode props 切换 false → true 时 handle 消失", async () => {
    const { rerender } = render(<FileDrawer mobileMode={false} />);
    openDrawer();
    await screen.findByTestId("file-drawer-resize-handle");

    rerender(<FileDrawer mobileMode={true} />);

    await waitFor(() => {
      expect(screen.queryByTestId("file-drawer-resize-handle")).toBeNull();
    });
  });
});
