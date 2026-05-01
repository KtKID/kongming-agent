import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { LoginPage } from "@/pages/Login";
import { useAuthStore } from "@/stores/auth";
import { ApiError, RateLimitedError } from "@/lib/api";

// 替换 useAuthStore.login 行为
const loginMock = vi.fn();

beforeEach(() => {
  loginMock.mockReset();
  useAuthStore.setState({
    authenticated: false,
    _checked: false,
    login: loginMock,
    // 保留其它 actions
  });
});

describe("LoginPage", () => {
  it("成功登录 → 跳 /chat", async () => {
    loginMock.mockResolvedValue(undefined);
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <LoginPage />
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("密码"), "secret");
    await user.click(screen.getByRole("button", { name: /登录/ }));
    await waitFor(() => expect(loginMock).toHaveBeenCalledWith("secret"));
  });

  it("密码错误 → 显示 '密码错误'", async () => {
    loginMock.mockRejectedValue(new ApiError(401, "unauthenticated", "x"));
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <LoginPage />
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("密码"), "wrong");
    await user.click(screen.getByRole("button", { name: /登录/ }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("密码错误"),
    );
  });

  it("429 → 显示倒计时", async () => {
    loginMock.mockRejectedValue(new RateLimitedError(60, "too many"));
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <LoginPage />
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("密码"), "x");
    await user.click(screen.getByRole("button", { name: /登录/ }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/60s/),
    );
  });

  it("空密码时按钮禁用", () => {
    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>,
    );
    const btn = screen.getByRole("button", { name: /登录/ });
    expect(btn).toBeDisabled();
  });
});
