import { beforeEach, describe, expect, it, vi } from "vitest";
import { useWorkspaceStore } from "@/stores/workspace";

const apiMocks = vi.hoisted(() => ({
  apiGet: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  apiGet: apiMocks.apiGet,
}));

describe("stores/workspace", () => {
  beforeEach(() => {
    apiMocks.apiGet.mockReset();
    useWorkspaceStore.setState({
      contextsByThread: {},
      loadingByThread: {},
      activeTabByThread: {},
      fileOpenRequestByThread: {},
    });
  });

  it("fetchContext 拉取 thread 的 workspace context", async () => {
    apiMocks.apiGet.mockResolvedValue({
      thread_id: "thread-1",
      backend_kind: "claude_code",
      workspace_root: "/tmp/proj",
      claude_thread_id: "sdk-1",
      shell_provider: "claude_code",
      files_available: true,
      shell_available: true,
      unavailable_reason: null,
    });

    const result = await useWorkspaceStore.getState().fetchContext("thread-1");
    expect(apiMocks.apiGet).toHaveBeenCalledWith(
      "/api/threads/thread-1/workspace-context",
    );
    expect(result.workspace_root).toBe("/tmp/proj");
    expect(
      useWorkspaceStore.getState().contextsByThread["thread-1"]?.shell_provider,
    ).toBe("claude_code");
  });

  it("setActiveTab 按 thread 记录当前 tab", () => {
    useWorkspaceStore.getState().setActiveTab("thread-1", "shell");
    useWorkspaceStore.getState().setActiveTab("thread-2", "git");

    expect(useWorkspaceStore.getState().activeTabByThread["thread-1"]).toBe("shell");
    expect(useWorkspaceStore.getState().activeTabByThread["thread-2"]).toBe("git");
  });

  it("requestOpenFile 按 thread 记录打开请求", () => {
    useWorkspaceStore.getState().requestOpenFile("thread-1", "src/app.ts");
    useWorkspaceStore.getState().requestOpenFile("thread-1", "src/app.ts");

    expect(useWorkspaceStore.getState().fileOpenRequestByThread["thread-1"]).toEqual({
      path: "src/app.ts",
      nonce: 2,
    });
  });

  it("resetThreadState 只清理当前 thread 的状态", () => {
    useWorkspaceStore.setState({
      contextsByThread: {
        "thread-1": {
          thread_id: "thread-1",
          backend_kind: "claude_code",
          workspace_root: "/tmp/one",
          claude_thread_id: "sdk-1",
          shell_provider: "claude_code",
          files_available: true,
          shell_available: true,
          unavailable_reason: null,
        },
        "thread-2": {
          thread_id: "thread-2",
          backend_kind: "claude_code",
          workspace_root: "/tmp/two",
          claude_thread_id: "sdk-2",
          shell_provider: "claude_code",
          files_available: true,
          shell_available: true,
          unavailable_reason: null,
        },
      },
      loadingByThread: {
        "thread-1": true,
        "thread-2": false,
      },
      activeTabByThread: {
        "thread-1": "shell",
        "thread-2": "git",
      },
      fileOpenRequestByThread: {
        "thread-1": {
          path: "src/one.ts",
          nonce: 1,
        },
        "thread-2": {
          path: "src/two.ts",
          nonce: 1,
        },
      },
    });

    useWorkspaceStore.getState().resetThreadState("thread-1");

    const state = useWorkspaceStore.getState();
    expect(state.contextsByThread["thread-1"]).toBeUndefined();
    expect(state.loadingByThread["thread-1"]).toBeUndefined();
    expect(state.activeTabByThread["thread-1"]).toBeUndefined();
    expect(state.fileOpenRequestByThread["thread-1"]).toBeUndefined();
    expect(state.contextsByThread["thread-2"]?.workspace_root).toBe("/tmp/two");
    expect(state.activeTabByThread["thread-2"]).toBe("git");
    expect(state.fileOpenRequestByThread["thread-2"]?.path).toBe("src/two.ts");
  });

  it("fetchContext 失败后会收起 loading 状态", async () => {
    apiMocks.apiGet.mockRejectedValue(new Error("boom"));

    await expect(
      useWorkspaceStore.getState().fetchContext("thread-1"),
    ).rejects.toThrow("boom");

    expect(useWorkspaceStore.getState().loadingByThread["thread-1"]).toBe(false);
  });
});
