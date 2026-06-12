import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  conversationKey,
  useAgentWorkflowViewerStore,
} from "../store";

const api = vi.hoisted(() => ({
  fetchAgentWorkflows: vi.fn(),
  fetchAgentWorkflowDetail: vi.fn(),
  fetchAgentWorkflowConversation: vi.fn(),
  fetchAgentWorkflowArtifact: vi.fn(),
  fetchThreadUsage: vi.fn(),
}));

vi.mock("../api", () => api);

describe("agent workflow viewer store", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAgentWorkflowViewerStore.setState({
      activeThreadId: null,
      list: null,
      detail: null,
      conversations: {},
      artifact: null,
      threadUsage: null,
      loadingList: false,
      loadingDetail: false,
      loadingConversation: false,
      loadingArtifact: false,
      loadingThreadUsage: false,
      error: null,
      artifactError: null,
    });
  });

  it("loads workflow list and detail", async () => {
    api.fetchAgentWorkflows.mockResolvedValue({
      thread_id: "thread-abcdef123456",
      workflows: [{ workflow_id: "wf-test" }],
    });
    api.fetchAgentWorkflowDetail.mockResolvedValue({
      item: { workflow_id: "wf-test" },
      reports: [],
    });

    await useAgentWorkflowViewerStore
      .getState()
      .loadList("thread-abcdef123456");
    await useAgentWorkflowViewerStore
      .getState()
      .loadDetail("thread-abcdef123456", "wf-test");

    expect(api.fetchAgentWorkflows).toHaveBeenCalledWith("thread-abcdef123456");
    expect(api.fetchAgentWorkflowDetail).toHaveBeenCalledWith(
      "thread-abcdef123456",
      "wf-test",
    );
    expect(useAgentWorkflowViewerStore.getState().list?.workflows[0].workflow_id).toBe(
      "wf-test",
    );
    expect(useAgentWorkflowViewerStore.getState().detail?.item.workflow_id).toBe(
      "wf-test",
    );
  });

  it("stores conversations by workflow and task run id", async () => {
    api.fetchAgentWorkflowConversation.mockResolvedValue({
      workflow_id: "wf-test",
      task_run_id: "task-1",
      messages: [{ role: "assistant", content: "done" }],
    });

    await useAgentWorkflowViewerStore
      .getState()
      .loadConversation("thread-abcdef123456", "wf-test", "task-1");

    const stored =
      useAgentWorkflowViewerStore.getState().conversations[
        conversationKey("wf-test", "task-1")
      ];
    expect(stored.messages[0].content).toBe("done");
  });

  it("stores artifact content separately from main errors", async () => {
    api.fetchAgentWorkflowArtifact.mockResolvedValue({
      artifact_id: "abc",
      title: "workflow.json",
      content: { status: "completed" },
    });

    await useAgentWorkflowViewerStore
      .getState()
      .loadArtifact("thread-abcdef123456", "wf-test", "abc");

    expect(useAgentWorkflowViewerStore.getState().artifact?.title).toBe(
      "workflow.json",
    );
    expect(useAgentWorkflowViewerStore.getState().artifactError).toBeNull();
  });

  it("loads thread usage independently", async () => {
    api.fetchThreadUsage.mockResolvedValue({
      totals: { total_tokens: 38682 },
    });

    await useAgentWorkflowViewerStore
      .getState()
      .loadThreadUsage("thread-abcdef123456");

    expect(api.fetchThreadUsage).toHaveBeenCalledWith("thread-abcdef123456");
    expect(useAgentWorkflowViewerStore.getState().threadUsage).toEqual({
      totals: { total_tokens: 38682 },
    });
  });
});
