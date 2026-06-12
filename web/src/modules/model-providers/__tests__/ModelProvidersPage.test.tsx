import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  getProviderCatalog: vi.fn(),
  getProviderConnections: vi.fn(),
  testProvider: vi.fn(),
  connectProvider: vi.fn(),
  testCurrentProvider: vi.fn(),
}));

vi.mock("@/modules/model-providers/api", () => apiMocks);

import { ModelProvidersPage } from "@/modules/model-providers";
import { useModelProvidersStore } from "@/modules/model-providers/store";

const providerCatalog = [
  {
    providerId: "minimax",
    displayName: "Minimax",
    regionLabel: "CN",
    description: "中国区 Minimax API Key，用于启用对应模型预设。",
    logoText: "M",
  },
  {
    providerId: "glm",
    displayName: "GLM",
    regionLabel: "CN",
    description: "智谱 GLM API Key，用于启用 GLM 模型预设。",
    logoText: "G",
  },
  {
    providerId: "deepseek",
    displayName: "DeepSeek",
    regionLabel: "CN",
    description: "DeepSeek API Key，用于启用 DeepSeek 模型预设。",
    logoText: "D",
  },
];

describe("ModelProvidersPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useModelProvidersStore.getState().reset();
    apiMocks.getProviderCatalog.mockResolvedValue(providerCatalog);
    apiMocks.getProviderConnections.mockResolvedValue([]);
  });

  it("按连接状态分组展示紧凑服务商列表", async () => {
    render(<ModelProvidersPage />);

    expect(screen.getByTestId("model-providers-loading")).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText("Minimax（CN）")).toBeInTheDocument();
    });

    expect(screen.getByRole("heading", { name: "已连接" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "未连接" })).toBeInTheDocument();
    expect(screen.getByText("GLM（CN）")).toBeInTheDocument();
    expect(screen.getByText("DeepSeek（CN）")).toBeInTheDocument();
    const minimaxRow = screen.getByTestId("provider-row-minimax");
    expect(within(minimaxRow).getByText("未连接")).toBeInTheDocument();
    expect(within(minimaxRow).getByRole("button", { name: "连接" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "测试" })).toBeNull();
  });

  it("连接弹窗要求测试通过后才能保存", async () => {
    const user = userEvent.setup();
    apiMocks.testProvider.mockResolvedValue({
      providerId: "minimax",
      ok: true,
      message: "连接测试通过。",
    });
    apiMocks.connectProvider.mockResolvedValue({
      providerId: "minimax",
      ok: true,
      message: "已保存，刚刚测试通过。",
      connection: {
        providerId: "minimax",
        status: "connected",
        model: "MiniMax-M3",
        authLabel: "Bearer",
      },
    });

    render(<ModelProvidersPage />);

    await screen.findByText("Minimax（CN）");
    await user.click(
      within(screen.getByTestId("provider-row-minimax")).getByRole("button", {
        name: "连接",
      }),
    );

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    const input = screen.getByLabelText("API Key");
    const testButton = screen.getByRole("button", { name: "测试" });
    const saveButton = screen.getByRole("button", { name: "保存" });
    expect(testButton).toBeDisabled();
    expect(saveButton).toBeDisabled();

    await user.type(input, "minimax-secret-key");
    expect(testButton).toBeEnabled();
    expect(saveButton).toBeDisabled();

    await user.click(testButton);
    await screen.findByText("连接测试通过。");
    expect(apiMocks.testProvider).toHaveBeenCalledWith("minimax", {
      apiKey: "minimax-secret-key",
    });
    expect(saveButton).toBeEnabled();

    await user.click(saveButton);
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    const minimaxRow = screen.getByTestId("provider-row-minimax");
    expect(within(minimaxRow).getByText("已连接")).toBeInTheDocument();
    expect(within(minimaxRow).getByRole("button", { name: "测试" })).toBeInTheDocument();
    expect(within(minimaxRow).getByRole("button", { name: "编辑" })).toBeInTheDocument();
  });

  it("输入变化会重置测试通过状态", async () => {
    const user = userEvent.setup();
    apiMocks.testProvider.mockResolvedValue({
      providerId: "minimax",
      ok: true,
      message: "连接测试通过。",
    });

    render(<ModelProvidersPage />);

    await screen.findByText("Minimax（CN）");
    await user.click(
      within(screen.getByTestId("provider-row-minimax")).getByRole("button", {
        name: "连接",
      }),
    );
    const input = screen.getByLabelText("API Key");
    const saveButton = screen.getByRole("button", { name: "保存" });

    await user.type(input, "minimax-secret-key");
    await user.click(screen.getByRole("button", { name: "测试" }));
    await screen.findByText("连接测试通过。");
    expect(saveButton).toBeEnabled();

    await user.type(input, "-changed");
    expect(saveButton).toBeDisabled();
  });

  it("已连接状态提供当前连接测试按钮", async () => {
    const user = userEvent.setup();
    apiMocks.getProviderConnections.mockResolvedValue([
      {
        providerId: "minimax",
        status: "connected",
        model: "MiniMax-M3",
        authLabel: "Bearer",
      },
    ]);
    apiMocks.testCurrentProvider.mockResolvedValue({
      providerId: "minimax",
      ok: true,
      message: "已保存连接测试通过。",
    });

    render(<ModelProvidersPage />);

    const minimaxRow = await screen.findByTestId("provider-row-minimax");
    expect(within(minimaxRow).getByText("已连接")).toBeInTheDocument();
    await user.click(within(minimaxRow).getByRole("button", { name: "测试" }));

    await screen.findByText("已保存连接测试通过。");
    expect(apiMocks.testCurrentProvider).toHaveBeenCalledWith("minimax");
    expect(screen.getByRole("button", { name: "编辑" })).toBeInTheDocument();
  });

  it("后端 catalog/connections 未落地时仍展示本地 Minimax fallback", async () => {
    apiMocks.getProviderCatalog.mockRejectedValue(new Error("Not Found"));
    apiMocks.getProviderConnections.mockRejectedValue(new Error("Not Found"));

    render(<ModelProvidersPage />);

    expect(await screen.findByText("Minimax（CN）")).toBeInTheDocument();
    expect(screen.getByText("GLM（CN）")).toBeInTheDocument();
    expect(screen.getByText("DeepSeek（CN）")).toBeInTheDocument();
    expect(
      within(screen.getByTestId("provider-row-minimax")).getByText("未连接"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("provider-row-minimax")).getByRole("button", {
        name: "连接",
      }),
    ).toBeInTheDocument();
  });
});
