import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Layout } from "@/components/Layout";
import { useApprovalInboxStore } from "@/features/approval-inbox";
import { resetSender } from "@/features/approval-inbox/senderRef";
import { apiGetThreadTaskProgress } from "@/lib/api";
import type { ThreadTaskProgressSnapshot } from "@/protocol";
import { useAuthStore } from "@/stores/auth";
import { useConnectionStatusStore } from "@/stores/connectionStatus";
import { useThreadsStore } from "@/stores/threads";

vi.mock("@/components/ThemeToggle", () => ({
  ThemeToggle: () => <button type="button">theme</button>,
}));

vi.mock("@/hooks/useHeartbeatConfig", () => ({
  useHeartbeatConfig: () => undefined,
}));

vi.mock("@/hooks/useThreadStatusWS", () => ({
  useThreadStatusWS: () => ({ state: "closed", latencyMs: null }),
}));

vi.mock("@/lib/api", () => ({
  apiPost: vi.fn().mockResolvedValue(undefined),
  apiGet: vi.fn().mockResolvedValue([]),
  apiGetThreadTaskProgress: vi.fn(),
  apiPatch: vi.fn().mockResolvedValue(undefined),
  apiDelete: vi.fn().mockResolvedValue(undefined),
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public errorCode: string,
      public detail: string,
    ) {
      super(detail);
    }
  },
  RateLimitedError: class RateLimitedError extends Error {
    constructor(
      public retryAfterSeconds: number,
      msg: string,
    ) {
      super(msg);
    }
  },
}));

const mockApiGetThreadTaskProgress = vi.mocked(apiGetThreadTaskProgress);

const emptyTaskProgressSnapshot: ThreadTaskProgressSnapshot = {
  schema_version: 1,
  session_id: "thread-empty",
  updated_at_ms: 1781190000000,
  source: "api",
  tasks: [],
  counts: {
    pending: 0,
    in_progress: 0,
    completed: 0,
    total: 0,
  },
};

const mobileTaskProgressSnapshot: ThreadTaskProgressSnapshot = {
  schema_version: 1,
  session_id: "thread-mobile123456",
  updated_at_ms: 1781190000000,
  source: "workflow",
  tasks: [
    {
      id: "wf:task-1",
      orchestration_task_id: "wf:task-1",
      task_id: "task-1",
      task_run_id: "task-1",
      desc: "移动端进度任务",
      status: "in_progress",
      display_order: 0,
    },
  ],
  counts: {
    pending: 0,
    in_progress: 1,
    completed: 0,
    total: 1,
  },
};

describe("Layout", () => {
  beforeEach(() => {
    mockApiGetThreadTaskProgress.mockReset();
    mockApiGetThreadTaskProgress.mockResolvedValue(emptyTaskProgressSnapshot);
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      writable: true,
      value: 1280,
    });
    window.dispatchEvent(new Event("resize"));
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
    useAuthStore.setState({ authenticated: true, _checked: true });
    useThreadsStore.setState({ threads: [], presets: [], loading: false });
    useConnectionStatusStore.setState({
      threadWsState: "closed",
      threadWsLatencyMs: null,
      threadWsActive: false,
      claudeWsState: "closed",
      claudeWsLatencyMs: null,
      claudeWsActive: false,
      statusWsState: "closed",
      statusWsLatencyMs: null,
    });
    useApprovalInboxStore.setState({ byRequestId: {} });
    resetSender();
  });

  it("renders logo, logout button, and outlet", () => {
    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/chat" element={<div data-testid="chat" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText("kongming")).toBeInTheDocument();
    expect(screen.getByLabelText("Logout")).toBeInTheDocument();
    expect(screen.getByTestId("chat")).toBeInTheDocument();
    expect(screen.getByTestId("app-header").className).toContain("z-30");
  });

  it("shows the manage title on /manage", () => {
    render(
      <MemoryRouter initialEntries={["/manage"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/manage" element={<div />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText("运行管理")).toBeInTheDocument();
  });

  it("shows Claude indicator only on Claude thread", () => {
    useThreadsStore.setState({
      threads: [
        {
          id: "thread-abcdef123456",
          name: "Claude Thread",
          preset_id: "",
          backend_kind: "claude_code",
          claude_thread_id: "sdk-1",
          codex_thread_id: "",
          cwd: "/workspace",
          created_at: 0,
          updated_at: 0,
          message_count: 0,
          is_pinned: false,
          is_archived: false,
          schema_version: 1,
        },
      ],
    });
    useConnectionStatusStore.setState({
      claudeWsState: "open",
      claudeWsLatencyMs: null,
      claudeWsActive: true,
      statusWsState: "open",
      statusWsLatencyMs: 4,
    });

    render(
      <MemoryRouter initialEntries={["/chat/thread-abcdef123456"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route
              path="/chat/:thread_id"
              element={<div data-testid="chat" />}
            />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText("Claude Thread")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Workflow" })).toHaveAttribute(
      "href",
      "/chat/thread-abcdef123456/agent-workflows",
    );
    expect(screen.getAllByTestId("connection-indicator")).toHaveLength(2);
    expect(screen.getByText("4ms")).toBeInTheDocument();
  });

  it("hides Claude indicator on generic threads", () => {
    useThreadsStore.setState({
      threads: [
        {
          id: "thread-generic123456",
          name: "Generic Thread",
          preset_id: "",
          backend_kind: "generic_chat",
          claude_thread_id: "",
          codex_thread_id: "",
          cwd: "/workspace",
          created_at: 0,
          updated_at: 0,
          message_count: 0,
          is_pinned: false,
          is_archived: false,
          schema_version: 1,
        },
      ],
    });
    useConnectionStatusStore.setState({
      claudeWsState: "open",
      claudeWsLatencyMs: null,
      claudeWsActive: true,
      statusWsState: "open",
      statusWsLatencyMs: 4,
    });

    render(
      <MemoryRouter initialEntries={["/chat/thread-generic123456"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route
              path="/chat/:thread_id"
              element={<div data-testid="chat" />}
            />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText("Generic Thread")).toBeInTheDocument();
    expect(screen.getAllByTestId("connection-indicator")).toHaveLength(1);
    expect(screen.getByText("4ms")).toBeInTheDocument();
  });

  it("shows workflow entry from route thread id before thread metadata loads", () => {
    render(
      <MemoryRouter
        initialEntries={["/chat/thread-route-only/agent-workflows"]}
      >
        <Routes>
          <Route element={<Layout />}>
            <Route
              path="/chat/:thread_id/agent-workflows"
              element={<div data-testid="workflow-page" />}
            />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText("Current thread")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Workflow" })).toHaveAttribute(
      "href",
      "/chat/thread-route-only/agent-workflows",
    );
    expect(screen.getByTestId("workflow-page")).toBeInTheDocument();
  });

  it("hides Claude indicator when Claude socket is inactive", () => {
    useThreadsStore.setState({
      threads: [
        {
          id: "thread-claude-inactive",
          name: "Claude Inactive",
          preset_id: "",
          backend_kind: "claude_code",
          claude_thread_id: "sdk-2",
          codex_thread_id: "",
          cwd: "/workspace",
          created_at: 0,
          updated_at: 0,
          message_count: 0,
          is_pinned: false,
          is_archived: false,
          schema_version: 1,
        },
      ],
    });
    useConnectionStatusStore.setState({
      claudeWsState: "open",
      claudeWsLatencyMs: null,
      claudeWsActive: false,
      statusWsState: "open",
      statusWsLatencyMs: 4,
    });

    render(
      <MemoryRouter initialEntries={["/chat/thread-claude-inactive"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route
              path="/chat/:thread_id"
              element={<div data-testid="chat" />}
            />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText("Claude Inactive")).toBeInTheDocument();
    expect(screen.getAllByTestId("connection-indicator")).toHaveLength(1);
    expect(screen.getByText("4ms")).toBeInTheDocument();
  });

  it("uses the tools menu on compact widths", async () => {
    mockApiGetThreadTaskProgress.mockResolvedValue(mobileTaskProgressSnapshot);
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      writable: true,
      value: 1024,
    });
    window.dispatchEvent(new Event("resize"));
    useThreadsStore.setState({
      threads: [
        {
          id: "thread-mobile123456",
          name: "Mobile Thread",
          preset_id: "",
          backend_kind: "generic_chat",
          claude_thread_id: "",
          codex_thread_id: "",
          cwd: "/workspace",
          created_at: 0,
          updated_at: 0,
          message_count: 0,
          is_pinned: false,
          is_archived: false,
          schema_version: 1,
        },
      ],
    });

    render(
      <MemoryRouter initialEntries={["/chat/thread-mobile123456"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route
              path="/chat/:thread_id"
              element={<div data-testid="chat" />}
            />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.queryByTestId("connection-indicator")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Open tools menu" }),
    ).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "Open tools menu" }),
    );

    expect(screen.getByText("Files")).toBeInTheDocument();
    expect(screen.getByText("Git")).toBeInTheDocument();
    expect(screen.getByText("Shell")).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "进度" })).toBeInTheDocument();
    expect(screen.getByText("Workflow")).toBeInTheDocument();
    expect(screen.getByText("定时任务")).toBeInTheDocument();
    expect(screen.getByText("司天报告")).toBeInTheDocument();
    expect(screen.getByText("复制线程 ID")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("menuitem", { name: "进度" }));

    expect(await screen.findByText("移动端进度任务")).toBeInTheDocument();
    expect(screen.queryByText("Files")).toBeNull();
  });
});
