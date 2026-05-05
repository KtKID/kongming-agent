import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { Layout } from "@/components/Layout";
import { useAuthStore } from "@/stores/auth";
import { useThreadsStore } from "@/stores/threads";

vi.mock("@/components/ThemeToggle", () => ({
  ThemeToggle: () => <button type="button">theme</button>,
}));

// 阻断 auth.logout 真请求
vi.mock("@/lib/api", () => ({
  apiPost: vi.fn().mockResolvedValue(undefined),
  apiGet: vi.fn().mockResolvedValue([]),
  apiPatch: vi.fn().mockResolvedValue(undefined),
  apiDelete: vi.fn().mockResolvedValue(undefined),
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public errorCode: string,
      public detail: string,
    ) {
      super(detail);
    }
  },
  RateLimitedError: class RateLimitedError extends Error {
    constructor(public retryAfterSeconds: number, msg: string) {
      super(msg);
    }
  },
}));

describe("Layout", () => {
  beforeEach(() => {
    useAuthStore.setState({ authenticated: true, _checked: true });
    useThreadsStore.setState({ threads: [], presets: [], loading: false });
  });

  it("渲染 logo + 退出按钮", () => {
    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/chat" element={<div data-testid="chat" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText("kongming")).toBeInTheDocument();
    expect(screen.getByLabelText("退出登录")).toBeInTheDocument();
    expect(screen.getByTestId("chat")).toBeInTheDocument();
    expect(screen.getByTestId("app-header").className).toContain("z-30");
  });

  it("/manage 路径下显示 '运行管理' 标题", () => {
    render(
      <MemoryRouter initialEntries={["/manage"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/manage" element={<div />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText("运行管理")).toBeInTheDocument();
  });
});
