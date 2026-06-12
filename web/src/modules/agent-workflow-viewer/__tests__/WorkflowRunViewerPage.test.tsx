import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { WorkflowRunViewerPage } from "../WorkflowRunViewerPage";
import { useAgentWorkflowViewerStore } from "../store";

const api = vi.hoisted(() => ({
  fetchAgentWorkflows: vi.fn(),
  fetchAgentWorkflowDetail: vi.fn(),
  fetchAgentWorkflowConversation: vi.fn(),
  fetchAgentWorkflowArtifact: vi.fn(),
  fetchThreadUsage: vi.fn(),
}));

vi.mock("../api", () => api);

const workflowItem = {
  workflow_id: "wf-review",
  thread_id: "thread-test",
  mode: "roundtable_review",
  status: "completed",
  desc: "梳理现有 thread/session 路径与顶部工具栏结构",
  title: "梳理现有 thread/session 路径与顶部工具栏结构",
  started_at: "2026-06-11T01:00:00Z",
  finished_at: "2026-06-11T01:02:00Z",
  report_count: 0,
  has_mode_panel: true,
  usage: {
    source: "fixture",
    totals: {
      total_input_tokens: 10,
      total_output_tokens: 20,
      total_tokens: 30,
    },
    provider_totals: {},
    records: [],
    diagnostics: [],
  },
  diagnostics: [],
};

const workflowDetail = {
  item: workflowItem,
  timeline: Array.from({ length: 8 }, (_, index) => ({
    event_id: `ev-${index}`,
    timestamp: `2026-06-11T01:00:0${index}Z`,
    label: `event ${index}`,
    action: "audit",
    payload: { index },
  })),
  flow_nodes: [],
  flow_edges: [],
  reports: [],
  panels: [
    {
      panel_id: "review-board",
      title: "Review Board",
      kind: "review_board",
      available: true,
      payload: {
        topic: "fixture",
        claim_count: 2,
        rebuttal_count: 1,
        context: "review context",
        sources: "review sources",
        consensus: "review consensus",
        final_report: "review final report",
        claims: [
          {
            claim_id: "C-001",
            agent: "diplomacy",
            severity: "P1",
            confidence: "0.8",
            claim: "claim one",
          },
          {
            claim_id: "C-002",
            agent: "risk",
            severity: "P2",
            claim: "claim two",
          },
        ],
        rebuttals: [
          {
            comment_id: "R-001",
            agent: "arbiter",
            severity: "P1",
            comment: "rebuttal one",
          },
        ],
      },
      diagnostics: [],
    },
  ],
  artifacts: [],
  usage: workflowItem.usage,
  diagnostics: [],
};

function renderViewer() {
  return render(
    <MemoryRouter
      initialEntries={["/chat/thread-test/agent-workflows/wf-review"]}
    >
      <Routes>
        <Route
          path="/chat/:thread_id/agent-workflows/:workflow_id"
          element={<WorkflowRunViewerPage />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe("WorkflowRunViewerPage review board UX", () => {
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
    api.fetchAgentWorkflows.mockResolvedValue({
      thread_id: "thread-test",
      workflows: [workflowItem],
    });
    api.fetchAgentWorkflowDetail.mockResolvedValue(workflowDetail);
    api.fetchThreadUsage.mockResolvedValue({ usage: null });
  });

  it("renders collapsible review board blocks with scrollable records and timeline", async () => {
    const { container } = renderViewer();

    expect(
      await screen.findByText(
        "子 agent 提出的结构化主张、证据、风险和建议，逐条来自 claims.jsonl。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getAllByText("梳理现有 thread/session 路径与顶部工具栏结构").length,
    ).toBeGreaterThanOrEqual(1);
    expect(
      screen.getByText("交叉质询阶段的评论、反驳和证据追问，逐条来自 rebuttals.jsonl。"),
    ).toBeInTheDocument();

    const details = container.querySelectorAll("details");
    expect(details.length).toBeGreaterThanOrEqual(6);

    const claims = screen.getByTestId("workflow-claims-list");
    expect(claims).toHaveClass("overflow-y-auto");
    expect(within(claims).getByText("#1 · C-001")).toBeInTheDocument();
    expect(within(claims).getByText("#2 · C-002")).toBeInTheDocument();

    const rebuttals = screen.getByTestId("workflow-rebuttals-list");
    expect(rebuttals).toHaveClass("overflow-y-auto");
    expect(within(rebuttals).getByText("#1 · R-001")).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByTestId("workflow-timeline-scroll")).toHaveClass(
        "overflow-y-auto",
      );
    });
  });
});
