import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { ChatPage } from "@/pages/Chat";
import { useThreadsStore } from "@/stores/threads";
import { useChatStore } from "@/stores/chat";
import { useWorkspaceStore } from "@/stores/workspace";
import { useAutoApprovalStore } from "@/features/auto-approval/useAutoApproval";
import type { ThreadMetadataDTO } from "@/protocol";

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

// useWS：可被 test 控制返回的 socket（模拟 thread WS 连上 / 未连上两种状态）。
// chat-receive-side-unify #5：generic 接收侧改走 socket.on(frame)→ingestFrame，
// 所以 mock socket 必须含 on（返回 unsubscribe），否则 ChatPage 的订阅 effect
// 调 socket.on 会因 undefined 崩。本测试不灌帧，on 注册后空跑即可。
const mockSocket: {
  send: ReturnType<typeof vi.fn>;
  on: ReturnType<typeof vi.fn>;
} = { send: vi.fn(), on: vi.fn(() => () => {}) };
let wsReturn: { socket: typeof mockSocket | null } = { socket: mockSocket };
vi.mock("@/hooks/useWS", () => ({
  useWS: () => wsReturn,
}));

// useStreamingRender 已退役（generic 不再用），保留 mock 兜底其它残留 import。
vi.mock("@/hooks/useStreamingRender", () => ({
  useStreamingRender: () => undefined,
}));

// Composer mock：把 leftActions 透传出来给断言；不需要真实输入框
vi.mock("@/components/Composer", () => ({
  Composer: ({
    leftActions,
    disabled,
  }: {
    leftActions?: ReactNode;
    disabled?: boolean;
  }) => (
    <div data-testid="composer" data-disabled={disabled ? "1" : "0"}>
      <div data-testid="composer-left-actions">{leftActions ?? null}</div>
    </div>
  ),
}));

// 其它视图 / 重组件：纯 stub
vi.mock("@/components/LeftSidebar", () => ({ LeftSidebar: () => null }));
vi.mock("@/components/MessageList", () => ({
  MessageList: () => <div data-testid="message-list" />,
}));
vi.mock("@/components/ApprovalDialog", () => ({
  ApprovalDialog: () => null,
}));
vi.mock("@/components/ClaudeCodeView", () => ({
  ClaudeCodeView: () => null,
}));
vi.mock("@/components/CodexView", () => ({ CodexView: () => null }));
vi.mock("@/components/WorkspaceFilesPanel", () => ({
  WorkspaceFilesPanel: () => null,
}));
vi.mock("@/components/WorkspaceGitPanel", () => ({
  WorkspaceGitPanel: () => null,
}));
vi.mock("@/components/WorkspaceShellPanel", () => ({
  WorkspaceShellPanel: () => null,
}));
vi.mock("@/components/WorkspaceTabs", () => ({
  WorkspaceTabs: () => <div data-testid="workspace-tabs" />,
}));
vi.mock("@/components/WhiteboardPanel", () => ({
  WhiteboardPanel: () => null,
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
        <Route path="/chat/:thread_id" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

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
  mockSocket.send.mockReset();
  mockSocket.on.mockReset();
  mockSocket.on.mockImplementation(() => () => {});
  wsReturn = { socket: mockSocket };
});

describe("ChatPage smart-approval-v1 generic_chat Toggle 挂载", () => {
  it("generic_chat thread + 有 cwd + 有 socket → 渲染 AutoApprovalToggle", () => {
    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });

    renderChat(t.id);

    // Composer 已渲染 + leftActions 槽里有 Toggle（按 data-testid 兜底）
    expect(screen.getByTestId("composer")).toBeInTheDocument();
    expect(screen.getByTestId("auto-approval-toggle")).toBeInTheDocument();
  });

  it("generic_chat thread 但 cwd 为空 → 不渲染 Toggle", () => {
    const t = makeThread({ cwd: "" });
    useThreadsStore.setState({ threads: [t] });

    renderChat(t.id);

    expect(screen.getByTestId("composer")).toBeInTheDocument();
    expect(screen.queryByTestId("auto-approval-toggle")).toBeNull();
  });

  it("generic_chat thread 有 cwd 但 socket=null → 不渲染 Toggle", () => {
    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });
    // 模拟 WS 还没连上（useWS 返回 socket: null）
    wsReturn = { socket: null };

    renderChat(t.id);

    expect(screen.getByTestId("composer")).toBeInTheDocument();
    expect(screen.queryByTestId("auto-approval-toggle")).toBeNull();
  });
});

describe("ChatPage compact workspace tabs", () => {
  it("hides workspace tabs on compact widths", () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      writable: true,
      value: 1024,
    });
    window.dispatchEvent(new Event("resize"));

    const t = makeThread({ cwd: "/tmp/project-a" });
    useThreadsStore.setState({ threads: [t] });

    renderChat(t.id);

    expect(screen.queryByTestId("workspace-tabs")).toBeNull();
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
  it("thread.cwd 空但 workspace_root 存在 → Toggle 渲染且用 workspace_root", () => {
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

    const toggle = screen.getByTestId("auto-approval-toggle");
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

  it("thread.cwd 与 workspace_root 都存在 → thread.cwd 优先", () => {
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

    const toggle = screen.getByTestId("auto-approval-toggle");
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
