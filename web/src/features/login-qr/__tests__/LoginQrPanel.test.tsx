import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LoginQrPanel } from "../LoginQrPanel";
import * as manager from "../LoginQrManager";

vi.mock("qrcode", () => ({
  default: {
    toDataURL: vi.fn().mockResolvedValue("data:image/png;base64,qr"),
  },
}));

vi.mock("../LoginQrManager", async () => {
  const actual = await vi.importActual<typeof import("../LoginQrManager")>(
    "../LoginQrManager",
  );
  return {
    ...actual,
    createLoginQrSession: vi.fn(),
    getLoginQrStatus: vi.fn(),
    confirmLoginQrSession: vi.fn(),
  };
});

const createLoginQrSessionMock = vi.mocked(manager.createLoginQrSession);
const getLoginQrStatusMock = vi.mocked(manager.getLoginQrStatus);
const confirmLoginQrSessionMock = vi.mocked(manager.confirmLoginQrSession);

function createdResponse() {
  return {
    login_qr_id: "lq_test",
    browser_token: "kgm_lqt_test",
    status: "pending_scan" as const,
    expires_at: new Date(Date.now() + 60_000).toISOString(),
    server_origin: {
      mode: "public_https" as const,
      origin: "https://kongming.example.com",
      scheme: "https" as const,
      host: "kongming.example.com",
      port: null,
    },
    server: "https://kongming.example.com",
    qr_payload: "xspace://login-kongming?login_qr_id=lq_test",
    copy_url: "https://kongming.example.com/-/xspace/mobile/login?login_qr_id=lq_test",
  };
}

describe("LoginQrPanel", () => {
  beforeEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    createLoginQrSessionMock.mockResolvedValue(createdResponse());
    getLoginQrStatusMock.mockResolvedValue({
      login_qr_id: "lq_test",
      status: "pending_scan",
      expires_at: new Date(Date.now() + 60_000).toISOString(),
      claim: null,
    });
    confirmLoginQrSessionMock.mockResolvedValue({
      status: "confirmed",
      poll_after_ms: 1000,
    });
  });

  it("创建二维码并展示扫码状态", async () => {
    render(<LoginQrPanel />);

    expect(await screen.findByAltText("XSpace 扫码登录二维码")).toBeInTheDocument();
    expect(screen.getByText(/公网 · 等待 XSpace 扫码/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "复制登录链接" })).toBeEnabled();
    expect(createLoginQrSessionMock).toHaveBeenCalledTimes(1);
  });

  it("扫码后展示设备并确认授权", async () => {
    getLoginQrStatusMock.mockResolvedValue({
      login_qr_id: "lq_test",
      status: "pending_confirm",
      expires_at: new Date(Date.now() + 60_000).toISOString(),
      claim: {
        claim_id: "cl_test",
        device_id: "android-pixel-9",
        label: "Pixel 9",
        platform: "android",
        app_version: "0.1.0",
        capabilities: { webview: true },
        status: "pending_confirm",
        created_at: new Date().toISOString(),
      },
    });
    render(<LoginQrPanel pollIntervalMs={10} />);
    await screen.findByAltText("XSpace 扫码登录二维码");

    expect(await screen.findByText("Pixel 9")).toBeInTheDocument();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("扫码登录确认密码"), "pwd");
    await user.click(screen.getByRole("button", { name: "确认授权" }));

    await waitFor(() =>
      expect(confirmLoginQrSessionMock).toHaveBeenCalledWith(
        "lq_test",
        "kgm_lqt_test",
        "cl_test",
        "pwd",
      ),
    );
  });
});
