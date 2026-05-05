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
          sdk_session_id: "",
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
          sdk_session_id: "",
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
});
