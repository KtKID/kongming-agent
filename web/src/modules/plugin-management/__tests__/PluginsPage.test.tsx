import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  listPluginTools: vi.fn(),
  updatePluginTool: vi.fn(),
}));

vi.mock("@/modules/plugin-management/api", () => apiMocks);

import { PluginsPage } from "@/modules/plugin-management";
import type { PluginToolDTO } from "@/protocol";

const webSearchTool: PluginToolDTO = {
  id: "mcp__minimax__web_search",
  name: "mcp__minimax__web_search",
  display_name: "Web Search",
  source: "mcp",
  enabled: true,
  server_id: "minimax",
  mcp_tool_name: "web_search",
  description: "Search with MiniMax MCP",
  canonical_name: "mcp__minimax__web_search",
  is_alias: false,
};

describe("PluginsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMocks.listPluginTools.mockResolvedValue({ plugins: [webSearchTool] });
    apiMocks.updatePluginTool.mockResolvedValue(webSearchTool);
  });

  it("从后端读取已注册工具并移除静态默认项", async () => {
    render(<PluginsPage />);

    expect(screen.getByTestId("plugin-management-loading")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "插件" })).toBeInTheDocument();

    expect(await screen.findByText("Web Search")).toBeInTheDocument();
    expect(screen.getByText("Search with MiniMax MCP")).toBeInTheDocument();
    expect(screen.getByText("minimax / web_search")).toBeInTheDocument();
    expect(screen.queryByText("OpenAI Documents")).toBeNull();
    expect(screen.queryByText("Documents")).toBeNull();
    expect(screen.queryByText("PDF")).toBeNull();
    expect(apiMocks.listPluginTools).toHaveBeenCalledTimes(1);
  });

  it("开关写回后端并更新状态", async () => {
    const user = userEvent.setup();
    apiMocks.updatePluginTool.mockResolvedValue({
      ...webSearchTool,
      enabled: false,
    });
    render(<PluginsPage />);

    const webSearchSwitch = await screen.findByLabelText("Web Search 插件开关");
    expect(webSearchSwitch).toHaveAttribute("aria-checked", "true");

    await user.click(webSearchSwitch);

    await waitFor(() => {
      expect(apiMocks.updatePluginTool).toHaveBeenCalledWith(
        "mcp__minimax__web_search",
        { enabled: false },
      );
    });
    expect(webSearchSwitch).toHaveAttribute("aria-checked", "false");
  });

  it("开关写回失败时回滚并展示错误", async () => {
    const user = userEvent.setup();
    apiMocks.updatePluginTool.mockRejectedValue(new Error("patch failed"));
    render(<PluginsPage />);

    const webSearchSwitch = await screen.findByLabelText("Web Search 插件开关");

    await user.click(webSearchSwitch);

    expect(await screen.findByRole("alert")).toHaveTextContent("patch failed");
    await waitFor(() => {
      expect(webSearchSwitch).toHaveAttribute("aria-checked", "true");
    });
  });

  it("空列表展示空态", async () => {
    apiMocks.listPluginTools.mockResolvedValue({ plugins: [] });
    render(<PluginsPage />);

    expect(await screen.findByText("当前没有已注册插件工具")).toBeInTheDocument();
  });

  it("后端返回 disabled 工具时按返回值展示", async () => {
    apiMocks.listPluginTools.mockResolvedValue({
      plugins: [{ ...webSearchTool, enabled: false }],
    });
    render(<PluginsPage />);

    expect(await screen.findByLabelText("Web Search 插件开关")).toHaveAttribute(
      "aria-checked",
      "false",
    );
  });
});
