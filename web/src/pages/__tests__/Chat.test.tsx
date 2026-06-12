import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { ChatPage } from "@/pages/Chat";
import { ChatManager } from "@/chat/ChatManager";
import { useThreadsStore } from "@/stores/threads";
import { useChatStore } from "@/stores/chat";
import { useConnectionStatusStore } from "@/stores/connectionStatus";
import { useWorkspaceStore } from "@/stores/workspace";
import { useAutoApprovalStore } from "@/features/auto-approval/useAutoApproval";
import { useModelProvidersStore } from "@/modules/model-providers/store";
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

const approvalDialogMock = vi.hoisted(() => ({
  latestSocket: null as null | {
    send(frame: {
      frame_type: "approval.ack";
      call_id: string;
      action: "reject";
    }): boolean | void;
  },
  reset() {
    this.latestSocket = null;
  },
}));

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
// 测试目标：smart-approval-v1（task #7）
// generic_chat 分支 Composer 是否按 (thread.cwd + socket) 条件渲染
// AutoApprovalToggle。其他子组件 mock 掉避免拖入 LeftSidebar / WorkspaceFiles /
// WhiteboardPanel / FileDrawer 等重组件，让单测聚焦本任务行为。
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

vi.mock("sonner", () => ({
  toast: toastMock,
}));

// Composer mock：把 leftActions 透传出来给断言；不需要真实输入框
vi.mock("@/components/Composer", () => ({
  Composer: ({
    leftActions,
    modelSwitcher,
    onSubmit,
    disabled,
  }: {
    leftActions?: ReactNode;
    modelSwitcher?: ReactNode;
    onSubmit?: (
      text: string,
      reasoningEffort: "low" | "medium" | "high" | null,
    ) => void;
    disabled?: boolean;
  }) => (
    <div data-testid="composer" data-disabled={disabled ? "1" : "0"}>
      <div data-testid="composer-left-actions">{leftActions ?? null}</div>
      <div data-testid="composer-model-switcher-slot">{modelSwitcher ?? null}</div>
      <button
        type="button"
        data-testid="composer-submit"
        disabled={disabled}
        onClick={() => onSubmit?.("hello model", "high")}
      >
        submit
      </button>
    </div>
  ),
}));

// 其它视图 / 重组件：纯 stub
vi.mock("@/components/LeftSidebar", () => ({ LeftSidebar: () => null }));
vi.mock("@/components/MessageList", () => ({
  MessageList: () => <div data-testid="message-list" />,
}));
vi.mock("@/components/ThreadTaskProgressPopover", () => ({
  ThreadTaskProgressPopover: ({ threadId }: { threadId?: string }) => (
    <button type="button" data-testid="thread-progress" data-thread-id={threadId}>
      进度
    </button>
  ),
}));
vi.mock("@/components/ApprovalDialog", () => ({
  ApprovalDialog: ({ socket }: { socket: typeof approvalDialogMock.latestSocket }) => {
    approvalDialogMock.latestSocket = socket;
    return (
      <button
        type="button"
        data-testid="approval-dialog-send"
        onClick={() =>
          socket?.send({
            frame_type: "approval.ack",
            call_id: "call-from-dialog",
            action: "reject",
          })
        }
      >
        approval ack
      </button>
    );
  },
}));
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
    filesContent,
    gitContent,
    shellContent,
    whiteboardContent,
  }: {
    activeTab: "chat" | "files" | "git" | "shell" | "whiteboard";
    onTabChange: (tab: "chat" | "files" | "git" | "shell" | "whiteboard") => void;
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
                : null}
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
function renderChat(threadId: string) {
  return render(
    <MemoryRouter initialEntries={[`/chat/${threadId}`]}>
      <Routes>
        <Route path="/chat" element={<ChatPage />} />
        <Route path="/chat/:thread_id" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

function renderChatRoot() {
  return render(
    <MemoryRouter initialEntries={["/chat"]}>
      <Routes>
        <Route path="/chat" element={<ChatPage />} />
        <Route path="/chat/:thread_id" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

const realLoadModelFamilies = useModelProvidersStore.getState().loadModelFamilies;
const realResetModelProviders = useModelProvidersStore.getState().reset;

beforeEach(() => {
  // 复位所有相关 store + mock socket（每测一份干净环境）
  useThreadsStore.setState({ threads: [], pendingNewSession: null });
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
  useAutoApprovalStore.getState().clear();
  useModelProvidersStore.setState({
    modelFamilies: [],
    familiesLoadStatus: "idle",
    loadModelFamilies: realLoadModelFamilies,
    reset: realResetModelProviders,
  } as unknown as Parameters<typeof useModelProvidersStore.setState>[0]);
  networkMock.reset();
  approvalDialogMock.reset();
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
  },
  {
    providerId: "glm",
    providerLabel: "GLM（CN）",
    familyId: "glm:glm-5.1",
    displayName: "glm-5.1",
    presetId: "bigmodel-glm5",
    model: "glm-5.1",
    connected: true,
  },
];

describe("ChatPage smart-approval-v1 generic_chat Toggle 挂载", () => {
  it("generic_chat thread + 有 cwd + 有 socket → 渲染 AutoApprovalToggle", async () => {
    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });

    renderChat(t.id);

    // Composer 已渲染 + leftActions 槽里有 Toggle（按 data-testid 兜底）
    expect(screen.getByTestId("composer")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("auto-approval-toggle")).toBeInTheDocument(),
    );
  });

  it("generic_chat thread 但 cwd 为空 → 不渲染 Toggle", () => {
    const t = makeThread({ cwd: "" });
    useThreadsStore.setState({ threads: [t] });

    renderChat(t.id);

    expect(screen.getByTestId("composer")).toBeInTheDocument();
    expect(screen.queryByTestId("auto-approval-toggle")).toBeNull();
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
    expect(screen.getByRole("link", { name: "Workflow Viewer" })).toHaveAttribute(
      "href",
      `/chat/${t.id}/agent-workflows`,
    );
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

  it("AutoApprovalToggle uses the generic ChannelHandle send path", async () => {
    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });

    renderChat(t.id);
    await waitFor(() =>
      expect(networkMock.handle.send).toHaveBeenCalledWith({
        frame_type: "auto-approval-query",
        cwd: "/tmp/project-a",
      }),
    );
    networkMock.handle.send.mockClear();

    fireEvent.click(screen.getByTestId("auto-approval-switch"));

    expect(networkMock.handle.send).toHaveBeenCalledWith({
      frame_type: "auto-approval-toggle",
      cwd: "/tmp/project-a",
      enabled: true,
    });
  });

  it("ApprovalDialog uses the generic ChannelHandle send path", async () => {
    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });

    renderChat(t.id);
    await waitFor(() =>
      expect(approvalDialogMock.latestSocket).not.toBeNull(),
    );
    networkMock.handle.send.mockClear();

    fireEvent.click(screen.getByTestId("approval-dialog-send"));

    expect(networkMock.handle.send).toHaveBeenCalledWith({
      frame_type: "approval.ack",
      call_id: "call-from-dialog",
      action: "reject",
    });
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
    expect(screen.getByRole("link", { name: "Workflow Viewer" })).toHaveAttribute(
      "href",
      `/chat/${t.id}/agent-workflows`,
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
});

// ---------------------------------------------------------------------------
// 测试目标：thread-cwd-fallback task #5
// thread.cwd 缺失时，Composer leftActions 用 workspace context.workspace_root
// 兜底（后端 fallback 后的 server 启动目录），让纯聊天 thread 也能挂智能审批。
// 用 AutoApprovalToggle 的 title 属性 `智能审批 · 当前 project: <cwd>` 反查
// effectiveCwd 实际传入值（避免引入新 testid）。
// ---------------------------------------------------------------------------

describe("ChatPage smart-approval-v1 effectiveCwd fallback（task #5）", () => {
  it("thread.cwd 空但 workspace_root 存在 → Toggle 渲染且用 workspace_root", async () => {
    const t = makeThread({ cwd: "" });
    useThreadsStore.setState({ threads: [t] });
    // 预置 workspace context（后端 fallback 后的 server 启动目录）。
    // contextsByThread[threadId] 真实 DTO 还含 backend_kind / claude_thread_id
    // 等字段，本测试只关心 workspace_root，其余用 unknown 跨过严类型校验。
    useWorkspaceStore.setState({
      contextsByThread: {
        [t.id]: {
          thread_id: t.id,
          backend_kind: "generic_chat",
          workspace_root: "/proj/server-root",
          claude_thread_id: "",
          shell_provider: "system_shell",
          files_available: true,
          shell_available: true,
        },
      },
    } as unknown as Parameters<typeof useWorkspaceStore.setState>[0]);

    renderChat(t.id);

    const toggle = await screen.findByTestId("auto-approval-toggle");
    expect(toggle).toBeInTheDocument();
    // title 模板：`智能审批 · 当前 project: ${cwd}` —— 反查实际 cwd 入参
    expect(toggle).toHaveAttribute(
      "title",
      "智能审批 · 当前 project: /proj/server-root",
    );
  });

  it("thread.cwd 与 workspace_root 都空 → Toggle 不渲染（与原行为对齐）", () => {
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
  });

  it("thread.cwd 与 workspace_root 都存在 → thread.cwd 优先", async () => {
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

    const toggle = await screen.findByTestId("auto-approval-toggle");
    expect(toggle).toBeInTheDocument();
    expect(toggle).toHaveAttribute(
      "title",
      "智能审批 · 当前 project: /explicit",
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
