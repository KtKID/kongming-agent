import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { ChatPage } from "@/pages/Chat";
import { Layout } from "@/components/Layout";
import { ChatManager } from "@/chat/ChatManager";
import {
  disposeTimelineStore,
  getTimelineStore,
  makeCronTimelineKey,
} from "@/chat/runtimeWiring";
import { useThreadsStore } from "@/stores/threads";
import { useChatStore } from "@/stores/chat";
import { useConnectionStatusStore } from "@/stores/connectionStatus";
import { useWorkspaceStore } from "@/stores/workspace";
import { useAuthStore } from "@/stores/auth";
import { useModelProvidersStore } from "@/modules/model-providers/store";
import { apiGet } from "@/lib/api";
import type { ThreadMetadataDTO } from "@/protocol";
import type { ConnectedModelFamily } from "@/modules/model-providers/types";

const networkMock = vi.hoisted(() => {
  type MessageListener = (frame: unknown) => void;
  type StateListener = (state: "closed" | "connecting" | "open" | "reconnecting" | "failed") => void;

  const messageListeners = new Set<MessageListener>();
  const stateListeners = new Set<StateListener>();
  let state: "closed" | "connecting" | "open" | "reconnecting" | "failed" = "open";
  const handle = {
    connId: "generic:thread-aaaaaaaaaaaa:conn-test",
    send: vi.fn(),
    close: vi.fn(),
    onMessage: vi.fn((cb: MessageListener) => {
      messageListeners.add(cb);
      return () => messageListeners.delete(cb);
    }),
    onState: vi.fn((cb: StateListener) => {
      stateListeners.add(cb);
      cb(state);
      return () => stateListeners.delete(cb);
    }),
  };
  return {
    handle,
    configure: vi.fn(),
    openChannel: vi.fn(() => handle),
    emitMessage: (frame: unknown) => {
      for (const cb of [...messageListeners]) cb(frame);
    },
    setState: (next: typeof state) => {
      state = next;
      for (const cb of [...stateListeners]) cb(next);
    },
    reset: () => {
      messageListeners.clear();
      stateListeners.clear();
      state = "open";
      handle.connId = "generic:thread-aaaaaaaaaaaa:conn-test";
      handle.send.mockReset();
      handle.close.mockReset();
      handle.onMessage.mockClear();
      handle.onState.mockClear();
      networkMock.configure.mockReset();
      networkMock.openChannel.mockClear();
    },
  };
});

const toastMock = vi.hoisted(() => ({
  error: vi.fn(),
  info: vi.fn(),
  success: vi.fn(),
  warning: vi.fn(),
}));

let heartbeatConfig:
  | {
      intervalMs: number;
      backgroundIntervalMs: number;
      timeoutMs: number;
      maxMissed: number;
    }
  | undefined = {
  intervalMs: 30_000,
  backgroundIntervalMs: 60_000,
  timeoutMs: 10_000,
  maxMissed: 3,
};

// ---------------------------------------------------------------------------
// 其他子组件 mock 掉，避免拖入 LeftSidebar / WorkspaceFiles / WhiteboardPanel /
// FileDrawer 等重组件，让 Chat 集成测试聚焦页面编排。
// ---------------------------------------------------------------------------

// whiteboard store：本测试不关心白板逻辑，且 ChatPage useEffect 挂载即触发
// fetchBoard()。真实 store 会调 apiGet → mapBoard，apiGet mock 返 `[]` 时
// dto.cards 是 undefined → mapBoard 抛 TypeError → unhandled rejection 让
// vitest exit 1。这里把整个 store hook 替换成静默 stub：selector 形式调用时
// 返回 fixed state，所有 action 都是 no-op vi.fn()。
//
// useWhiteboardStore 既以 selector 形式 `useWhiteboardStore((s) => s.x)` 被调，
// 也作为对象 `useWhiteboardStore.setState(...)` 被调（本测试不再用 setState
// 复位 whiteboard，因 mock 状态恒定）。下方实现兼顾两种用法。
vi.mock("@/stores/whiteboard", () => {
  const stubState = {
    currentThreadId: null,
    globalTitle: "Whiteboard",
    projectTitle: null,
    boardUpdatedAt: 0,
    cards: [] as unknown[],
    loading: false,
    selectedCardId: null,
    draggingCardId: null,
    resizingCardId: null,
    fetchBoard: vi.fn().mockResolvedValue(undefined),
    clearBoard: vi.fn(),
    createCard: vi.fn().mockResolvedValue(undefined),
    updateCardContentLocal: vi.fn(),
    saveCardContent: vi.fn().mockResolvedValue(undefined),
    updateCardMetaLocal: vi.fn(),
    updateCardLayoutLocal: vi.fn(),
    bringCardToFront: vi.fn(),
    toggleCollapsed: vi.fn(),
    deleteCard: vi.fn().mockResolvedValue(undefined),
  };
  const useWhiteboardStore = <T,>(selector?: (s: typeof stubState) => T) =>
    selector ? selector(stubState) : stubState;
  // zustand-like 静态属性兜底（测试代码若仍调 setState 不会 crash）
  (useWhiteboardStore as unknown as { setState: (...a: unknown[]) => void }).setState =
    () => undefined;
  (useWhiteboardStore as unknown as { getState: () => typeof stubState }).getState =
    () => stubState;
  return { useWhiteboardStore };
});

// 阻断网络相关 store action（默认会 fetchThreadUsage / fetchWorkspaceContext）
vi.mock("@/lib/api", () => ({
  apiPost: vi.fn().mockResolvedValue(undefined),
  apiGet: vi.fn().mockResolvedValue([]),
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
    constructor(public retryAfterSeconds: number, msg: string) {
      super(msg);
    }
  },
}));

vi.mock("@/network", () => ({
  networkManager: {
    configure: networkMock.configure,
    openChannel: networkMock.openChannel,
  },
}));

vi.mock("@/hooks/useHeartbeatConfig", () => ({
  useHeartbeatConfig: () => heartbeatConfig,
}));

vi.mock("@/hooks/useThreadStatusWS", () => ({
  useThreadStatusWS: () => ({ state: "closed", latencyMs: null }),
}));

vi.mock("sonner", () => ({
  toast: toastMock,
}));

vi.mock("@/components/ThemeToggle", () => ({
  ThemeToggle: () => <button type="button">theme</button>,
}));

vi.mock("@/features/approval-inbox", () => ({
  ApprovalToastQueue: () => null,
}));

vi.mock("@/features/thread-permissions", () => ({
  ThreadPermissionsManager: ({ threadId }: { threadId: string }) => (
    <div data-testid="thread-permissions-entry" data-thread-id={threadId} />
  ),
}));

vi.mock("@/modules/scheduler", () => {
  const schedulerState = { openDrawer: vi.fn() };
  return {
    SchedulerDrawerHost: () => null,
    SchedulerEntryButton: () => <button type="button">定时任务</button>,
    useSchedulerStore: <T,>(selector: (state: typeof schedulerState) => T) =>
      selector(schedulerState),
  };
});

vi.mock("@/modules/logs", () => {
  const logsState = { open: vi.fn() };
  return {
    LogViewerEntryButton: () => <button type="button">日志</button>,
    LogViewerOverlay: () => null,
    useLogViewerStore: <T,>(selector: (state: typeof logsState) => T) =>
      selector(logsState),
  };
});

vi.mock("@/modules/sitian", () => ({
  SitianReportDialog: () => null,
  SitianReportEntryButton: () => <button type="button">司天报告</button>,
  useSitian: () => ({ open: vi.fn().mockResolvedValue(undefined), loading: false }),
}));

// Composer mock：把 leftActions 透传出来给断言；不需要真实输入框
vi.mock("@/components/Composer", () => ({
  Composer: ({
    leftActions,
    modelSwitcher,
    onSubmit,
    disabled,
    restoreDraftToken,
    reasoningOptions,
    initialReasoningEffort,
  }: {
    leftActions?: ReactNode;
    modelSwitcher?: ReactNode;
    onSubmit?: (
      text: string,
      reasoningEffort: import("@/chat/types").ReasoningEffort | null,
    ) => void;
    disabled?: boolean;
    restoreDraftToken?: number | null;
    reasoningOptions?: import("@/chat/types").ReasoningEffort[];
    initialReasoningEffort?: import("@/chat/types").ReasoningEffort | null;
  }) => (
    <div
      data-testid="composer"
      data-disabled={disabled ? "1" : "0"}
      data-restore-draft-token={restoreDraftToken ?? ""}
      data-reasoning-options={reasoningOptions?.join("|") ?? ""}
      data-initial-reasoning-effort={initialReasoningEffort ?? ""}
    >
      <div data-testid="composer-left-actions">{leftActions ?? null}</div>
      <div data-testid="composer-model-switcher-slot">{modelSwitcher ?? null}</div>
      <button
        type="button"
        data-testid="composer-submit"
        disabled={disabled}
        onClick={() =>
          onSubmit?.("hello model", initialReasoningEffort ?? "high")
        }
      >
        submit
      </button>
      <button
        type="button"
        data-testid="composer-submit-none"
        disabled={disabled}
        onClick={() => onSubmit?.("hello model", "none")}
      >
        submit none
      </button>
    </div>
  ),
}));

// 其它视图 / 重组件：纯 stub
vi.mock("@/components/LeftSidebar", () => ({
  LeftSidebar: ({
    isOpen,
    compactMode,
    mobileMode,
  }: {
    isOpen?: boolean;
    compactMode?: boolean;
    mobileMode?: boolean;
  }) => (
    <aside
      className="z-20"
      data-testid="left-sidebar"
      data-open={isOpen ? "true" : "false"}
      data-compact={compactMode ? "true" : "false"}
      data-mobile={mobileMode ? "true" : "false"}
    />
  ),
}));
vi.mock("@/components/MessageList", () => ({
  MessageList: ({
    forkLineage,
  }: {
    forkLineage?: { parentThreadId: string; historyIndex: number } | null;
  }) => (
    <div
      data-fork-lineage-history-index={forkLineage?.historyIndex}
      data-fork-lineage-parent-id={forkLineage?.parentThreadId}
      data-testid="message-list"
    />
  ),
}));
vi.mock("@/components/ThreadTaskProgressPopover", async () => {
  const React = await vi.importActual<typeof import("react")>("react");
  type Trigger = (props: {
    open: boolean;
    disabled: boolean;
    onClick: () => void;
  }) => import("react").ReactNode;
  return {
    ThreadTaskProgressPopover: ({
      threadId,
      trigger,
    }: {
      threadId?: string;
      trigger?: Trigger;
    }) => {
      const [open, setOpen] = React.useState(false);
      const disabled = !threadId;
      if (trigger) {
        return (
          <span data-testid="thread-progress-trigger-host">
            {trigger({
              open,
              disabled,
              onClick: () => {
                if (!disabled) setOpen((current) => !current);
              },
            })}
            {open ? (
              <div data-testid="thread-progress-panel">任务进度 {threadId}</div>
            ) : null}
          </span>
        );
      }
      return (
        <button type="button" data-testid="thread-progress" data-thread-id={threadId}>
          进度
        </button>
      );
    },
  };
});
vi.mock("@/modules/scheduler/components/ThreadCronRunsPopover", async () => {
  const React = await vi.importActual<typeof import("react")>("react");
  type Trigger = (props: {
    open: boolean;
    disabled: boolean;
    onClick: () => void;
  }) => import("react").ReactNode;
  return {
    ThreadCronRunsPopover: ({
      threadId,
      taskId,
      activeRunId,
      trigger,
    }: {
      threadId?: string;
      taskId?: string | null;
      activeRunId?: string | null;
      trigger?: Trigger;
    }) => {
      const [open, setOpen] = React.useState(false);
      const disabled = !threadId || !taskId;
      if (trigger) {
        return (
          <span data-testid="thread-cron-runs-trigger-host">
            {trigger({
              open,
              disabled,
              onClick: () => {
                if (!disabled) setOpen((current) => !current);
              },
            })}
            {open ? (
              <div
                data-testid="thread-cron-runs-panel"
                data-thread-id={threadId}
                data-task-id={taskId ?? ""}
                data-active-run-id={activeRunId ?? ""}
              >
                运行记录 {taskId}
              </div>
            ) : null}
          </span>
        );
      }
      return (
        <button
          type="button"
          data-testid="thread-cron-runs"
          data-thread-id={threadId}
          data-task-id={taskId ?? ""}
          data-active-run-id={activeRunId ?? ""}
        >
          运行记录
        </button>
      );
    },
  };
});
vi.mock("@/components/ClaudeCodeView", () => ({
  ClaudeCodeView: () => null,
}));
vi.mock("@/components/CodexView", () => ({ CodexView: () => null }));
vi.mock("@/components/WorkspaceFilesPanel", () => ({
  WorkspaceFilesPanel: () => <div data-testid="workspace-files-panel" />,
}));
vi.mock("@/components/WorkspaceGitPanel", () => ({
  WorkspaceGitPanel: () => <div data-testid="workspace-git-panel" />,
}));
vi.mock("@/components/WorkspaceShellPanel", () => ({
  WorkspaceShellPanel: () => <div data-testid="workspace-shell-panel" />,
}));
vi.mock("@/components/WorkspaceDock", () => ({
  WorkspaceDock: ({
    activeTab,
    onTabChange,
    chatContent,
    filesContent,
    gitContent,
    shellContent,
    whiteboardContent,
  }: {
    activeTab: "chat" | "files" | "git" | "shell" | "whiteboard";
    onTabChange: (tab: "chat" | "files" | "git" | "shell" | "whiteboard") => void;
    chatContent?: ReactNode;
    filesContent?: ReactNode;
    gitContent?: ReactNode;
    shellContent?: ReactNode;
    whiteboardContent?: ReactNode;
  }) => (
    <aside data-testid="workspace-dock" data-active-tab={activeTab}>
      <button type="button" onClick={() => onTabChange("files")}>
        Files
      </button>
      <button type="button" onClick={() => onTabChange("git")}>
        Git
      </button>
      <button type="button" onClick={() => onTabChange("shell")}>
        Shell
      </button>
      <button type="button" onClick={() => onTabChange("whiteboard")}>
        Whiteboard
      </button>
      <div data-testid={`dock-content-${activeTab}`}>
        {activeTab === "files"
          ? filesContent
          : activeTab === "git"
            ? gitContent
            : activeTab === "shell"
              ? shellContent
              : activeTab === "whiteboard"
                ? whiteboardContent
                : chatContent}
      </div>
    </aside>
  ),
}));
vi.mock("@/components/WhiteboardPanel", () => ({
  WhiteboardPanel: ({
    embedded,
    variant,
  }: {
    embedded?: boolean;
    variant?: string;
  }) => (
    <aside
      data-testid="whiteboard-panel"
      data-embedded={embedded ? "true" : "false"}
      data-variant={variant}
    />
  ),
}));
vi.mock("@/components/FileDrawer", () => ({ FileDrawer: () => null }));

/** 生成一条 generic_chat thread；可自定义 cwd / backend_kind 等关键字段 */
function makeThread(overrides: Partial<ThreadMetadataDTO> = {}): ThreadMetadataDTO {
  return {
    id: "thread-aaaaaaaaaaaa",
    name: "test",
    preset_id: "preset-1",
    backend_kind: "generic_chat",
    claude_thread_id: "",
    codex_thread_id: "",
    cwd: "/tmp/project-a",
    created_at: 0,
    updated_at: 0,
    message_count: 0,
    is_pinned: false,
    is_archived: false,
    ...overrides,
  };
}

/** 包 MemoryRouter + 路由匹配 :thread_id，让 useParams 工作 */
function LocationProbe() {
  const location = useLocation();
  return (
    <div data-testid="location">
      {location.pathname}
      {location.search}
    </div>
  );
}

function renderChat(threadId: string, search = "") {
  return render(
    <MemoryRouter initialEntries={[`/chat/${threadId}${search}`]}>
      <LocationProbe />
      <Routes>
        <Route path="/chat" element={<ChatPage />} />
        <Route path="/chat/:thread_id" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

function renderChatInLayout(threadId: string, search = "") {
  return render(
    <MemoryRouter initialEntries={[`/chat/${threadId}${search}`]}>
      <LocationProbe />
      <Routes>
        <Route element={<Layout />}>
          <Route path="/chat/:thread_id" element={<ChatPage />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

function renderChatRoot() {
  return render(
    <MemoryRouter initialEntries={["/chat"]}>
      <LocationProbe />
      <Routes>
        <Route path="/chat" element={<ChatPage />} />
        <Route path="/chat/:thread_id" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

function mockPendingItemRects(rows: HTMLElement[]) {
  rows.forEach((row, index) => {
    row.getBoundingClientRect = vi.fn(() => ({
      x: 0,
      y: index * 50,
      top: index * 50,
      bottom: index * 50 + 40,
      left: 0,
      right: 320,
      width: 320,
      height: 40,
      toJSON: () => ({}),
    })) as () => DOMRect;
  });
}

const realLoadModelFamilies = useModelProvidersStore.getState().loadModelFamilies;
const realResetModelProviders = useModelProvidersStore.getState().reset;

beforeEach(() => {
  // 复位所有相关 store + mock socket（每测一份干净环境）
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    writable: true,
    value: 1280,
  });
  window.dispatchEvent(new Event("resize"));
  useAuthStore.setState({ authenticated: true, _checked: true });
  useThreadsStore.setState({
    threads: [],
    pendingNewSession: null,
    reasoningSelectionByThread: {},
  });
  // 用 unknown 跨过 zustand setState 严格 union 校验：测试只关心几个字段
  // 复位，不构造完整 state；运行时 zustand 接受 partial，类型层面用 unknown
  // 绕一层即可，不引入新依赖。
  useChatStore.setState(
    { itemsByThread: {}, usageByThread: {} } as unknown as Parameters<
      typeof useChatStore.setState
    >[0],
  );
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
  // whiteboard store 已被整个 vi.mock 顶替，无需复位（state 恒定 stub）
  useWorkspaceStore.setState(
    {
      contextsByThread: {},
      loadingByThread: {},
      activeTabByThread: {},
      fileOpenRequestByThread: {},
    } as unknown as Parameters<typeof useWorkspaceStore.setState>[0],
  );
  vi.mocked(apiGet).mockResolvedValue([]);
  useModelProvidersStore.setState({
    modelFamilies: [],
    familiesLoadStatus: "idle",
    loadModelFamilies: realLoadModelFamilies,
    reset: realResetModelProviders,
  } as unknown as Parameters<typeof useModelProvidersStore.setState>[0]);
  networkMock.reset();
  toastMock.error.mockReset();
  toastMock.info.mockReset();
  toastMock.success.mockReset();
  toastMock.warning.mockReset();
  heartbeatConfig = {
    intervalMs: 30_000,
    backgroundIntervalMs: 60_000,
    timeoutMs: 10_000,
    maxMissed: 3,
  };
});

const modelFamilies: ConnectedModelFamily[] = [
  {
    providerId: "minimax",
    providerLabel: "Minimax（CN）",
    familyId: "minimax:MiniMax-M3",
    displayName: "MiniMax-M3",
    presetId: "minimax-cn",
    model: "MiniMax-M3",
    connected: true,
    supportedReasoningEfforts: ["none", "high"],
    defaultReasoningEffort: "high",
    reasoningAdapter: "anthropic_thinking_toggle",
    contextWindowTokens: 200000,
  },
  {
    providerId: "glm",
    providerLabel: "GLM（CN）",
    familyId: "glm:glm-5.1",
    displayName: "glm-5.1",
    presetId: "bigmodel-glm5",
    model: "glm-5.1",
    connected: true,
    supportedReasoningEfforts: ["none", "high"],
    defaultReasoningEffort: "high",
    reasoningAdapter: "glm_thinking_toggle",
    contextWindowTokens: 1000000,
  },
];

describe("ChatPage fork lineage navigation", () => {
  it("普通任务不显示续接入口", () => {
    const thread = makeThread({ forked_from_id: null });
    useThreadsStore.setState({ threads: [thread] });

    renderChat(thread.id);

    expect(screen.getByTestId("message-list")).not.toHaveAttribute(
      "data-fork-lineage-parent-id",
    );
  });

  it("fork 任务将直接父任务和精确历史边界传给消息时间线", () => {
    const grandparent = makeThread({
      id: "thread-dddddddddddd",
      name: "grandparent task",
    });
    const parent = makeThread({
      id: "thread-bbbbbbbbbbbb",
      name: "parent task",
      forked_from_id: grandparent.id,
    });
    const fork = makeThread({
      id: "thread-cccccccccccc",
      name: "fork task",
      forked_from_id: parent.id,
      forked_from_history_index: 3,
    });
    useThreadsStore.setState({ threads: [fork, parent, grandparent] });

    renderChat(fork.id);

    expect(screen.getByTestId("fork-lineage-message-viewport")).toHaveClass(
      "min-h-0",
      "flex-1",
    );
    expect(screen.getByTestId("message-list")).toHaveAttribute(
      "data-fork-lineage-parent-id",
      parent.id,
    );
    expect(screen.getByTestId("message-list")).toHaveAttribute(
      "data-fork-lineage-history-index",
      "3",
    );
  });
});

describe("ChatPage thread permissions 入口", () => {
  it("generic_chat thread 在 Dock 中渲染当前 thread 本子入口", async () => {
    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });

    renderChat(t.id);

    expect(screen.getByTestId("composer")).toBeInTheDocument();
    expect(screen.queryByTestId("auto-approval-toggle")).toBeNull();
    await waitFor(() =>
      expect(screen.getByTestId("thread-permissions-entry")).toHaveAttribute(
        "data-thread-id",
        t.id,
      ),
    );
  });

  it("thread 本子入口与 cwd 是否绑定无关", async () => {
    const t = makeThread({ cwd: "" });
    useThreadsStore.setState({ threads: [t] });

    renderChat(t.id);

    expect(screen.getByTestId("composer")).toBeInTheDocument();
    expect(screen.queryByTestId("auto-approval-toggle")).toBeNull();
    await waitFor(() =>
      expect(screen.getByTestId("thread-permissions-entry")).toHaveAttribute(
        "data-thread-id",
        t.id,
      ),
    );
  });

  it("generic_chat thread 有 cwd 但 heartbeat config 未就绪 → 保持输入区且不开 channel", () => {
    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });
    heartbeatConfig = undefined;

    const { unmount } = renderChat(t.id);

    expect(networkMock.openChannel).not.toHaveBeenCalled();
    expect(screen.getByTestId("composer")).toBeInTheDocument();
    expect(screen.getByTestId("composer")).toHaveAttribute("data-disabled", "1");
    expect(screen.queryByTestId("auto-approval-toggle")).toBeNull();
    expect(useConnectionStatusStore.getState().threadWsActive).toBe(true);

    unmount();

    expect(useConnectionStatusStore.getState()).toMatchObject({
      threadWsActive: false,
      threadWsState: "closed",
      threadWsLatencyMs: null,
    });
  });

  it("generic_chat heartbeat config 非法 → 不开 channel 且 cleanup 清状态", () => {
    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });
    heartbeatConfig = {
      intervalMs: Number.NaN,
      backgroundIntervalMs: 60_000,
      timeoutMs: 10_000,
      maxMissed: 3,
    };

    const { unmount } = renderChat(t.id);

    expect(networkMock.openChannel).not.toHaveBeenCalled();
    expect(screen.getByTestId("composer")).toHaveAttribute("data-disabled", "1");
    expect(useConnectionStatusStore.getState().threadWsActive).toBe(true);

    unmount();

    expect(useConnectionStatusStore.getState().threadWsActive).toBe(false);
  });
});

describe("ChatPage generic pending blank page", () => {
  it("pendingNewSession.backendKind=generic_chat 时渲染空白页并不开 generic WS", () => {
    useThreadsStore.setState({
      threads: [],
      pendingNewSession: {
        backendKind: "generic_chat",
        cwd: "",
        projectName: "",
      },
    });

    renderChatRoot();

    expect(screen.getByTestId("generic-empty-thread-view")).toBeInTheDocument();
    expect(screen.getByTestId("composer")).toBeInTheDocument();
    expect(screen.queryByTestId("message-list")).toBeNull();
    expect(screen.queryByTestId("whiteboard-panel")).toBeNull();
    expect(screen.queryByTestId("approval-dialog-send")).toBeNull();
    expect(networkMock.openChannel).not.toHaveBeenCalled();
  });

  it("首发跳转到真实 thread 后继承用户选择的 none", async () => {
    const sendSpy = vi
      .spyOn(ChatManager.prototype, "sendMessage")
      .mockResolvedValue(undefined);
    const created = makeThread({
      id: "thread-bbbbbbbbbbbb",
      preset_id: "minimax-cn",
      message_count: 1,
    });
    const createGenericThreadFromFirstMessage = vi.fn().mockImplementation(async () => {
      useThreadsStore.setState({
        threads: [created],
        pendingNewSession: null,
      });
      return created;
    });
    useThreadsStore.setState({
      threads: [],
      pendingNewSession: {
        backendKind: "generic_chat",
        cwd: "",
        projectName: "",
      },
      createGenericThreadFromFirstMessage,
    } as unknown as Parameters<typeof useThreadsStore.setState>[0]);
    useModelProvidersStore.setState({
      modelFamilies,
      loadModelFamilies: vi.fn().mockResolvedValue(undefined),
    } as unknown as Parameters<typeof useModelProvidersStore.setState>[0]);

    renderChatRoot();
    await userEvent.setup().click(screen.getByTestId("composer-submit-none"));

    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(created.id),
    );
    expect(screen.getByTestId("composer")).toHaveAttribute(
      "data-initial-reasoning-effort",
      "none",
    );
    await userEvent.setup().click(screen.getByTestId("composer-submit"));
    expect(sendSpy).toHaveBeenCalledWith({
      common: {
        text: "hello model",
        reasoningEffort: "none",
        attachments: undefined,
        references: undefined,
      },
      provider: {
        provider: "generic",
        threadId: created.id,
        presetId: "minimax-cn",
        modelFamilyId: "minimax:MiniMax-M3",
      },
    });
    sendSpy.mockRestore();
  });
});

describe("ChatPage workflow viewer entry", () => {
  it("renders workflow and progress entries in the thread toolbar", () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      writable: true,
      value: 1280,
    });
    window.dispatchEvent(new Event("resize"));
    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });

    renderChat(t.id);

    expect(screen.getByTestId("thread-progress")).toHaveAttribute(
      "data-thread-id",
      t.id,
    );
    expect(screen.getByRole("link", { name: "任务详情" })).toHaveAttribute(
      "href",
      `/chat/${t.id}/task-detail`,
    );
  });

  it("registers ChatPage thread progress into the Layout rail", async () => {
    const user = userEvent.setup();
    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });

    renderChatInLayout(t.id);

    const rail = screen.getByTestId("web-shell-rail");
    expect(screen.getByTestId("left-sidebar")).toHaveClass("z-20");
    expect(rail).toHaveStyle({ zIndex: "45" });

    fireEvent.mouseEnter(rail);
    const progress = await screen.findByTestId(
      "web-shell-rail-item-thread-task-progress",
    );
    await user.click(progress);

    expect(await screen.findByTestId("thread-progress-panel")).toHaveTextContent(
      t.id,
    );
  });

  it("registers scheduled thread runs into the Layout rail", async () => {
    const user = userEvent.setup();
    const t = makeThread({
      cwd: "/tmp/project-a",
      thread_kind: "scheduled_task",
      source_kind: "scheduled_task",
      source_id: "task-1",
    });
    vi.mocked(apiGet).mockImplementation(async (path) => {
      if (String(path) === "/api/cron/tasks/task-1/runs/run-1/messages") {
        return { messages: [] };
      }
      return [];
    });
    useThreadsStore.setState({ threads: [t] });

    renderChatInLayout(t.id, "?taskId=task-1&runId=run-1");

    const rail = screen.getByTestId("web-shell-rail");
    fireEvent.mouseEnter(rail);
    const cronRuns = await screen.findByTestId(
      "web-shell-rail-item-thread-cron-runs",
    );
    await user.click(cronRuns);

    const panel = await screen.findByTestId("thread-cron-runs-panel");
    expect(panel).toHaveAttribute("data-thread-id", t.id);
    expect(panel).toHaveAttribute("data-task-id", "task-1");
  });
});

describe("ChatPage generic NetworkManager channel", () => {
  it("wraps inbound generic frames in RawFrameEnvelope before ingestFrame", async () => {
    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });
    const ingestSpy = vi.spyOn(ChatManager.prototype, "ingestFrame");
    const frame = {
      frame_type: "content.delta",
      turn: 1,
      delta: "hello",
      seq: 1,
      run_id: "run-1",
    };

    renderChat(t.id);
    await waitFor(() =>
      expect(networkMock.openChannel).toHaveBeenCalledWith("generic", t.id),
    );

    networkMock.emitMessage(frame);

    expect(ingestSpy).toHaveBeenCalledWith({
      connectionId: "generic:thread-aaaaaaaaaaaa:conn-test",
      channel: "generic",
      threadId: t.id,
      frame,
      receivedAt: expect.any(Number),
    });
  });

  it("loads cron run messages into an isolated timeline and sends follow-up messages to the run session", async () => {
    const t = makeThread({
      cwd: "/tmp/project-a",
      thread_kind: "scheduled_task",
      source_kind: "scheduled_task",
      source_id: "task-1",
    });
    const sendSpy = vi
      .spyOn(ChatManager.prototype, "sendMessage")
      .mockResolvedValue(undefined);
    const runKey = makeCronTimelineKey(t.id, "run-1");
    disposeTimelineStore(t.id);
    disposeTimelineStore(runKey);
    useThreadsStore.setState({ threads: [t] });
    vi.mocked(apiGet).mockImplementation(async (path) => {
      if (String(path) === "/api/cron/tasks/task-1/runs/run-1/messages") {
        return {
          messages: [
            {
              frame_type: "text",
              role: "assistant",
              content: "run history",
              id: "run-msg-1",
              timestamp: "2026-06-15T10:00:00+08:00",
            },
          ],
        };
      }
      return [];
    });

    renderChat(t.id, "?taskId=task-1&runId=run-1");

    await waitFor(() =>
      expect(networkMock.openChannel).toHaveBeenCalledWith("cron-run", "task-1:run-1"),
    );

    await waitFor(() =>
      expect(apiGet).toHaveBeenCalledWith(
        "/api/cron/tasks/task-1/runs/run-1/messages",
      ),
    );
    await waitFor(() =>
      expect(getTimelineStore(runKey).snapshot().historyLoaded).toBe(true),
    );

    const runState = getTimelineStore(runKey).snapshot();
    expect(runState.orderedMessageIds).toEqual(["run-msg-1"]);
    expect(runState.messagesById["run-msg-1"].parts).toEqual([
      { type: "text", text: "run history" },
    ]);
    expect(getTimelineStore(t.id).snapshot().orderedMessageIds).toHaveLength(0);
    expect(screen.getByTestId("composer")).toHaveAttribute("data-disabled", "0");

    fireEvent.click(screen.getByTestId("composer-submit"));

    expect(screen.getByTestId("location")).toHaveTextContent(
      `/chat/${t.id}?taskId=task-1&runId=run-1`,
    );
    expect(sendSpy).toHaveBeenCalledWith({
      common: {
        text: "hello model",
        reasoningEffort: "high",
        attachments: undefined,
      },
      provider: {
        provider: "generic",
        threadId: runKey,
        presetId: "preset-1",
        modelFamilyId: null,
      },
    });

    sendSpy.mockRestore();
  });

  it("opens a scheduled task thread on the latest run by default", async () => {
    const t = makeThread({
      cwd: "/tmp/project-a",
      thread_kind: "scheduled_task",
      source_kind: "scheduled_task",
      source_id: "task-1",
    });
    useThreadsStore.setState({ threads: [t] });
    vi.mocked(apiGet).mockImplementation(async (path) => {
      if (String(path) === "/api/cron/tasks/task-1/runs?limit=1") {
        return [
          {
            run_id: "run-latest",
            task_id: "task-1",
            task_name: "daily",
            session_id: "session-latest",
            thread_id: t.id,
            scheduled_for: "2026-06-15T14:47:00+08:00",
            started_at: "2026-06-15T14:47:01+08:00",
            finished_at: "2026-06-15T14:47:05+08:00",
            status: "success",
            final_message_excerpt: "latest",
            delivery_status: "delivered",
            delivery_error: null,
          },
        ];
      }
      if (String(path) === "/api/cron/tasks/task-1/runs/run-latest/messages") {
        return { messages: [] };
      }
      return [];
    });

    renderChat(t.id);

    await waitFor(() =>
      expect(apiGet).toHaveBeenCalledWith(
        "/api/cron/tasks/task-1/runs?limit=1",
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(
        `/chat/${t.id}?taskId=task-1&runId=run-latest`,
      ),
    );
    expect(screen.getByTestId("thread-cron-runs")).toHaveAttribute(
      "data-task-id",
      "task-1",
    );
  });

  it("switches the parent thread to the incoming cron run timeline", async () => {
    const t = makeThread({ cwd: "/tmp/project-a" });
    const ingestSpy = vi.spyOn(ChatManager.prototype, "ingestFrame");
    const runKey = makeCronTimelineKey(t.id, "run-2");
    disposeTimelineStore(t.id);
    disposeTimelineStore(runKey);
    useThreadsStore.setState({ threads: [t] });
    vi.mocked(apiGet).mockImplementation(async (path) => {
      if (String(path) === "/api/cron/tasks/task-1/runs/run-2/messages") {
        return {
          messages: [
            {
              frame_type: "text",
              role: "user",
              content: "回复 收到",
              id: "history-user-2",
              timestamp: "2026-06-15T14:47:37+08:00",
            },
            {
              frame_type: "text",
              role: "assistant",
              content: "收到。",
              id: "history-assistant-2",
              timestamp: "2026-06-15T14:47:39+08:00",
            },
          ],
        };
      }
      return [];
    });

    renderChat(t.id);
    await waitFor(() =>
      expect(networkMock.openChannel).toHaveBeenCalledWith("generic", t.id),
    );
    ingestSpy.mockClear();

    act(() => {
      networkMock.emitMessage({
        frame_type: "cron.message.appended",
        timestamp_ms: 1_700_000_000_000,
        thread_id: t.id,
        task_id: "task-1",
        run_id: "run-2",
        session_id: "session-run-2",
        task_name: "daily",
        message_id: "cron-msg-2",
        content: "other run",
      });
    });

    expect(ingestSpy).toHaveBeenCalledWith({
      connectionId: "generic:thread-aaaaaaaaaaaa:conn-test",
      channel: "generic",
      threadId: runKey,
      frame: expect.objectContaining({ run_id: "run-2" }),
      receivedAt: expect.any(Number),
    });
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(
        `/chat/${t.id}?taskId=task-1&runId=run-2`,
      ),
    );
    await waitFor(() =>
      expect(getTimelineStore(runKey).snapshot().historyLoaded).toBe(true),
    );
    const runState = getTimelineStore(runKey).snapshot();
    expect(runState.orderedMessageIds).toEqual([
      "history-user-2",
      "history-assistant-2",
    ]);
    expect(runState.messagesById["history-assistant-2"].parts).toEqual([
      { type: "text", text: "收到。" },
    ]);
    expect(runState.messagesById["cron-msg-2"]).toBeUndefined();
    expect(getTimelineStore(t.id).snapshot().orderedMessageIds).toHaveLength(0);
  });

  it("generic channel 未就绪时不渲染处置模式选择器", () => {
    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });

    renderChat(t.id);
    expect(screen.queryByTestId("approval-mode-selector")).toBeNull();
    expect(networkMock.handle.send).not.toHaveBeenCalledWith(
      expect.objectContaining({ frame_type: "auto-approval-query" }),
    );
  });

  it("choice.request mounts ChoicePanel and confirms via generic ChannelHandle", async () => {
    const user = userEvent.setup();
    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });

    renderChat(t.id);
    await waitFor(() =>
      expect(networkMock.openChannel).toHaveBeenCalledWith("generic", t.id),
    );
    networkMock.handle.send.mockClear();

    networkMock.emitMessage({
      frame_type: "choice.request",
      timestamp_ms: 1_700_000_000_000,
      request_id: "call-1",
      title: "选择方案",
      description: "请选择下一步。",
      turn: 1,
      run_id: "run-1",
      questions: [
        {
          id: "scope",
          title: "范围",
          options: [
            {
              id: "minimal",
              label: "最小实现",
              description: "先打通主链路。",
            },
          ],
        },
      ],
    });

    expect(await screen.findByTestId("choice-panel")).toBeInTheDocument();
    await user.click(screen.getByTestId("choice-option-minimal"));
    await user.click(screen.getByTestId("choice-confirm"));

    expect(networkMock.handle.send).toHaveBeenCalledWith({
      frame_type: "choice.submit",
      request_id: "call-1",
      answers: [
        {
          question_id: "scope",
          option_id: "minimal",
          option_label: "最小实现",
          custom_text: null,
          value: null,
        },
      ],
    });
  });

  it("pending-input.snapshot renders queue panel and sends cancel frame", async () => {
    const user = userEvent.setup();
    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });

    renderChat(t.id);
    await waitFor(() =>
      expect(networkMock.openChannel).toHaveBeenCalledWith("generic", t.id),
    );
    networkMock.handle.send.mockClear();

    networkMock.emitMessage({
      frame_type: "pending-input.snapshot",
      timestamp_ms: 1_700_000_000_000,
      thread_id: t.id,
      max_items: 20,
      active_run_id: null,
      version: 1,
      items: [
        {
          id: "pin-1",
          thread_id: t.id,
          source: "user_input",
          priority: "user_message",
          content: "queued message",
          preview: "queued message",
          status: "queued",
          created_at_ms: 1_700_000_000_000,
          updated_at_ms: 1_700_000_000_000,
          sequence: 1,
          metadata: {},
        },
      ],
    });

    expect(await screen.findByTestId("pending-input-queue")).toHaveTextContent(
      "queued message",
    );
    await user.click(screen.getByLabelText("立即发送待发送消息"));

    expect(networkMock.handle.send).toHaveBeenCalledWith({
      frame_type: "pending-input.send-now",
      pending_input_id: "pin-1",
      request_id: null,
    });
    networkMock.handle.send.mockClear();

    await user.click(screen.getByLabelText("删除待发送消息"));

    expect(networkMock.handle.send).toHaveBeenCalledWith({
      frame_type: "pending-input.cancel",
      pending_input_id: "pin-1",
    });
  });

  it("pending-input.snapshot drag release sends reorder frame", async () => {
    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });

    renderChat(t.id);
    await waitFor(() =>
      expect(networkMock.openChannel).toHaveBeenCalledWith("generic", t.id),
    );
    networkMock.handle.send.mockClear();

    networkMock.emitMessage({
      frame_type: "pending-input.snapshot",
      timestamp_ms: 1_700_000_000_000,
      thread_id: t.id,
      max_items: 20,
      active_run_id: null,
      version: 1,
      items: ["one", "two", "three", "four"].map((content, index) => ({
        id: `pin-${index + 1}`,
        thread_id: t.id,
        source: "user_input",
        priority: "user_message",
        content,
        preview: content,
        status: "queued",
        created_at_ms: 1_700_000_000_000 + index,
        updated_at_ms: 1_700_000_000_000 + index,
        sequence: index + 1,
        metadata: {},
      })),
    });

    expect(await screen.findByTestId("pending-input-queue")).toHaveTextContent("four");
    mockPendingItemRects(screen.getAllByTestId("pending-input-item"));
    const handles = screen.getAllByTestId("pending-input-drag-handle");

    fireEvent.pointerDown(handles[3], {
      pointerId: 1,
      clientY: 175,
      buttons: 1,
    });
    fireEvent.pointerMove(handles[3], {
      pointerId: 1,
      clientY: 65,
      buttons: 1,
    });
    expect(networkMock.handle.send).not.toHaveBeenCalled();

    fireEvent.pointerUp(handles[3], {
      pointerId: 1,
      clientY: 65,
    });

    expect(networkMock.handle.send).toHaveBeenCalledWith({
      frame_type: "pending-input.reorder",
      ordered_ids: ["pin-1", "pin-4", "pin-2", "pin-3"],
    });
  });

  it("pending_input_queue_full error asks Composer to restore the submitted draft", async () => {
    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });

    renderChat(t.id);
    await waitFor(() =>
      expect(networkMock.openChannel).toHaveBeenCalledWith("generic", t.id),
    );

    fireEvent.click(screen.getByTestId("composer-submit"));
    act(() => {
      networkMock.emitMessage({
        frame_type: "error",
        timestamp_ms: 1_700_000_000_000,
        code: "invalid_request",
        message: "待发送队列已满（最多 20 条）。",
        reason: "pending_input_queue_full",
      });
    });

    await waitFor(() =>
      expect(screen.getByTestId("composer")).toHaveAttribute(
        "data-restore-draft-token",
        "1",
      ),
    );
    expect(toastMock.error).toHaveBeenCalledWith("待发送队列已满（最多 20 条）。");
  });

  it("run.interrupted keeps the migrated stop toast behavior", async () => {
    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });

    renderChat(t.id);
    await waitFor(() =>
      expect(networkMock.openChannel).toHaveBeenCalledWith("generic", t.id),
    );

    networkMock.emitMessage({
      frame_type: "run.interrupted",
      run_id: "run-stop",
      cancelled_at_turn: 2,
      cancelled_tool_call_id: null,
      cancel_reason: "user_interrupt",
      timestamp_ms: 1_700_000_000_000,
    });

    expect(toastMock.info).toHaveBeenCalledWith("已停止当前任务");
  });
});

describe("ChatPage generic model switcher", () => {
  it("loads connected model families and switches the thread preset", async () => {
    const user = userEvent.setup();
    const t = makeThread({ preset_id: "minimax-cn" });
    const loadModelFamilies = vi.fn().mockResolvedValue(undefined);
    const updateThreadPreset = vi.fn().mockResolvedValue({
      ...t,
      preset_id: "bigmodel-glm5",
      updated_at: 200,
    });

    useThreadsStore.setState({
      threads: [t],
      updateThreadPreset,
    } as unknown as Parameters<typeof useThreadsStore.setState>[0]);
    useModelProvidersStore.setState({
      modelFamilies,
      loadModelFamilies,
    } as unknown as Parameters<typeof useModelProvidersStore.setState>[0]);

    renderChat(t.id);

    await waitFor(() => expect(loadModelFamilies).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("composer-model-switcher-slot")).toHaveTextContent(
      "MiniMax-M3",
    );
    expect(screen.getByTestId("composer")).toHaveAttribute(
      "data-reasoning-options",
      "none|high",
    );

    await user.click(screen.getByTestId("composer-model-switcher"));
    await user.click(screen.getByTestId("composer-model-option-glm"));

    expect(updateThreadPreset).toHaveBeenCalledWith(t.id, "bigmodel-glm5");
    await waitFor(() =>
      expect(toastMock.success).toHaveBeenCalledWith(
        "模型已切换，下一次发送生效。",
      ),
    );
  });

  it("sends current preset and model family metadata through ChatManager", async () => {
    const t = makeThread({ preset_id: "minimax-cn" });
    const sendSpy = vi
      .spyOn(ChatManager.prototype, "sendMessage")
      .mockResolvedValue(undefined);

    useThreadsStore.setState({ threads: [t] });
    useModelProvidersStore.setState({
      modelFamilies,
      loadModelFamilies: vi.fn().mockResolvedValue(undefined),
    } as unknown as Parameters<typeof useModelProvidersStore.setState>[0]);

    renderChat(t.id);
    await waitFor(() =>
      expect(networkMock.openChannel).toHaveBeenCalledWith("generic", t.id),
    );

    fireEvent.click(screen.getByTestId("composer-submit"));

    expect(sendSpy).toHaveBeenCalledWith({
      common: {
        text: "hello model",
        reasoningEffort: "high",
        attachments: undefined,
      },
      provider: {
        provider: "generic",
        threadId: t.id,
        presetId: "minimax-cn",
        modelFamilyId: "minimax:MiniMax-M3",
      },
    });

    sendSpy.mockRestore();
  });
});

describe("ChatPage compact workspace dock", () => {
  it("hides the workspace dock on compact widths while keeping thread toolbar actions", () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      writable: true,
      value: 1024,
    });
    window.dispatchEvent(new Event("resize"));

    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });

    renderChat(t.id);

    expect(screen.queryByTestId("workspace-dock")).toBeNull();
    expect(screen.getByTestId("thread-progress")).toHaveAttribute(
      "data-thread-id",
      t.id,
    );
    expect(screen.getByRole("link", { name: "任务详情" })).toHaveAttribute(
      "href",
      `/chat/${t.id}/task-detail`,
    );
  });
});

describe("ChatPage workspace dock layout", () => {
  it("keeps the main chat visible when the dock shows Whiteboard", () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      writable: true,
      value: 1280,
    });
    window.dispatchEvent(new Event("resize"));

    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });
    useWorkspaceStore.setState({
      activeTabByThread: {
        [t.id]: "whiteboard",
      },
    } as unknown as Parameters<typeof useWorkspaceStore.setState>[0]);

    renderChat(t.id);

    expect(screen.getByTestId("message-list")).toBeInTheDocument();
    expect(screen.getByTestId("composer")).toBeInTheDocument();
    expect(screen.getByTestId("workspace-dock")).toHaveAttribute(
      "data-active-tab",
      "whiteboard",
    );
    const whiteboard = screen.getByTestId("whiteboard-panel");
    expect(whiteboard).toHaveAttribute("data-embedded", "true");
    expect(whiteboard).toHaveAttribute("data-variant", "dock");
  });

  it("renders Files inside the dock while the main chat remains mounted", () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      writable: true,
      value: 1280,
    });
    window.dispatchEvent(new Event("resize"));

    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });
    useWorkspaceStore.setState({
      activeTabByThread: {
        [t.id]: "files",
      },
    } as unknown as Parameters<typeof useWorkspaceStore.setState>[0]);

    renderChat(t.id);

    expect(screen.getByTestId("workspace-dock")).toHaveAttribute(
      "data-active-tab",
      "files",
    );
    expect(screen.getByTestId("workspace-files-panel")).toBeInTheDocument();
    expect(screen.getByTestId("message-list")).toBeInTheDocument();
    expect(screen.getByTestId("composer")).toBeInTheDocument();
  });

  it("renders workflow and thread permissions inside the dock Chat tab", async () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      writable: true,
      value: 1280,
    });
    window.dispatchEvent(new Event("resize"));

    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });

    renderChat(t.id);

    expect(screen.getByTestId("workspace-dock")).toHaveAttribute(
      "data-active-tab",
      "chat",
    );
    expect(screen.getByTestId("dock-chat-context")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "任务详情" })).toHaveAttribute(
      "href",
      `/chat/${t.id}/task-detail`,
    );
    await waitFor(() =>
      expect(screen.getByTestId("thread-permissions-entry")).toHaveAttribute(
        "data-thread-id",
        t.id,
      ),
    );
  });

  it("keeps progress and workflow toolbar scoped to the main chat panel", () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      writable: true,
      value: 1280,
    });
    window.dispatchEvent(new Event("resize"));

    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });

    renderChat(t.id);

    const toolbar = screen.getByTestId("chat-thread-toolbar");
    expect(screen.getByTestId("chat-main-panel")).toContainElement(toolbar);
    expect(screen.getByTestId("workspace-dock")).not.toContainElement(toolbar);
  });
});

describe("ChatPage thread permissions 与 workspace cwd 解耦", () => {
  it("thread.cwd 为空且 workspace_root 存在时仍按 thread id 管理本子", async () => {
    const t = makeThread({ cwd: "" });
    const workspaceContext = {
      thread_id: t.id,
      backend_kind: "generic_chat",
      workspace_root: "/proj/server-root",
      claude_thread_id: "",
      shell_provider: "system_shell",
      files_available: true,
      shell_available: true,
    };
    useThreadsStore.setState({ threads: [t] });
    vi.mocked(apiGet).mockImplementation(async (path) => {
      if (String(path).endsWith(`/api/threads/${t.id}/workspace-context`)) {
        return workspaceContext;
      }
      return [];
    });
    // 预置 workspace context（后端 fallback 后的 server 启动目录）。
    // contextsByThread[threadId] 真实 DTO 还含 backend_kind / claude_thread_id
    // 等字段，本测试只关心 workspace_root，其余用 unknown 跨过严类型校验。
    useWorkspaceStore.setState({
      contextsByThread: {
        [t.id]: workspaceContext,
      },
    } as unknown as Parameters<typeof useWorkspaceStore.setState>[0]);

    renderChat(t.id);

    expect(screen.queryByTestId("auto-approval-toggle")).toBeNull();
    await waitFor(() =>
      expect(screen.getByTestId("thread-permissions-entry")).toHaveAttribute(
        "data-thread-id",
        t.id,
      ),
    );
  });

  it("thread.cwd 与 workspace_root 都空时仍按 thread id 管理本子", async () => {
    const t = makeThread({ cwd: "" });
    useThreadsStore.setState({ threads: [t] });
    // workspace context 缺失（contextsByThread 留空）→ effectiveCwd=""
    // workspace context 存在但 workspace_root="" 也走同一分支（这里测前者更通用）
    useWorkspaceStore.setState({
      contextsByThread: {},
    } as unknown as Parameters<typeof useWorkspaceStore.setState>[0]);

    renderChat(t.id);

    expect(screen.getByTestId("composer")).toBeInTheDocument();
    expect(screen.queryByTestId("auto-approval-toggle")).toBeNull();
    await waitFor(() =>
      expect(screen.getByTestId("thread-permissions-entry")).toHaveAttribute(
        "data-thread-id",
        t.id,
      ),
    );
  });

  it("thread.cwd 与 workspace_root 都存在时 permissions 归属仍取 thread id", async () => {
    const t = makeThread({ cwd: "/explicit" });
    useThreadsStore.setState({ threads: [t] });
    useWorkspaceStore.setState({
      contextsByThread: {
        [t.id]: {
          thread_id: t.id,
          backend_kind: "generic_chat",
          workspace_root: "/server-root",
          claude_thread_id: "",
          shell_provider: "system_shell",
          files_available: true,
          shell_available: true,
        },
      },
    } as unknown as Parameters<typeof useWorkspaceStore.setState>[0]);

    renderChat(t.id);

    expect(screen.queryByTestId("auto-approval-toggle")).toBeNull();
    await waitFor(() =>
      expect(screen.getByTestId("thread-permissions-entry")).toHaveAttribute(
        "data-thread-id",
        t.id,
      ),
    );
  });
});

describe("ChatPage clears stale pending provider session", () => {
  it("opening a real codex thread clears pendingNewSession", async () => {
    const t = makeThread({
      id: "thread-codex-1",
      backend_kind: "codex",
      codex_thread_id: "019e7923-e92b-7253-a712-3fcdcf60c7f8",
    });
    useThreadsStore.setState({
      threads: [t],
      pendingNewSession: {
        cwd: "/tmp/old-pending",
        projectName: "old-pending",
        backendKind: "codex",
      },
    });

    renderChat(t.id);

    await waitFor(() =>
      expect(useThreadsStore.getState().pendingNewSession).toBeNull(),
    );
  });
});
