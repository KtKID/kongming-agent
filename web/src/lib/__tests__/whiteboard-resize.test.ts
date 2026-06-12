import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  WHITEBOARD_DEFAULT_WIDTH,
  WHITEBOARD_MIN_WIDTH,
  WHITEBOARD_RESERVED_CHAT_WIDTH,
  WHITEBOARD_WIDTH_KEY,
  clampWidth,
  getMaxWidth,
  loadPersistedWidth,
  persistWidth,
  useWhiteboardResize,
} from "@/lib/whiteboard-resize";

// 项目 vitest 环境的 localStorage backend 不稳（参考 LeftSidebarTabs.test.tsx）。
// 用 stubGlobal 替换为可控的内存 mock。
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

describe("whiteboard-resize 纯函数", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", makeMockStorage());
    setViewportWidth(1440);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  describe("getMaxWidth", () => {
    it("大屏：返回 viewport - 320（含写死值对照常量）", () => {
      // 写死值断言（核心契约）：viewport 1440 → max 1120，避免常量变了测试跟着变绿
      expect(getMaxWidth(1440)).toBe(1120);
      expect(getMaxWidth(1920)).toBe(1600);
      // 常量算术断言（关系契约）：实现公式 = viewport - RESERVED
      expect(getMaxWidth(1440)).toBe(1440 - WHITEBOARD_RESERVED_CHAT_WIDTH);
    });

    it("小屏：max 退到 min（避免负数）", () => {
      // viewport - 320 < 384，max 钳到 min（384）
      expect(getMaxWidth(420)).toBe(384);
      expect(getMaxWidth(100)).toBe(384);
      expect(getMaxWidth(0)).toBe(384);
      expect(getMaxWidth(-1000)).toBe(384);
    });

    it("NaN / Infinity 退化到 min（输入边界防御）", () => {
      expect(getMaxWidth(Number.NaN)).toBe(WHITEBOARD_MIN_WIDTH);
      expect(getMaxWidth(Number.POSITIVE_INFINITY)).toBe(WHITEBOARD_MIN_WIDTH);
      expect(getMaxWidth(Number.NEGATIVE_INFINITY)).toBe(WHITEBOARD_MIN_WIDTH);
    });
  });

  describe("clampWidth", () => {
    it("正常值在 [min, max] 内原样返回（取整）", () => {
      expect(clampWidth(800, 1440)).toBe(800);
      expect(clampWidth(800.6, 1440)).toBe(801);
    });

    it("小于 min 返回 min", () => {
      expect(clampWidth(100, 1440)).toBe(WHITEBOARD_MIN_WIDTH);
      expect(clampWidth(0, 1440)).toBe(WHITEBOARD_MIN_WIDTH);
      expect(clampWidth(-500, 1440)).toBe(WHITEBOARD_MIN_WIDTH);
    });

    it("大于 max 返回 max（写死值 + 常量算术双断言）", () => {
      expect(clampWidth(9999, 1440)).toBe(1120);
      expect(clampWidth(9999, 1440)).toBe(1440 - WHITEBOARD_RESERVED_CHAT_WIDTH);
    });

    it("NaN / Infinity 返回 default", () => {
      expect(clampWidth(Number.NaN, 1440)).toBe(WHITEBOARD_DEFAULT_WIDTH);
      expect(clampWidth(Number.POSITIVE_INFINITY, 1440)).toBe(WHITEBOARD_DEFAULT_WIDTH);
    });
  });

  describe("loadPersistedWidth", () => {
    it("localStorage 无值 → default（在 viewport 边界内）", () => {
      expect(loadPersistedWidth(1440)).toBe(WHITEBOARD_DEFAULT_WIDTH);
    });

    it("读到合法值 → 原样返回", () => {
      window.localStorage.setItem(WHITEBOARD_WIDTH_KEY, "900");
      expect(loadPersistedWidth(1440)).toBe(900);
    });

    it("读到非数字字符串 → default", () => {
      window.localStorage.setItem(WHITEBOARD_WIDTH_KEY, "abc");
      expect(loadPersistedWidth(1440)).toBe(WHITEBOARD_DEFAULT_WIDTH);
    });

    it("读到越界值 → clamp 到边界（写死值）", () => {
      window.localStorage.setItem(WHITEBOARD_WIDTH_KEY, "9999");
      expect(loadPersistedWidth(1440)).toBe(1120);

      window.localStorage.setItem(WHITEBOARD_WIDTH_KEY, "50");
      expect(loadPersistedWidth(1440)).toBe(384);
    });
  });

  describe("persistWidth", () => {
    it("写入 localStorage（取整）", () => {
      persistWidth(823.7);
      expect(window.localStorage.getItem(WHITEBOARD_WIDTH_KEY)).toBe("824");
    });

    it("NaN / Infinity 不写盘（输入边界防御）", () => {
      persistWidth(Number.NaN);
      expect(window.localStorage.getItem(WHITEBOARD_WIDTH_KEY)).toBeNull();

      persistWidth(Number.POSITIVE_INFINITY);
      expect(window.localStorage.getItem(WHITEBOARD_WIDTH_KEY)).toBeNull();
    });
  });
});

describe("useWhiteboardResize hook", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", makeMockStorage());
    setViewportWidth(1440);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("mount 时无持久化值则取 default", () => {
    const { result } = renderHook(() => useWhiteboardResize({ enabled: true }));
    expect(result.current.width).toBe(WHITEBOARD_DEFAULT_WIDTH);
    expect(result.current.isResizing).toBe(false);
  });

  it("mount 时读已持久化的 width", () => {
    window.localStorage.setItem(WHITEBOARD_WIDTH_KEY, "950");
    const { result } = renderHook(() => useWhiteboardResize({ enabled: true }));
    expect(result.current.width).toBe(950);
  });

  it("已持久化值越界 → mount 时被 clamp", () => {
    window.localStorage.setItem(WHITEBOARD_WIDTH_KEY, "5000");
    const { result } = renderHook(() => useWhiteboardResize({ enabled: true }));
    expect(result.current.width).toBe(1440 - WHITEBOARD_RESERVED_CHAT_WIDTH);
  });

  it("resetWidth 复位为 default 并写 localStorage", () => {
    window.localStorage.setItem(WHITEBOARD_WIDTH_KEY, "1000");
    const { result } = renderHook(() => useWhiteboardResize({ enabled: true }));
    expect(result.current.width).toBe(1000);

    act(() => {
      result.current.resetWidth();
    });
    expect(result.current.width).toBe(WHITEBOARD_DEFAULT_WIDTH);
    expect(window.localStorage.getItem(WHITEBOARD_WIDTH_KEY)).toBe(
      String(WHITEBOARD_DEFAULT_WIDTH),
    );
  });

  it("enabled=false 时 resetWidth 是 no-op", () => {
    window.localStorage.setItem(WHITEBOARD_WIDTH_KEY, "1000");
    const { result } = renderHook(() => useWhiteboardResize({ enabled: false }));
    expect(result.current.width).toBe(1000);

    act(() => {
      result.current.resetWidth();
    });
    expect(result.current.width).toBe(1000);
    expect(window.localStorage.getItem(WHITEBOARD_WIDTH_KEY)).toBe("1000");
  });

  it("enabled=false 时 startResize 是 no-op，不进入 resizing", () => {
    const { result } = renderHook(() => useWhiteboardResize({ enabled: false }));
    const fakeEvent = {
      pointerId: 1,
      clientX: 500,
      preventDefault: () => undefined,
      stopPropagation: () => undefined,
    } as unknown as React.PointerEvent;

    act(() => {
      result.current.startResize(fakeEvent);
    });
    expect(result.current.isResizing).toBe(false);
  });

  it("视窗 resize 变小时 width clamp 到新 max（写死值）", () => {
    window.localStorage.setItem(WHITEBOARD_WIDTH_KEY, "1100");
    setViewportWidth(1440);
    const { result } = renderHook(() => useWhiteboardResize({ enabled: true }));
    expect(result.current.width).toBe(1100);

    // 缩小视窗：新 max = 1000-320=680
    act(() => {
      setViewportWidth(1000);
      window.dispatchEvent(new Event("resize"));
    });
    expect(result.current.width).toBe(680);
    expect(window.localStorage.getItem(WHITEBOARD_WIDTH_KEY)).toBe("680");
  });

  it("enabled 从 true 切到 false 时如正在拖拽则强制结束并持久化（R2#1）", () => {
    const { result, rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useWhiteboardResize({ enabled }),
      { initialProps: { enabled: true } },
    );

    // 启动拖拽
    const startEvent = {
      pointerId: 3,
      clientX: 1000,
      preventDefault: () => undefined,
      stopPropagation: () => undefined,
    } as unknown as React.PointerEvent;
    act(() => {
      result.current.startResize(startEvent);
    });
    expect(result.current.isResizing).toBe(true);

    // 拖拽中切到 enabled=false（场景：视窗缩到 compact / mobile）
    rerender({ enabled: false });

    expect(result.current.isResizing).toBe(false);
    // 持久化触发：localStorage 应已写入当前 width（即 default 800）
    expect(window.localStorage.getItem(WHITEBOARD_WIDTH_KEY)).toBe(
      String(WHITEBOARD_DEFAULT_WIDTH),
    );
  });

  it("pointercancel 与 pointerup 等价：能正常 finalize（R3#2）", () => {
    const { result } = renderHook(() => useWhiteboardResize({ enabled: true }));

    const startEvent = {
      pointerId: 9,
      clientX: 1000,
      preventDefault: () => undefined,
      stopPropagation: () => undefined,
    } as unknown as React.PointerEvent;
    act(() => {
      result.current.startResize(startEvent);
    });
    expect(result.current.isResizing).toBe(true);

    // 拖到 clientX=920 → width 增加 80 → 880
    act(() => {
      const moveEvent = new Event("pointermove") as PointerEvent;
      Object.defineProperty(moveEvent, "pointerId", { value: 9 });
      Object.defineProperty(moveEvent, "clientX", { value: 920 });
      window.dispatchEvent(moveEvent);
    });
    expect(result.current.width).toBe(880);

    // 用 pointercancel 结束（系统中断，比如 contextmenu 弹出）
    act(() => {
      const cancelEvent = new Event("pointercancel") as PointerEvent;
      Object.defineProperty(cancelEvent, "pointerId", { value: 9 });
      window.dispatchEvent(cancelEvent);
    });
    expect(result.current.isResizing).toBe(false);
    expect(result.current.width).toBe(880);
    expect(window.localStorage.getItem(WHITEBOARD_WIDTH_KEY)).toBe("880");
  });

  it("startResize → pointermove → pointerup 完整流程更新 width 并持久化", () => {
    const { result } = renderHook(() => useWhiteboardResize({ enabled: true }));
    expect(result.current.width).toBe(WHITEBOARD_DEFAULT_WIDTH);

    // 在 clientX=1000 处按下手柄
    const startEvent = {
      pointerId: 7,
      clientX: 1000,
      preventDefault: () => undefined,
      stopPropagation: () => undefined,
    } as unknown as React.PointerEvent;
    act(() => {
      result.current.startResize(startEvent);
    });
    expect(result.current.isResizing).toBe(true);

    // 鼠标向左到 clientX=900 → width 增加 100 → 900
    act(() => {
      const moveEvent = new Event("pointermove") as PointerEvent;
      Object.defineProperty(moveEvent, "pointerId", { value: 7 });
      Object.defineProperty(moveEvent, "clientX", { value: 900 });
      window.dispatchEvent(moveEvent);
    });
    expect(result.current.width).toBe(900);

    // pointerup 结束并持久化
    act(() => {
      const upEvent = new Event("pointerup") as PointerEvent;
      Object.defineProperty(upEvent, "pointerId", { value: 7 });
      Object.defineProperty(upEvent, "clientX", { value: 900 });
      window.dispatchEvent(upEvent);
    });
    expect(result.current.isResizing).toBe(false);
    expect(result.current.width).toBe(900);
    expect(window.localStorage.getItem(WHITEBOARD_WIDTH_KEY)).toBe("900");
  });
});
