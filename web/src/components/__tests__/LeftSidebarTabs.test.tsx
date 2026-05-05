import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  LeftSidebarTabs,
  loadPersistedTab,
  persistTab,
} from "@/components/LeftSidebarTabs";

describe("LeftSidebarTabs", () => {
  beforeEach(() => {
    try {
      localStorage.clear();
    } catch {
      // ignore
    }
  });

  it("渲染 通用 / Claude 两个 tab", () => {
    render(<LeftSidebarTabs active="generic" onChange={vi.fn()} />);
    expect(screen.getByText(/通用/)).toBeInTheDocument();
    expect(screen.getByText(/Claude/)).toBeInTheDocument();
  });

  it("点击 Claude tab 触发 onChange('claude')", async () => {
    const onChange = vi.fn();
    render(<LeftSidebarTabs active="generic" onChange={onChange} />);
    await userEvent.click(screen.getByText(/Claude/));
    expect(onChange).toHaveBeenCalledWith("claude");
  });

  it("loadPersistedTab 默认返回 generic", () => {
    expect(loadPersistedTab()).toBe("generic");
  });

  it("persistTab + loadPersistedTab round-trip（用 mock localStorage）", () => {
    // 项目 vitest 环境的 localStorage backend 不稳，直接 mock 全局 localStorage
    const store: Record<string, string> = {};
    const mockLocalStorage = {
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
    };
    vi.stubGlobal("localStorage", mockLocalStorage);
    try {
      persistTab("claude");
      expect(loadPersistedTab()).toBe("claude");
      persistTab("generic");
      expect(loadPersistedTab()).toBe("generic");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("loadPersistedTab 对未知值兜底 generic", () => {
    const mockLocalStorage = {
      getItem: () => "garbage",
      setItem: () => {},
      clear: () => {},
      removeItem: () => {},
      key: () => null,
      length: 0,
    };
    vi.stubGlobal("localStorage", mockLocalStorage);
    try {
      expect(loadPersistedTab()).toBe("generic");
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
