import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { LoginPage } from "@/pages/Login";
import { useAuthStore } from "@/stores/auth";
import * as api from "@/lib/api";
import { ApiError, RateLimitedError } from "@/lib/api";

const loginMock = vi.fn();

beforeEach(() => {
  loginMock.mockReset();
  vi.clearAllMocks();
  useAuthStore.setState({
    authenticated: false,
    _checked: false,
    login: loginMock,
  });
});

describe("LoginPage", () => {
  it("成功登录时调用 login", async () => {
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

  it("密码错误时显示提示", async () => {
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

  it("429 时显示倒计时", async () => {
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
    expect(screen.getByRole("button", { name: /登录/ })).toBeDisabled();
  });

  it("登录页显示忘记密码按钮并可打开原始重置弹窗", async () => {
    vi.spyOn(api, "apiPost").mockResolvedValue({ ok: true });
    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "忘记密码？" }));
    expect(screen.getByText("重置密码")).toBeInTheDocument();
    expect(
      screen.getByText("直接输入新密码即可重置，无需额外验证。"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("新密码")).toBeInTheDocument();
  });
});
