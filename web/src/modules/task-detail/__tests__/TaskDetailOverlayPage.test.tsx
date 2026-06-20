import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TaskDetailOverlayPage } from "../TaskDetailOverlayPage";

const api = vi.hoisted(() => ({
  fetchThreadArtifacts: vi.fn(),
  fetchThreadArtifactContent: vi.fn(),
}));

vi.mock("../api", () => api);

vi.mock("@/modules/agent-workflow-viewer", () => ({
  WorkflowViewerEmbed: ({
    threadId,
    workflowId,
  }: {
    threadId?: string;
    workflowId?: string;
  }) => (
    <div data-testid="workflow-embed">
      {threadId}:{workflowId ?? "list"}
    </div>
  ),
}));

function renderOverlay(initialPath: string) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route
          path="/chat/:thread_id"
          element={
            <>
              <div data-testid="chat-dom">chat stays mounted</div>
              <Outlet />
            </>
          }
        >
          <Route path="task-detail" element={<TaskDetailOverlayPage />} />
          <Route path="task-detail/files/:artifact_id" element={<TaskDetailOverlayPage />} />
          <Route path="agent-workflows" element={<TaskDetailOverlayPage />} />
          <Route path="agent-workflows/:workflow_id" element={<TaskDetailOverlayPage />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe("TaskDetailOverlayPage", () => {
  beforeEach(() => {
    api.fetchThreadArtifacts.mockReset();
    api.fetchThreadArtifactContent.mockReset();
    api.fetchThreadArtifacts.mockResolvedValue({
      thread_id: "thread-aaaaaaaaaaaa",
      diagnostics: [],
      files: [
        {
          artifact_id: "bWFuaWZlc3QuanNvbg",
          path: "manifest.json",
          kind: "json",
          title: "manifest.json",
          available: true,
        },
        {
          artifact_id: "dGhyZWFkLWFhYWFhYWFhYWFhYS5qc29ubA",
          path: "thread-aaaaaaaaaaaa.jsonl",
          kind: "jsonl",
          title: "thread-aaaaaaaaaaaa.jsonl",
          available: true,
          record_count: 1,
        },
      ],
    });
    api.fetchThreadArtifactContent.mockResolvedValue({
      artifact_id: "bWFuaWZlc3QuanNvbg",
      path: "manifest.json",
      kind: "json",
      title: "manifest.json",
      content: { run_count: 2 },
      truncated: false,
      diagnostics: [],
    });
  });

  it("opens above the chat DOM and renders the session file list", async () => {
    renderOverlay("/chat/thread-aaaaaaaaaaaa/task-detail");

    expect(screen.getByTestId("chat-dom")).toBeInTheDocument();
    const overlay = await screen.findByTestId("task-detail-overlay");
    expect(overlay).toHaveClass("absolute");
    expect(screen.getByRole("button", { name: "返回对话" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "会话内容" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(await screen.findByText("manifest.json")).toBeInTheDocument();
    expect(await screen.findByText(/run_count/)).toBeInTheDocument();

    await waitFor(() => {
      expect(api.fetchThreadArtifacts).toHaveBeenCalledWith("thread-aaaaaaaaaaaa");
      expect(api.fetchThreadArtifactContent).toHaveBeenCalledWith({
        threadId: "thread-aaaaaaaaaaaa",
        artifactId: "bWFuaWZlc3QuanNvbg",
      });
    });
  });

  it("returns to the thread chat without relying on browser history", async () => {
    renderOverlay("/chat/thread-aaaaaaaaaaaa/task-detail");

    expect(await screen.findByTestId("task-detail-overlay")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "返回对话" }));

    await waitFor(() => {
      expect(screen.queryByTestId("task-detail-overlay")).toBeNull();
    });
    expect(screen.getByTestId("chat-dom")).toBeInTheDocument();
  });

  it("deep links agent workflows into the Workflows tab", async () => {
    renderOverlay("/chat/thread-aaaaaaaaaaaa/agent-workflows/wf-review");

    expect(screen.getByTestId("chat-dom")).toBeInTheDocument();
    expect(await screen.findByTestId("task-detail-overlay")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Workflows" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByTestId("workflow-embed")).toHaveTextContent(
      "thread-aaaaaaaaaaaa:wf-review",
    );
  });
});
