import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ThreadTaskProgressPopover } from "@/components/ThreadTaskProgressPopover";
import { useThreadSubAgents } from "@/hooks/useThreadSubAgents";
import { useThreadTaskProgress } from "@/hooks/useThreadTaskProgress";
import { useThreadWorkflowHistory } from "@/hooks/useThreadWorkflowHistory";
import type {
  ThreadSubAgentDisplayItem,
  ThreadTaskProgressSnapshot,
  ThreadTaskProgressViewModel,
} from "@/protocol";

vi.mock("@/hooks/useThreadTaskProgress", () => ({
  useThreadTaskProgress: vi.fn(),
}));

vi.mock("@/hooks/useThreadWorkflowHistory", () => ({
  useThreadWorkflowHistory: vi.fn(),
}));

vi.mock("@/hooks/useThreadSubAgents", () => ({
  useThreadSubAgents: vi.fn(),
}));

const mockUseThreadTaskProgress = vi.mocked(useThreadTaskProgress);
const mockUseThreadWorkflowHistory = vi.mocked(useThreadWorkflowHistory);
const mockUseThreadSubAgents = vi.mocked(useThreadSubAgents);

const viewModel: ThreadTaskProgressViewModel = {
  title: "进度",
  variant: "compact_checklist",
  empty: {
    title: "暂无任务进度",
    desc: "当前 thread 还没有可展示的 checklist。",
  },
  items: [
    {
      key: "wf:1",
      orchestration_task_id: "wf:1",
      task_id: "contract",
      desc: "梳理接口合同",
      status: "completed",
      status_label: "已完成",
      icon_variant: "check_circle",
      order: 0,
      aria_label: "已完成：梳理接口合同",
    },
    {
      key: "wf:2",
      orchestration_task_id: "wf:2",
      task_id: "implement",
      desc: "实现顶部进度入口与 popover UI",
      status: "in_progress",
      status_label: "进行中",
      icon_variant: "active_ring",
      order: 1,
      aria_label: "进行中：实现顶部进度入口与 popover UI",
    },
    {
      key: "wf:3",
      orchestration_task_id: "wf:3",
      task_id: "verify",
      desc: "补充移动端工具入口测试",
      status: "pending",
      status_label: "未完成",
      icon_variant: "ring",
      order: 2,
      aria_label: "未完成：补充移动端工具入口测试",
    },
  ],
};

const snapshot: ThreadTaskProgressSnapshot = {
  schema_version: 1,
  session_id: "thread-1",
  updated_at_ms: 1781190000000,
  source: "workflow",
  tasks: [],
  counts: {
    pending: 1,
    in_progress: 1,
    completed: 1,
    total: 3,
  },
};

function makeWorkflowSnapshot(
  status: "pending" | "in_progress" | "completed",
  updatedAtMs: number,
): ThreadTaskProgressSnapshot {
  const sourceStatus =
    status === "pending"
      ? "assigned"
      : status === "in_progress"
        ? "running"
        : "completed";
  return {
    schema_version: 1,
    session_id: "thread-1",
    updated_at_ms: updatedAtMs,
    source: "workflow",
    tasks: [
      {
        id: "wf:hi-1",
        orchestration_task_id: "wf:hi-1",
        workflow_id: "wf",
        task_id: "hi-1",
        task_run_id: "hi-1",
        desc: "hi-1",
        status,
        source_status: sourceStatus,
        error_message: null,
        display_order: 0,
        updated_at_ms: updatedAtMs,
      },
    ],
    counts: {
      pending: status === "pending" ? 1 : 0,
      in_progress: status === "in_progress" ? 1 : 0,
      completed: status === "completed" ? 1 : 0,
      total: 1,
    },
  };
}

function makeWorkflowViewModel(
  status: "pending" | "in_progress" | "completed",
): ThreadTaskProgressViewModel {
  const statusLabel =
    status === "pending"
      ? "未完成"
      : status === "in_progress"
        ? "进行中"
        : "已完成";
  const iconVariant =
    status === "pending"
      ? "ring"
      : status === "in_progress"
        ? "active_ring"
        : "check_circle";
  return {
    title: "进度",
    variant: "compact_checklist",
    empty: viewModel.empty,
    items: [
      {
        key: "wf:hi-1",
        orchestration_task_id: "wf:hi-1",
        task_id: "hi-1",
        desc: "hi-1",
        status,
        status_label: statusLabel,
        icon_variant: iconVariant,
        order: 0,
        aria_label: `${statusLabel}：hi-1`,
      },
    ],
  };
}

function makeSubAgent(
  overrides: Partial<ThreadSubAgentDisplayItem>,
): ThreadSubAgentDisplayItem {
  const name = overrides.name ?? "子智能体任务";
  const statusLabel = overrides.status_label ?? "运行中";
  return {
    key: overrides.key ?? name,
    name,
    status: overrides.status ?? "running",
    status_label: statusLabel,
    icon_variant: overrides.icon_variant ?? "running",
    is_active: overrides.is_active ?? true,
    source_label: overrides.source_label,
    started_at_ms: overrides.started_at_ms ?? 1781190000000,
    updated_at_ms: overrides.updated_at_ms ?? 1781190001000,
    aria_label: overrides.aria_label ?? `${statusLabel}：${name}`,
  };
}

describe("ThreadTaskProgressPopover", () => {
  beforeEach(() => {
    mockUseThreadTaskProgress.mockReset();
    mockUseThreadWorkflowHistory.mockReset();
    mockUseThreadSubAgents.mockReset();
    mockUseThreadTaskProgress.mockReturnValue({
      snapshot,
      viewModel,
      isLoading: false,
      error: null,
      refresh: vi.fn(),
    });
    mockUseThreadWorkflowHistory.mockReturnValue({
      items: [],
      isLoading: false,
      error: null,
      refresh: vi.fn(),
    });
    mockUseThreadSubAgents.mockReturnValue({
      items: [],
      isLoading: false,
      error: null,
      refresh: vi.fn(),
    });
  });

  it("renders compact checklist with desc, status labels, and icon variants", async () => {
    render(<ThreadTaskProgressPopover threadId="thread-1" />);

    await userEvent.click(screen.getByRole("button", { name: "进度" }));

    const dialog = screen.getByRole("dialog", { name: "当前 thread 任务进度" });
    expect(dialog).toBeInTheDocument();
    expect(dialog).toHaveClass("absolute");
    expect(dialog).not.toHaveClass("fixed");
    expect(screen.getByText("环境信息")).toBeInTheDocument();
    expect(screen.getByText("长任务")).toBeInTheDocument();
    expect(screen.getByText("子智能体")).toBeInTheDocument();
    expect(screen.getByText("历史任务")).toBeInTheDocument();
    expect(screen.getByText("暂无子智能体")).toBeInTheDocument();
    expect(dialog.textContent).toMatch(/长任务[\s\S]*子智能体[\s\S]*历史任务/);
    expect(screen.getByText("1/3 已完成")).toBeInTheDocument();
    expect(
      screen.getByRole("listitem", { name: "已完成：梳理接口合同" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("listitem", {
        name: "进行中：实现顶部进度入口与 popover UI",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("listitem", {
        name: "未完成：补充移动端工具入口测试",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("已完成")).toBeInTheDocument();
    expect(screen.getByText("进行中")).toBeInTheDocument();
    expect(screen.getByText("未完成")).toBeInTheDocument();
    expect(
      document.querySelector('[data-icon-variant="check_circle"]'),
    ).toBeInTheDocument();
    expect(
      document.querySelector('[data-icon-variant="active_ring"]'),
    ).toBeInTheDocument();
    expect(
      document.querySelector('[data-icon-variant="ring"]'),
    ).toBeInTheDocument();
  });

  it("mobile mode keeps the panel fixed near the viewport top", async () => {
    render(<ThreadTaskProgressPopover threadId="thread-1" mobileMode />);

    await userEvent.click(screen.getByRole("button", { name: "进度" }));

    const dialog = screen.getByRole("dialog", { name: "当前 thread 任务进度" });
    expect(dialog).toHaveClass("fixed");
    expect(dialog).toHaveClass("top-14");
    expect(dialog).not.toHaveClass("absolute");
  });

  it("shows an empty state", async () => {
    mockUseThreadTaskProgress.mockReturnValue({
      snapshot: {
        ...snapshot,
        counts: { pending: 0, in_progress: 0, completed: 0, total: 0 },
      },
      viewModel: { ...viewModel, items: [] },
      isLoading: false,
      error: null,
      refresh: vi.fn(),
    });

    render(<ThreadTaskProgressPopover threadId="thread-1" />);

    await userEvent.click(screen.getByRole("button", { name: "进度" }));

    expect(screen.getByText("暂无任务进度")).toBeInTheDocument();
    expect(
      screen.getByText("当前 thread 还没有可展示的 checklist。"),
    ).toBeInTheDocument();
    expect(screen.getByText("暂无子智能体")).toBeInTheDocument();
  });

  it("renders subagents with status icons and source labels", async () => {
    mockUseThreadSubAgents.mockReturnValue({
      items: [
        makeSubAgent({
          key: "agent-running",
          name: "调研后端合同",
          status: "running",
          status_label: "运行中",
          icon_variant: "running",
          is_active: true,
          source_label: "chat",
          aria_label: "运行中：调研后端合同",
        }),
        makeSubAgent({
          key: "agent-completed",
          name: "汇总 workflow 事件",
          status: "completed",
          status_label: "已完成",
          icon_variant: "success",
          is_active: false,
          source_label: "wf-review",
          aria_label: "已完成：汇总 workflow 事件",
        }),
        makeSubAgent({
          key: "agent-failed",
          name: "验证异常路径",
          status: "failed",
          status_label: "失败",
          icon_variant: "error",
          is_active: false,
          source_label: "wf-review",
          aria_label: "失败：验证异常路径",
        }),
      ],
      isLoading: false,
      error: null,
      refresh: vi.fn(),
    });

    render(<ThreadTaskProgressPopover threadId="thread-1" />);

    const dialog = await screen.findByRole("dialog", {
      name: "当前 thread 任务进度",
    });
    expect(dialog.textContent).toMatch(/长任务[\s\S]*子智能体[\s\S]*历史任务/);
    expect(
      screen.getByRole("listitem", { name: "运行中：调研后端合同" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("listitem", { name: "已完成：汇总 workflow 事件" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("listitem", { name: "失败：验证异常路径" }),
    ).toBeInTheDocument();
    expect(screen.getByText("chat")).toBeInTheDocument();
    expect(screen.getAllByText("wf-review")).toHaveLength(2);
    expect(
      document.querySelector('[data-subagent-result="running"]'),
    ).toBeInTheDocument();
    expect(
      document.querySelector('[data-subagent-result="success"]'),
    ).toBeInTheDocument();
    expect(
      document.querySelector('[data-subagent-result="error"]'),
    ).toBeInTheDocument();
  });

  it("shows an error state and refresh action", async () => {
    const refresh = vi.fn();
    const refreshHistory = vi.fn();
    const refreshSubAgents = vi.fn();
    mockUseThreadTaskProgress.mockReturnValue({
      snapshot: null,
      viewModel: { ...viewModel, items: [] },
      isLoading: false,
      error: "server failed",
      refresh,
    });
    mockUseThreadWorkflowHistory.mockReturnValue({
      items: [],
      isLoading: false,
      error: null,
      refresh: refreshHistory,
    });
    mockUseThreadSubAgents.mockReturnValue({
      items: [],
      isLoading: false,
      error: null,
      refresh: refreshSubAgents,
    });

    render(<ThreadTaskProgressPopover threadId="thread-1" />);

    await userEvent.click(screen.getByRole("button", { name: "进度" }));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "读取任务进度失败：server failed",
    );

    await userEvent.click(screen.getByRole("button", { name: "刷新任务进度" }));

    expect(refresh).toHaveBeenCalledTimes(1);
    expect(refreshSubAgents).toHaveBeenCalledTimes(1);
    expect(refreshHistory).toHaveBeenCalledTimes(1);
  });

  it("disables the trigger without a thread id", () => {
    render(<ThreadTaskProgressPopover />);

    expect(screen.getByRole("button", { name: "进度" })).toBeDisabled();
  });

  it("supports a custom rail trigger and panel placement classes", async () => {
    render(
      <ThreadTaskProgressPopover
        threadId="thread-1"
        trigger={({ open, disabled, onClick }) => (
          <button
            type="button"
            data-open={open ? "true" : "false"}
            disabled={disabled}
            onClick={onClick}
          >
            rail progress
          </button>
        )}
        panelClassName="left-[calc(100%+0.75rem)] right-auto top-0"
      />,
    );

    const trigger = screen.getByRole("button", { name: "rail progress" });
    expect(trigger).toHaveAttribute("data-open", "false");

    await userEvent.click(trigger);

    expect(trigger).toHaveAttribute("data-open", "true");
    expect(
      screen.getByRole("dialog", { name: "当前 thread 任务进度" }),
    ).toHaveClass("left-[calc(100%+0.75rem)]", "right-auto", "top-0");
  });

  it("keeps polling while the popover is closed", () => {
    render(<ThreadTaskProgressPopover threadId="thread-1" />);

    expect(mockUseThreadTaskProgress).toHaveBeenLastCalledWith("thread-1", {
      enabled: true,
    });
    expect(mockUseThreadWorkflowHistory).toHaveBeenLastCalledWith("thread-1", {
      enabled: false,
    });
    expect(mockUseThreadSubAgents).toHaveBeenLastCalledWith("thread-1", {
      enabled: true,
    });
    expect(
      screen.queryByRole("dialog", { name: "当前 thread 任务进度" }),
    ).not.toBeInTheDocument();
  });

  it("renders current workflow title beside the current task count", async () => {
    const currentSnapshot: ThreadTaskProgressSnapshot = {
      ...snapshot,
      tasks: [
        {
          id: "wf-current:hi-1",
          orchestration_task_id: "wf-current:hi-1",
          workflow_id: "wf-current",
          task_id: "hi-1",
          task_run_id: "hi-1",
          desc: "当前子任务",
          status: "completed",
          display_order: 0,
        },
      ],
      counts: { pending: 0, in_progress: 0, completed: 1, total: 1 },
    };
    mockUseThreadTaskProgress.mockReturnValue({
      snapshot: currentSnapshot,
      viewModel: {
        ...viewModel,
        items: [
          {
            key: "wf-current:hi-1",
            orchestration_task_id: "wf-current:hi-1",
            task_id: "hi-1",
            desc: "当前子任务",
            status: "completed",
            status_label: "已完成",
            icon_variant: "check_circle",
            order: 0,
            aria_label: "已完成：当前子任务",
          },
        ],
      },
      isLoading: false,
      error: null,
      refresh: vi.fn(),
    });
    mockUseThreadWorkflowHistory.mockReturnValue({
      items: [
        {
          workflow_id: "wf-current",
          title: "最小 workflow 验证：3 个子 agent 并行返回随机数",
          status: "completed",
          status_label: "已完成",
          started_at: null,
          finished_at: null,
        },
      ],
      isLoading: false,
      error: null,
      refresh: vi.fn(),
    });

    render(<ThreadTaskProgressPopover threadId="thread-1" />);

    await userEvent.click(screen.getByRole("button", { name: "进度" }));

    expect(
      screen.getByTitle(
        "最小 workflow 验证：3 个子 agent 并行返回随机数 · 1/1 已完成",
      ),
    ).toBeInTheDocument();
  });

  it("renders historical workflows as compact task rows with result icons", async () => {
    mockUseThreadWorkflowHistory.mockReturnValue({
      items: [
        {
          workflow_id: "wf-old",
          title: "统一回复 hi",
          status: "completed",
          status_label: "已完成",
          started_at: "2026-06-12T01:00:00Z",
          finished_at: "2026-06-12T01:05:00Z",
        },
        {
          workflow_id: "wf-running",
          title: "运行中的 workflow",
          status: "running",
          status_label: "进行中",
          started_at: null,
          finished_at: null,
        },
        {
          workflow_id: "wf-failed",
          title: "失败的 workflow",
          status: "failed",
          status_label: "失败",
          started_at: null,
          finished_at: null,
        },
      ],
      isLoading: false,
      error: null,
      refresh: vi.fn(),
    });

    render(<ThreadTaskProgressPopover threadId="thread-1" />);

    await userEvent.click(screen.getByRole("button", { name: "进度" }));

    expect(
      screen.getByRole("listitem", { name: "已完成：统一回复 hi" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("listitem", { name: "进行中：运行中的 workflow" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("listitem", { name: "失败：失败的 workflow" }),
    ).toBeInTheDocument();
    expect(
      document.querySelector('[data-workflow-result="success"]'),
    ).toBeInTheDocument();
    expect(
      document.querySelector('[data-workflow-result="warning"]'),
    ).toBeInTheDocument();
    expect(
      document.querySelector('[data-workflow-result="error"]'),
    ).toBeInTheDocument();
    expect(screen.queryByText("失败")).not.toBeInTheDocument();
  });

  it("filters current workflow from historical workflows", async () => {
    const currentSnapshot: ThreadTaskProgressSnapshot = {
      ...snapshot,
      tasks: [
        {
          id: "wf-current:hi-1",
          orchestration_task_id: "wf-current:hi-1",
          workflow_id: "wf-current",
          task_id: "hi-1",
          task_run_id: "hi-1",
          desc: "当前子任务",
          status: "completed",
          display_order: 0,
        },
      ],
      counts: { pending: 0, in_progress: 0, completed: 1, total: 1 },
    };
    mockUseThreadTaskProgress.mockReturnValue({
      snapshot: currentSnapshot,
      viewModel: {
        ...viewModel,
        items: [
          {
            key: "wf-current:hi-1",
            orchestration_task_id: "wf-current:hi-1",
            task_id: "hi-1",
            desc: "当前子任务",
            status: "completed",
            status_label: "已完成",
            icon_variant: "check_circle",
            order: 0,
            aria_label: "已完成：当前子任务",
          },
        ],
      },
      isLoading: false,
      error: null,
      refresh: vi.fn(),
    });
    mockUseThreadWorkflowHistory.mockReturnValue({
      items: [
        {
          workflow_id: "wf-current",
          title: "当前 workflow",
          status: "running",
          status_label: "进行中",
          started_at: null,
          finished_at: null,
        },
        {
          workflow_id: "wf-old",
          title: "旧 workflow",
          status: "completed",
          status_label: "已完成",
          started_at: null,
          finished_at: null,
        },
      ],
      isLoading: false,
      error: null,
      refresh: vi.fn(),
    });

    render(<ThreadTaskProgressPopover threadId="thread-1" />);

    await userEvent.click(screen.getByRole("button", { name: "进度" }));

    expect(screen.getByTitle("当前 workflow · 1/1 已完成")).toBeInTheDocument();
    expect(
      screen.queryByRole("listitem", { name: "进行中：当前 workflow" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("旧 workflow")).toBeInTheDocument();
  });

  it("opens automatically when workflow progress appears", async () => {
    mockUseThreadTaskProgress.mockReturnValue({
      snapshot: null,
      viewModel: { ...viewModel, items: [] },
      isLoading: false,
      error: null,
      refresh: vi.fn(),
    });
    const { rerender } = render(
      <ThreadTaskProgressPopover threadId="thread-1" />,
    );

    expect(
      screen.queryByRole("dialog", { name: "当前 thread 任务进度" }),
    ).not.toBeInTheDocument();

    const runningSnapshot = makeWorkflowSnapshot("in_progress", 1781190001000);
    mockUseThreadTaskProgress.mockReturnValue({
      snapshot: runningSnapshot,
      viewModel: makeWorkflowViewModel("in_progress"),
      isLoading: false,
      error: null,
      refresh: vi.fn(),
    });

    rerender(<ThreadTaskProgressPopover threadId="thread-1" />);

    await waitFor(() => {
      expect(
        screen.getByRole("dialog", { name: "当前 thread 任务进度" }),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByRole("listitem", { name: "进行中：hi-1" }),
    ).toBeInTheDocument();
  });

  it("opens automatically when an active subagent appears", async () => {
    mockUseThreadTaskProgress.mockReturnValue({
      snapshot: null,
      viewModel: { ...viewModel, items: [] },
      isLoading: false,
      error: null,
      refresh: vi.fn(),
    });
    mockUseThreadSubAgents.mockReturnValue({
      items: [],
      isLoading: false,
      error: null,
      refresh: vi.fn(),
    });
    const { rerender } = render(
      <ThreadTaskProgressPopover threadId="thread-1" />,
    );

    expect(
      screen.queryByRole("dialog", { name: "当前 thread 任务进度" }),
    ).not.toBeInTheDocument();

    mockUseThreadSubAgents.mockReturnValue({
      items: [
        makeSubAgent({
          key: "agent-running",
          name: "普通 chat 子任务",
          status: "running",
          status_label: "运行中",
          icon_variant: "running",
          is_active: true,
          source_label: "chat",
          aria_label: "运行中：普通 chat 子任务",
        }),
      ],
      isLoading: false,
      error: null,
      refresh: vi.fn(),
    });

    rerender(<ThreadTaskProgressPopover threadId="thread-1" />);

    await waitFor(() => {
      expect(
        screen.getByRole("dialog", { name: "当前 thread 任务进度" }),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByRole("listitem", { name: "运行中：普通 chat 子任务" }),
    ).toBeInTheDocument();
  });

  it("does not reopen for the same snapshot after close and reopens on completion", async () => {
    mockUseThreadTaskProgress.mockReturnValue({
      snapshot: null,
      viewModel: { ...viewModel, items: [] },
      isLoading: false,
      error: null,
      refresh: vi.fn(),
    });
    const { rerender } = render(
      <ThreadTaskProgressPopover threadId="thread-1" />,
    );

    const runningSnapshot = makeWorkflowSnapshot("in_progress", 1781190001000);
    mockUseThreadTaskProgress.mockReturnValue({
      snapshot: runningSnapshot,
      viewModel: makeWorkflowViewModel("in_progress"),
      isLoading: false,
      error: null,
      refresh: vi.fn(),
    });
    rerender(<ThreadTaskProgressPopover threadId="thread-1" />);

    await waitFor(() => {
      expect(
        screen.getByRole("dialog", { name: "当前 thread 任务进度" }),
      ).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(
      screen.queryByRole("dialog", { name: "当前 thread 任务进度" }),
    ).not.toBeInTheDocument();

    rerender(<ThreadTaskProgressPopover threadId="thread-1" />);
    expect(
      screen.queryByRole("dialog", { name: "当前 thread 任务进度" }),
    ).not.toBeInTheDocument();

    const completedSnapshot = makeWorkflowSnapshot("completed", 1781190002000);
    mockUseThreadTaskProgress.mockReturnValue({
      snapshot: completedSnapshot,
      viewModel: makeWorkflowViewModel("completed"),
      isLoading: false,
      error: null,
      refresh: vi.fn(),
    });
    rerender(<ThreadTaskProgressPopover threadId="thread-1" />);

    await waitFor(() => {
      expect(
        screen.getByRole("dialog", { name: "当前 thread 任务进度" }),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByRole("listitem", { name: "已完成：hi-1" }),
    ).toBeInTheDocument();
  });
});
