import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { AuthGuard } from "@/components/AuthGuard";
import { useAuthStore } from "@/stores/auth";

const checkAuthMock = vi.fn();

beforeEach(() => {
  checkAuthMock.mockReset();
  useAuthStore.setState({
    authenticated: false,
    _checked: false,
    checkAuth: checkAuthMock.mockResolvedValue(undefined),
  });
});

describe("AuthGuard", () => {
  it("未 checkAuth → 显示 skeleton", () => {
    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route element={<AuthGuard />}>
            <Route
              path="/chat"
              element={<div data-testid="chat-content" />}
            />
          </Route>
          <Route path="/login" element={<div data-testid="login" />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("chat-content")).not.toBeInTheDocument();
    expect(screen.queryByTestId("login")).not.toBeInTheDocument();
  });

  it("已检查未认证 → redirect /login", async () => {
    useAuthStore.setState({
      authenticated: false,
      _checked: true,
      checkAuth: checkAuthMock,
    });
    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route element={<AuthGuard />}>
            <Route
              path="/chat"
              element={<div data-testid="chat-content" />}
            />
          </Route>
          <Route path="/login" element={<div data-testid="login" />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("login")).toBeInTheDocument(),
    );
  });

  it("已认证 → 渲染 Outlet", () => {
    useAuthStore.setState({
      authenticated: true,
      _checked: true,
      checkAuth: checkAuthMock,
    });
    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route element={<AuthGuard />}>
            <Route
              path="/chat"
              element={<div data-testid="chat-content" />}
            />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByTestId("chat-content")).toBeInTheDocument();
  });
});
