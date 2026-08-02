import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { StatusLine } from "@/components/StatusLine";
import { useChatStore } from "@/stores/chat";
import { useThreadsStore } from "@/stores/threads";

describe("StatusLine v2", () => {
  beforeEach(() => {
    useChatStore.setState({ itemsByThread: {}, usageByThread: {} });
    useThreadsStore.setState({ threads: [] });
  });

  it("Claude provider 渲染：context_window=0 走 '上下文 X' 占位", () => {
    const tid = "thread-aaaaaaaaaaaa";
    useChatStore.setState({
      usageByThread: {
        [tid]: {
          provider: "claude",
          input_tokens: 6,
          output_tokens: 881,
          cache_read_input_tokens: 950000,
          cache_creation_input_tokens: 3194,
          cache_creation: {
            ephemeral_1h_input_tokens: 3194,
            ephemeral_5m_input_tokens: 0,
          },
          context_usage: 953200,
          model: "",
          context_window: 0, // 未知模型
        },
      },
    });

    render(<StatusLine threadId={tid} />);
    expect(screen.getByText(/上下文/)).toBeTruthy();
  });

  it("Claude provider 渲染：已知 context_window 显示百分比进度条", () => {
    const tid = "thread-bbbbbbbbbbbb";
    useChatStore.setState({
      usageByThread: {
        [tid]: {
          provider: "claude",
          input_tokens: 100,
          output_tokens: 50,
          cache_read_input_tokens: 400000,
          cache_creation_input_tokens: 99900,
          cache_creation: {
            ephemeral_1h_input_tokens: 99900,
            ephemeral_5m_input_tokens: 0,
          },
          context_usage: 500000,
          model: "claude-opus-4",
          context_window: 1000000,
        },
      },
    });

    render(<StatusLine threadId={tid} />);
    const el = screen.getByText(/上下文/);
    expect(el).toBeTruthy();
    expect(el.textContent).toContain("上下文");
  });

  it("Claude provider 渲染：1h 和 5m TTL 都 > 0 时两个细分都显示", () => {
    const tid = "thread-cccccccccccc";
    useChatStore.setState({
      usageByThread: {
        [tid]: {
          provider: "claude",
          input_tokens: 10,
          output_tokens: 5,
          cache_read_input_tokens: 0,
          cache_creation_input_tokens: 3500,
          cache_creation: {
            ephemeral_1h_input_tokens: 3000,
            ephemeral_5m_input_tokens: 500,
          },
          context_usage: 3510,
          model: "claude-sonnet-4-5",
          context_window: 200000,
        },
      },
    });

    render(<StatusLine threadId={tid} />);
    expect(screen.getByText(/1h 3\.0K/)).toBeTruthy();
    expect(screen.getByText(/5m 500/)).toBeTruthy();
  });

  it("Claude provider 渲染：1h=0 / 5m>0 只显示 5m 细分", () => {
    const tid = "thread-dddddddddddd";
    useChatStore.setState({
      usageByThread: {
        [tid]: {
          provider: "claude",
          input_tokens: 10,
          output_tokens: 5,
          cache_read_input_tokens: 0,
          cache_creation_input_tokens: 200,
          cache_creation: {
            ephemeral_1h_input_tokens: 0,
            ephemeral_5m_input_tokens: 200,
          },
          context_usage: 210,
          model: "claude-sonnet-4-5",
          context_window: 200000,
        },
      },
    });

    render(<StatusLine threadId={tid} />);
    expect(screen.queryByText(/1h /)).toBeNull();
    expect(screen.getByText(/5m 200/)).toBeTruthy();
  });

  it("generic_chat 未报告指标显示为空态，cache hit 保留真实数值", () => {
    const tid = "thread-unknown-usage";
    useChatStore.setState({
      usageByThread: {
        [tid]: {
          provider: "claude",
          input_tokens: 194,
          output_tokens: 119,
          cache_read_input_tokens: 9088,
          cache_creation_input_tokens: null,
          cache_creation: {
            ephemeral_1h_input_tokens: null,
            ephemeral_5m_input_tokens: null,
          },
          context_usage: null,
          model: "MiniMax-M3",
          context_window: 204800,
        },
      },
    });

    render(<StatusLine threadId={tid} />);
    expect(screen.getByText("缓存命中 9.1K")).toBeTruthy();
    expect(screen.getByText("缓存写入 —")).toBeTruthy();
    expect(screen.getByText("上下文 —")).toBeTruthy();
  });

  it("usage=null 时底栏显示「等待对话」占位（不带模型名）", () => {
    const tid = "thread-eeeeeeeeeeee";
    // 不设 usage、不设 thread → modelName 为空
    render(<StatusLine threadId={tid} />);
    expect(screen.getByText("等待对话")).toBeTruthy();
  });

  it("模型名 stat：usage.model 非空时显示 usage.model（claude）", () => {
    const tid = "thread-ffffffffffff";
    useChatStore.setState({
      usageByThread: {
        [tid]: {
          provider: "claude",
          input_tokens: 10,
          output_tokens: 5,
          cache_read_input_tokens: 0,
          cache_creation_input_tokens: 0,
          cache_creation: {
            ephemeral_1h_input_tokens: 0,
            ephemeral_5m_input_tokens: 0,
          },
          context_usage: 10,
          model: "claude-sonnet-4-5",
          context_window: 200000,
        },
      },
    });
    render(<StatusLine threadId={tid} />);
    expect(screen.getByText("claude-sonnet-4-5")).toBeTruthy();
  });

  it("模型名 stat：Codex usage 没 model 字段，fallback 到 threadModel (preset_id)", () => {
    const tid = "thread-111111111111";
    useThreadsStore.setState({
      threads: [
        {
          id: tid,
          name: "codex t",
          preset_id: "gpt-5-codex",
          backend_kind: "codex",
          claude_thread_id: "",
          codex_thread_id: "abc",
          cwd: "/tmp",
          created_at: 0,
          updated_at: 0,
          message_count: 0,
          is_pinned: false,
          is_archived: false,
        },
      ],
    });
    useChatStore.setState({
      usageByThread: {
        [tid]: {
          provider: "openai",
          total: {
            input_tokens: 100,
            cached_input_tokens: 50,
            output_tokens: 20,
            reasoning_output_tokens: 5,
            total_tokens: 175,
          },
          last: {
            input_tokens: 100,
            cached_input_tokens: 50,
            output_tokens: 20,
            reasoning_output_tokens: 5,
            total_tokens: 175,
          },
          model_context_window: 400000,
          rate_limits: null,
        },
      },
    });
    render(<StatusLine threadId={tid} />);
    expect(screen.getByText("gpt-5-codex")).toBeTruthy();
  });

  it("模型名 stat：usage.model 为空字符串时 fallback 到 threadModel", () => {
    const tid = "thread-222222222222";
    useThreadsStore.setState({
      threads: [
        {
          id: tid,
          name: "claude t",
          preset_id: "claude-fallback",
          backend_kind: "claude_code",
          claude_thread_id: "x",
          codex_thread_id: "",
          cwd: "/tmp",
          created_at: 0,
          updated_at: 0,
          message_count: 0,
          is_pinned: false,
          is_archived: false,
        },
      ],
    });
    useChatStore.setState({
      usageByThread: {
        [tid]: {
          provider: "claude",
          input_tokens: 1,
          output_tokens: 1,
          cache_read_input_tokens: 0,
          cache_creation_input_tokens: 0,
          cache_creation: {
            ephemeral_1h_input_tokens: 0,
            ephemeral_5m_input_tokens: 0,
          },
          context_usage: 1,
          model: "",
          context_window: 200000,
        },
      },
    });
    render(<StatusLine threadId={tid} />);
    expect(screen.getByText("claude-fallback")).toBeTruthy();
  });
});
