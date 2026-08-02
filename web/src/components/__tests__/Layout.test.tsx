import { useMemo, type ComponentProps } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Layout } from "@/components/Layout";
import {
  useRegisterWebShellRailItems,
  type WebShellRailItem,
} from "@/components/web-shell-rail";
import { useApprovalInboxStore } from "@/features/approval-inbox";
import { resetSender } from "@/features/approval-inbox/senderRef";
import { apiGet, apiGetThreadTaskProgress, apiPost } from "@/lib/api";
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
const mockApiGet = vi.mocked(apiGet);
const mockApiPost = vi.mocked(apiPost);

function TestRailIcon({ className }: ComponentProps<"span">) {
  return <span className={className} />;
}

function XspaceCapabilityRailItems() {
  const items = useMemo<WebShellRailItem[]>(
    () => [
      {
        id: "xspace-capability-test",
        scope: "global",
        priority: "p1",
        label: "XSpace capability",
        icon: TestRailIcon,
        available: true,
        requiredCapability: "xspaceHost",
      },
      {
        id: "native-dialog-capability-test",
        scope: "global",
        priority: "p1",
        label: "Native dialog capability",
        icon: TestRailIcon,
        available: true,
        requiredCapability: "nativeFileDialog",
      },
    ],
    [],
  );
  useRegisterWebShellRailItems("layout-capability-test", items);
  return <div data-testid="capability-registration" />;
}

const emptyTaskProgressSnapshot: ThreadTaskProgressSnapshot = {
  schema_version: 2,
  session_id: "thread-empty",
  workflow_id: null,
  title: null,
  control_mode: null,
  updated_at_ms: 1781190000000,
  tasks: [],
  counts: {
    pending: 0,
    in_progress: 0,
    completed: 0,
    failed: 0,
    cancelled: 0,
    total: 0,
  },
};

const mobileTaskProgressSnapshot: ThreadTaskProgressSnapshot = {
  schema_version: 2,
  session_id: "thread-mobile123456",
  workflow_id: "wf-mobile",
  title: "移动端计划",
  control_mode: "llm_steps",
  updated_at_ms: 1781190000000,
  tasks: [
    {
      task_id: "task-1",
      task_run_id: "task-1",
      desc: "移动端进度任务",
      depends_on: [],
      status: "in_progress",
      display_order: 0,
      error_message: null,
      updated_at_ms: 1781190000000,
    },
  ],
  counts: {
    pending: 0,
    in_progress: 1,
    completed: 0,
    failed: 0,
    cancelled: 0,
    total: 1,
  },
};

describe("Layout", () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    mockApiGet.mockResolvedValue([]);
    mockApiGetThreadTaskProgress.mockReset();
    mockApiGetThreadTaskProgress.mockResolvedValue(emptyTaskProgressSnapshot);
    mockApiPost.mockReset();
    mockApiPost.mockResolvedValue(undefined);
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
    expect(screen.getByRole("link", { name: /插件/ })).toHaveAttribute(
      "href",
      "/manage/plugins",
    );
    expect(screen.getByTestId("chat")).toBeInTheDocument();
    expect(screen.getByTestId("app-header").className).toContain("z-30");
    const rail = screen.getByTestId("web-shell-rail");
    expect(rail).toHaveAttribute("data-density", "desktop");
    expect(rail).toHaveStyle({ zIndex: "45", height: "420px" });

    fireEvent.mouseEnter(rail);

    expect(rail).toHaveAttribute("data-open", "true");
    expect(screen.getByTestId("web-shell-rail-item-manage")).toBeInTheDocument();
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
    expect(
      screen.getByTestId("web-shell-rail-item-thread-task-detail-route"),
    ).toHaveAttribute("href", "/chat/thread-abcdef123456/task-detail");
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
        initialEntries={["/chat/thread-route-only/task-detail"]}
      >
        <Routes>
          <Route element={<Layout />}>
            <Route
              path="/chat/:thread_id/task-detail"
              element={<div data-testid="workflow-page" />}
            />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText("Current thread")).toBeInTheDocument();
    expect(
      screen.getByTestId("web-shell-rail-item-thread-task-detail-route"),
    ).toHaveAttribute("href", "/chat/thread-route-only/task-detail");
    expect(screen.getByTestId("workflow-page")).toBeInTheDocument();
  });

  it("passes xspace client capabilities into registered rail items", async () => {
    mockApiGet.mockResolvedValue({
      host_environment: "xspace",
      capabilities: {
        xspace_host: true,
        native_file_dialog: false,
      },
      ws_heartbeat_interval_ms: 30_000,
      ws_heartbeat_background_interval_ms: 60_000,
      ws_heartbeat_timeout_ms: 10_000,
      ws_heartbeat_max_missed: 3,
      dashboard_poll_interval_seconds: 5,
      timezone: "UTC",
    });

    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/chat" element={<XspaceCapabilityRailItems />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    const rail = screen.getByTestId("web-shell-rail");
    fireEvent.mouseEnter(rail);

    expect(await screen.findByTestId("web-shell-rail-item-xspace-capability-test"))
      .toBeInTheDocument();
    expect(
      screen.queryByTestId("web-shell-rail-item-native-dialog-capability-test"),
    ).toBeNull();
  });

  it("shows the pet rail button only for xspace client config", async () => {
    mockApiGet.mockResolvedValue({
      host_environment: "xspace",
      capabilities: {
        xspace_host: true,
        native_file_dialog: true,
      },
      ws_heartbeat_interval_ms: 30_000,
      ws_heartbeat_background_interval_ms: 60_000,
      ws_heartbeat_timeout_ms: 10_000,
      ws_heartbeat_max_missed: 3,
      dashboard_poll_interval_seconds: 5,
      timezone: "UTC",
    });

    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/chat" element={<div data-testid="chat" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    const rail = screen.getByTestId("web-shell-rail");
    fireEvent.mouseEnter(rail);

    const petButton = await screen.findByTestId("web-shell-rail-item-pet");
    expect(petButton).toHaveAccessibleName("宠物");

    await userEvent.click(petButton);

    expect(mockApiPost).not.toHaveBeenCalled();
  });

  it("hides the pet rail button for browser client config", async () => {
    mockApiGet.mockResolvedValue({
      host_environment: "browser",
      capabilities: {
        xspace_host: false,
        native_file_dialog: false,
      },
      ws_heartbeat_interval_ms: 30_000,
      ws_heartbeat_background_interval_ms: 60_000,
      ws_heartbeat_timeout_ms: 10_000,
      ws_heartbeat_max_missed: 3,
      dashboard_poll_interval_seconds: 5,
      timezone: "UTC",
    });

    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/chat" element={<div data-testid="chat" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    const rail = screen.getByTestId("web-shell-rail");
    fireEvent.mouseEnter(rail);

    await waitFor(() =>
      expect(mockApiGet).toHaveBeenCalledWith("/api/config/client"),
    );

    expect(screen.queryByTestId("web-shell-rail-item-pet")).toBeNull();
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

    const rail = screen.getByTestId("web-shell-rail");
    expect(rail).toHaveAttribute("data-density", "compact");
    expect(rail).toHaveStyle({ height: "340px" });
    fireEvent.mouseEnter(rail);
    expect(screen.getByTestId("web-shell-rail-item-manage")).toBeInTheDocument();
    expect(
      screen.getByTestId("web-shell-rail-item-thread-task-detail-route"),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("web-shell-rail-item-theme")).toBeNull();
    expect(screen.queryByTestId("web-shell-rail-item-logout")).toBeNull();
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
    expect(screen.getByText("任务详情")).toBeInTheDocument();
    expect(screen.getByText("定时任务")).toBeInTheDocument();
    expect(screen.getByText("司天报告")).toBeInTheDocument();
    expect(screen.getByText("复制线程 ID")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("menuitem", { name: "进度" }));

    expect(await screen.findByText("移动端进度任务")).toBeInTheDocument();
    expect(screen.queryByText("Files")).toBeNull();
  });

  it("removes the rail on mobile widths and keeps the tools menu", async () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      writable: true,
      value: 640,
    });
    window.dispatchEvent(new Event("resize"));
    useThreadsStore.setState({
      threads: [
        {
          id: "thread-mobile-only",
          name: "Mobile Only",
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
      <MemoryRouter initialEntries={["/chat/thread-mobile-only"]}>
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

    await waitFor(() =>
      expect(mockApiGetThreadTaskProgress).toHaveBeenCalledWith(
        "thread-mobile-only",
      ),
    );
    expect(screen.queryByTestId("web-shell-rail")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Open tools menu" }),
    ).toBeInTheDocument();
  });
});
