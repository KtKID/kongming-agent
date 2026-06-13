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
  reports: [
    {
      task_run_id: "001-reviewer",
      task_id: "reviewer",
      task_name: "Reviewer",
      status: "completed",
      summary: "done",
      error_message: null,
      report_path: "reports/001-reviewer.json",
      working_dir: "/tmp/work",
      session_id: "subagent-reviewer",
      run_id: "run-reviewer-1",
      reported_at: "2026-06-11T01:00:03Z",
      usage: { total_tokens: 42 },
      conversation_available: true,
      conversation_source: "subagent-reviewer.jsonl",
      snapshot: {
        task_id: "reviewer",
        task_name: "Reviewer",
        working_dir: "/tmp/work",
      },
      activity_events: [
        {
          activity_id: "audit-1",
          activity_type: "created",
          ts: "2026-06-11T01:00:01Z",
          title: "子 agent 已创建",
          task_run_id: "001-reviewer",
          task_id: "reviewer",
          session_id: "subagent-reviewer",
          run_id: null,
          status: null,
          summary: "Reviewer · /tmp/work",
          source: "audit.jsonl",
          source_action: "subagent_created",
          payload: { task_run_id: "001-reviewer", working_dir: "/tmp/work" },
        },
        {
          activity_id: "audit-3",
          activity_type: "reported",
          ts: "2026-06-11T01:00:03Z",
          title: "报告已写入",
          task_run_id: "001-reviewer",
          task_id: "reviewer",
          session_id: "subagent-reviewer",
          run_id: "run-reviewer-1",
          status: "completed",
          summary: "reports/001-reviewer.json",
          source: "audit.jsonl",
          source_action: "subagent_reported",
          payload: { report_path: "reports/001-reviewer.json" },
        },
      ],
      diagnostics: [],
    },
  ],
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
    api.fetchAgentWorkflowConversation.mockResolvedValue({
      thread_id: "thread-test",
      workflow_id: "wf-review",
      task_run_id: "001-reviewer",
      child_session_id: "subagent-reviewer",
      source_path: "subagent-reviewer.jsonl",
      messages: [
        {
          record_index: 0,
          role: "assistant",
          content: "conversation done",
          created_at: "2026-06-11T01:00:04Z",
          message_type: null,
          tool_calls: [],
          usage: null,
          raw: {},
        },
      ],
      next_cursor: null,
      diagnostics: [],
    });
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

  it("renders subagent snapshot conversation and activity timeline", async () => {
    renderViewer();

    expect(await screen.findByTestId("workflow-task-snapshot")).toBeInTheDocument();
    expect(screen.getByText("Task Snapshot")).toBeInTheDocument();
    expect(screen.getByText("Conversation · subagent-reviewer")).toBeInTheDocument();
    expect(await screen.findByText("conversation done")).toBeInTheDocument();

    const timeline = screen.getByTestId("workflow-activity-timeline");
    expect(within(timeline).getByText("Activity Timeline")).toBeInTheDocument();
    expect(within(timeline).getByText("子 agent 已创建")).toBeInTheDocument();
    expect(within(timeline).getByText("报告已写入")).toBeInTheDocument();
    expect(within(timeline).getByText("reports/001-reviewer.json")).toBeInTheDocument();
  });

  it("renders empty activity state beside conversation panel", async () => {
    api.fetchAgentWorkflowDetail.mockResolvedValue({
      ...workflowDetail,
      reports: [
        {
          ...workflowDetail.reports[0],
          activity_events: [],
        },
      ],
    });

    renderViewer();

    expect(await screen.findByTestId("workflow-activity-timeline")).toBeInTheDocument();
    expect(screen.getByText("暂无行为事件")).toBeInTheDocument();
    expect(await screen.findByText("conversation done")).toBeInTheDocument();
  });
});
