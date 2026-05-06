import { render, screen } from "@testing-library/react";
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
          cumulative_prompt_tokens: 0,
          cumulative_completion_tokens: 0,
          cumulative_total_tokens: 0,
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
          cumulative_prompt_tokens: 0,
          cumulative_completion_tokens: 0,
          cumulative_total_tokens: 0,
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

  it("按 backend_kind 渲染来源图标", () => {
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
          cumulative_prompt_tokens: 0,
          cumulative_completion_tokens: 0,
          cumulative_total_tokens: 0,
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
          cumulative_prompt_tokens: 0,
          cumulative_completion_tokens: 0,
          cumulative_total_tokens: 0,
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
          cumulative_prompt_tokens: 0,
          cumulative_completion_tokens: 0,
          cumulative_total_tokens: 0,
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
    expect(screen.getByLabelText("普通聊天 会话")).toBeInTheDocument();

    const claudeBadge = screen.getByLabelText("Claude 会话");
    const codexBadge = screen.getByLabelText("Codex 会话");
    expect(claudeBadge).toBeInTheDocument();
    expect(codexBadge).toBeInTheDocument();
    expect(claudeBadge.querySelector("img")).toHaveAttribute("src", "/brand/claude-app-icon.png");
    expect(codexBadge.querySelector("img")).toHaveAttribute("src", "/brand/codex-app-icon.png");
  });
});
