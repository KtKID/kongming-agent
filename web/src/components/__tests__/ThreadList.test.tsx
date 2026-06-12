import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { ThreadList } from "@/components/ThreadList";
import { useThreadsStore } from "@/stores/threads";

beforeEach(() => {
  useThreadsStore.setState({
    threads: [],
    presets: [],
    loading: false,
    fetchThreads: vi.fn().mockResolvedValue(undefined),
    fetchPresets: vi.fn().mockResolvedValue(undefined),
    createThread: vi.fn(),
    renameThread: vi.fn(),
    deleteThread: vi.fn(),
    startPendingGenericThread: vi.fn(() => {
      useThreadsStore.setState({
        pendingNewSession: {
          backendKind: "generic_chat",
          cwd: "",
          projectName: "",
        },
      });
    }),
  });
});

describe("ThreadList", () => {
  it("空列表显示 '还没有会话'", () => {
    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route path="/chat" element={<ThreadList />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText("还没有会话")).toBeInTheDocument();
  });

  it("渲染 thread 名（updated_at 排序由 store 负责，这里只看渲染）", () => {
    useThreadsStore.setState({
      threads: [
        {
          id: "thread-aaaaaa",
          name: "alpha",
          preset_id: "p1",
          backend_kind: "generic_chat",
          claude_thread_id: "",
          codex_thread_id: "",
          cwd: "",
          created_at: 1,
          updated_at: 100,
          message_count: 0,
          is_pinned: false,
          is_archived: false,
        },
        {
          id: "thread-bbbbbb",
          name: "beta",
          preset_id: "p1",
          backend_kind: "generic_chat",
          claude_thread_id: "",
          codex_thread_id: "",
          cwd: "",
          created_at: 1,
          updated_at: 50,
          message_count: 0,
          is_pinned: false,
          is_archived: false,
        },
      ],
    });
    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route path="/chat" element={<ThreadList />} />
          <Route path="/chat/:thread_id" element={<ThreadList />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText("alpha")).toBeInTheDocument();
    expect(screen.getByText("beta")).toBeInTheDocument();
  });

  it("只渲染 backend_kind=generic_chat 的 thread，claude_code / codex 被过滤掉", () => {
    useThreadsStore.setState({
      threads: [
        {
          id: "thread-generic",
          name: "chat thread",
          preset_id: "p1",
          backend_kind: "generic_chat",
          claude_thread_id: "",
          codex_thread_id: "",
          cwd: "",
          created_at: 1,
          updated_at: 100,
          message_count: 0,
          is_pinned: false,
          is_archived: false,
        },
        {
          id: "thread-claude",
          name: "claude thread",
          preset_id: "",
          backend_kind: "claude_code",
          claude_thread_id: "claude-1",
          codex_thread_id: "",
          cwd: "/repo",
          created_at: 1,
          updated_at: 90,
          message_count: 0,
          is_pinned: false,
          is_archived: false,
        },
        {
          id: "thread-codex",
          name: "codex thread",
          preset_id: "",
          backend_kind: "codex",
          claude_thread_id: "",
          codex_thread_id: "codex-1",
          cwd: "/repo",
          created_at: 1,
          updated_at: 80,
          message_count: 0,
          is_pinned: false,
          is_archived: false,
        },
      ],
    });
    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route path="/chat" element={<ThreadList />} />
        </Routes>
      </MemoryRouter>,
    );
    // generic_chat thread 出现
    expect(screen.getByText("chat thread")).toBeInTheDocument();
    expect(screen.getByLabelText("普通聊天 会话")).toBeInTheDocument();
    // claude_code / codex backend_kind 的 thread 不应出现在通用 tab
    expect(screen.queryByText("claude thread")).not.toBeInTheDocument();
    expect(screen.queryByText("codex thread")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Claude 会话")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Codex 会话")).not.toBeInTheDocument();
  });

  it("已绑定 claude_thread_id 的旧 thread 也渲染 Claude 图标", () => {
    useThreadsStore.setState({
      threads: [
        {
          id: "thread-legacy-claude",
          name: "legacy claude thread",
          preset_id: "p1",
          backend_kind: "generic_chat",
          claude_thread_id: "claude-legacy-1",
          codex_thread_id: "",
          cwd: "/repo",
          created_at: 1,
          updated_at: 100,
          message_count: 0,
          is_pinned: false,
          is_archived: false,
        },
      ],
    });
    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route path="/chat" element={<ThreadList />} />
        </Routes>
      </MemoryRouter>,
    );

    const claudeBadge = screen.getByLabelText("Claude 会话");
    expect(claudeBadge.querySelector("img")).toHaveAttribute("src", "/brand/claude-app-icon.png");
  });

  it("点击新建对话进入 generic pending 空白页状态", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/chat/thread-aaaaaaaaaaaa"]}>
        <Routes>
          <Route path="/chat" element={<div data-testid="chat-root" />} />
          <Route path="/chat/:thread_id" element={<ThreadList />} />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "新建对话" }));

    expect(useThreadsStore.getState().startPendingGenericThread).toHaveBeenCalledTimes(1);
    expect(useThreadsStore.getState().pendingNewSession?.backendKind).toBe("generic_chat");
    expect(screen.getByTestId("chat-root")).toBeInTheDocument();
  });
});
