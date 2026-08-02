import { describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPatch: vi.fn(),
}));

vi.mock("@/lib/api", () => apiMocks);

import { listPluginTools, updatePluginTool } from "@/modules/plugin-management/api";

describe("plugin-management api", () => {
  it("listPluginTools 读取管理页插件工具列表", async () => {
    apiMocks.apiGet.mockResolvedValue({ plugins: [] });

    await listPluginTools();

    expect(apiMocks.apiGet).toHaveBeenCalledWith("/api/manage/plugins");
  });

  it("updatePluginTool 写回 encoded tool id 和 enabled payload", async () => {
    apiMocks.apiPatch.mockResolvedValue({ id: "mcp/tool", enabled: false });

    await updatePluginTool("mcp/tool", { enabled: false });

    expect(apiMocks.apiPatch).toHaveBeenCalledWith("/api/manage/plugins/mcp%2Ftool", {
      enabled: false,
    });
  });
});
