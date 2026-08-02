import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ThreadTaskProgressPopover } from "@/components/ThreadTaskProgressPopover";
import { useThreadSubAgents } from "@/hooks/useThreadSubAgents";
import { useThreadTaskProgress } from "@/hooks/useThreadTaskProgress";
import { useThreadWorkflowHistory } from "@/hooks/useThreadWorkflowHistory";
import type {
  ThreadTaskProgressSnapshot,
  ThreadTaskProgressViewModel,
} from "@/protocol";

vi.mock("@/hooks/useThreadTaskProgress", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/hooks/useThreadTaskProgress")>();
  return { ...actual, useThreadTaskProgress: vi.fn() };
});
vi.mock("@/hooks/useThreadWorkflowHistory", () => ({
  useThreadWorkflowHistory: vi.fn(),
}));
vi.mock("@/hooks/useThreadSubAgents", () => ({ useThreadSubAgents: vi.fn() }));

const mockUseThreadTaskProgress = vi.mocked(useThreadTaskProgress);
const mockUseThreadWorkflowHistory = vi.mocked(useThreadWorkflowHistory);
const mockUseThreadSubAgents = vi.mocked(useThreadSubAgents);

const snapshot: ThreadTaskProgressSnapshot = {
  schema_version: 2,
  session_id: "thread-1",
  workflow_id: "wf-current",
  title: "发布检查",
  control_mode: "llm_steps",
  updated_at_ms: 1781190000000,
  tasks: [
    {
      task_id: "verify",
      task_run_id: "001-verify",
      desc: "验证发布",
      depends_on: [],
      status: "failed",
      display_order: 0,
      error_message: "检查失败",
      updated_at_ms: 1781190000000,
    },
  ],
  counts: {
    pending: 0,
    in_progress: 0,
    completed: 0,
    failed: 1,
    cancelled: 0,
    total: 1,
  },
};

const viewModel: ThreadTaskProgressViewModel = {
  title: "进度",
  variant: "compact_checklist",
  empty: { title: "暂无任务进度", desc: "当前 thread 还没有可展示的 checklist。" },
  items: [
    {
      key: "wf-current:verify",
      task_id: "verify",
      desc: "验证发布",
      status: "failed",
      status_label: "失败",
      icon_variant: "error_circle",
      order: 0,
      aria_label: "失败：验证发布",
    },
  ],
};

function setProgress(
  nextSnapshot: ThreadTaskProgressSnapshot | null = snapshot,
  nextViewModel: ThreadTaskProgressViewModel = viewModel,
) {
  mockUseThreadTaskProgress.mockReturnValue({
    snapshot: nextSnapshot,
    viewModel: nextViewModel,
    isLoading: false,
    error: null,
    refresh: vi.fn(),
  });
}

describe("ThreadTaskProgressPopover", () => {
  beforeEach(() => {
    mockUseThreadTaskProgress.mockReset();
    mockUseThreadWorkflowHistory.mockReset();
    mockUseThreadSubAgents.mockReset();
    setProgress();
    mockUseThreadWorkflowHistory.mockReturnValue({
      items: [], isLoading: false, error: null, refresh: vi.fn(),
    });
    mockUseThreadSubAgents.mockReturnValue({
      items: [], isLoading: false, error: null, refresh: vi.fn(),
    });
  });

  it("renders the foreground title and failed task icon from v2 snapshot", () => {
    render(<ThreadTaskProgressPopover threadId="thread-1" open />);

    expect(screen.getByText("发布检查 · 0/1 已完成")).toBeInTheDocument();
    expect(screen.getByText("验证发布")).toBeInTheDocument();
    expect(screen.getByText("失败")).toBeInTheDocument();
    expect(document.querySelector('[data-icon-variant="error_circle"]')).not.toBeNull();
  });

  it("uses the foreground workflow id to exclude only current history", () => {
    mockUseThreadWorkflowHistory.mockReturnValue({
      items: [
        { workflow_id: "wf-current", title: "当前计划", status: "running", status_label: "进行中", started_at: null, finished_at: null },
        { workflow_id: "wf-old", title: "历史计划", status: "completed", status_label: "已完成", started_at: null, finished_at: null },
      ],
      isLoading: false,
      error: null,
      refresh: vi.fn(),
    });
    render(<ThreadTaskProgressPopover threadId="thread-1" open />);

    expect(screen.queryByText("当前计划")).not.toBeInTheDocument();
    expect(screen.getByText("历史计划")).toBeInTheDocument();
  });

  it("opens automatically for an unfinished foreground workflow", () => {
    const pendingSnapshot: ThreadTaskProgressSnapshot = {
      ...snapshot,
      tasks: [{ ...snapshot.tasks[0], status: "pending", error_message: null }],
      counts: { ...snapshot.counts, pending: 1, failed: 0 },
    };
    const pendingViewModel: ThreadTaskProgressViewModel = {
      ...viewModel,
      items: [{ ...viewModel.items[0], status: "pending", status_label: "未完成", icon_variant: "ring", aria_label: "未完成：验证发布" }],
    };
    setProgress(pendingSnapshot, pendingViewModel);

    render(<ThreadTaskProgressPopover threadId="thread-1" />);

    expect(screen.getByRole("dialog", { name: "当前 thread 任务进度" })).toBeInTheDocument();
  });
});
