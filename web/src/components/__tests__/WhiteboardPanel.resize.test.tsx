import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WhiteboardPanel } from "@/components/WhiteboardPanel";
import {
  WHITEBOARD_DEFAULT_WIDTH,
  WHITEBOARD_WIDTH_KEY,
} from "@/lib/whiteboard-resize";

// localStorage backend 在 jsdom + 项目 vitest 配置下不稳，统一用 stubGlobal
function makeMockStorage(): Storage {
  const store: Record<string, string> = {};
  return {
    getItem: (k: string) => store[k] ?? null,
    setItem: (k: string, v: string) => {
      store[k] = v;
    },
    clear: () => {
      for (const k of Object.keys(store)) delete store[k];
    },
    removeItem: (k: string) => {
      delete store[k];
    },
    key: () => null,
    length: 0,
  } as Storage;
}

function setViewportWidth(width: number): void {
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    writable: true,
    value: width,
  });
}

describe("WhiteboardPanel · 拖拽手柄渲染规则", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", makeMockStorage());
    setViewportWidth(1440);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("desktop 展开态：渲染左边缘拖拽手柄 + 应用 inline width", () => {
    render(<WhiteboardPanel title="白板" cards={[]} isOpen={true} />);

    const handle = screen.getByTestId("whiteboard-resize-handle");
    expect(handle).toBeInTheDocument();
    expect(handle.getAttribute("role")).toBe("separator");
    expect(handle.getAttribute("aria-orientation")).toBe("vertical");

    const panel = screen.getByTestId("whiteboard-panel");
    expect(panel.style.width).toBe(`${WHITEBOARD_DEFAULT_WIDTH}px`);
  });

  it("compact 模式：不渲染拖拽手柄，且没有 inline width", () => {
    render(
      <WhiteboardPanel
        title="白板"
        cards={[]}
        isOpen={true}
        compactMode={true}
      />,
    );

    expect(screen.queryByTestId("whiteboard-resize-handle")).toBeNull();
    const panel = screen.getByTestId("whiteboard-panel");
    expect(panel.style.width).toBe("");
  });

  it("mobile 模式打开态：不渲染拖拽手柄，className 含 w-[90vw]", () => {
    render(
      <WhiteboardPanel
        title="白板"
        cards={[]}
        isOpen={true}
        compactMode={true}
        mobileMode={true}
      />,
    );

    expect(screen.queryByTestId("whiteboard-resize-handle")).toBeNull();
    const panel = screen.getByTestId("whiteboard-panel");
    expect(panel.className).toContain("w-[90vw]");
    expect(panel.style.width).toBe("");
  });

  it("折叠态：不渲染拖拽手柄", () => {
    render(<WhiteboardPanel title="白板" cards={[]} isOpen={false} />);
    expect(screen.queryByTestId("whiteboard-resize-handle")).toBeNull();
  });

  it("desktop 展开态：mount 时读 localStorage 持久化 width 并应用", () => {
    window.localStorage.setItem(WHITEBOARD_WIDTH_KEY, "950");
    render(<WhiteboardPanel title="白板" cards={[]} isOpen={true} />);
    const panel = screen.getByTestId("whiteboard-panel");
    expect(panel.style.width).toBe("950px");
  });

  it("双击手柄复位宽度为默认值 800 并写 localStorage", () => {
    window.localStorage.setItem(WHITEBOARD_WIDTH_KEY, "1100");
    render(<WhiteboardPanel title="白板" cards={[]} isOpen={true} />);
    const panel = screen.getByTestId("whiteboard-panel");
    expect(panel.style.width).toBe("1100px");

    const handle = screen.getByTestId("whiteboard-resize-handle");
    fireEvent.doubleClick(handle);

    expect(panel.style.width).toBe(`${WHITEBOARD_DEFAULT_WIDTH}px`);
    expect(window.localStorage.getItem(WHITEBOARD_WIDTH_KEY)).toBe(
      String(WHITEBOARD_DEFAULT_WIDTH),
    );
  });
});
