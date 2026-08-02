import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ClaudeCodeView } from "@/components/ClaudeCodeView";
import type { NormalizedMessage, ThreadMetadataDTO } from "@/protocol";
import { useThreadStatusStore } from "@/stores/threadStatus";
import { useThreadsStore } from "@/stores/threads";
import type { InitialMessageDraft } from "@/stores/threads";

const mockApiGet = vi.fn();
const mockUseClaudeCodeWS = vi.fn();

vi.mock("@/lib/api", () => ({
  apiGet: (...args: unknown[]) => mockApiGet(...args),
}));

vi.mock("@/hooks/useClaudeCodeWS", () => ({
  useClaudeCodeWS: (...args: unknown[]) => mockUseClaudeCodeWS(...args),
}));

/**
 * 复用 input_json 测试的 socket fixture 模式：截获 socket.on(cb) 拿到 listener，
 * 让测试可以通过 inject(frame) 直接喂 NormalizedMessage 触发 ClaudeCodeView 状态机。
 */
interface SocketHandle {
  socket: { on: ReturnType<typeof vi.fn>; send: ReturnType<typeof vi.fn> };
  inject: (frame: NormalizedMessage) => void;
}

function makeSocket(): SocketHandle {
  let listener: ((frame: NormalizedMessage) => void) | null = null;
  const socket = {
    on: vi.fn((cb: (frame: NormalizedMessage) => void) => {
      listener = cb;
      return () => {
        listener = null;
      };
    }),
    send: vi.fn(),
  };
  return {
    socket,
    inject: (frame) => {
      act(() => {
        listener?.(frame);
      });
    },
  };
}

function initialDraft(text: string): InitialMessageDraft {
  return {
    text,
    reasoningEffort: null,
    restoreDraft: {
      text,
      reasoningEffort: null,
      attachments: [],
      references: [],
    },
  };
}

async function mountStreamingView(
  handle: SocketHandle,
  threadOverrides: Partial<ThreadMetadataDTO> = {},
) {
  mockUseClaudeCodeWS.mockReturnValue({
    socket: handle.socket,
    state: "open",
  });
  mockApiGet.mockResolvedValue({ messages: [] });

  render(
    <MemoryRouter>
      <ClaudeCodeView
        threadId="thread-1"
        thread={{ claude_thread_id: "sdk-1", ...threadOverrides } as ThreadMetadataDTO}
      />
    </MemoryRouter>,
  );

  await waitFor(() => expect(handle.socket.on).toHaveBeenCalled());

  // chat-running-state-unify #3：isRunning 已切到三频道共享 useThreadRunning
  // （后端 /ws/thread-status phase 真源）。真实环境中 stream_status 到达时后端
  // 也通过 thread-status 广播 phase=responding——单元测试在此显式同步该真源，
  // 让 useThreadRunning(threadId)=true → Composer 收到 isRunning=true → 渲染 Stop。
  // stream_status 帧仍 inject（保留对 streamPhase / UI 文案的副作用测试）。
  handle.inject({
    frame_type: "stream_status",
    phase: "responding",
  } as unknown as NormalizedMessage);
  act(() => {
    useThreadStatusStore.getState().applyStatus({
      frame_type: "thread-status",
      threadId: "thread-1",
      phase: "responding",
      sequence: 1,
      runId: "run-1",
      runGeneration: 1,
    }, 1);
  });
}

describe("ClaudeCodeView", () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    mockUseClaudeCodeWS.mockReset();
    // chat-running-state-unify #3：清 thread-status 真源，避免测试间残留 phase 污染 isRunning。
    useThreadStatusStore.setState({
      statuses: {},
      connectionGeneration: 1,
      lastSequence: 0,
    });
    useThreadsStore.setState({
      pendingNewSession: null,
      initialMessage: null,
    });
  });

  it("创建后的首条消息发送失败时恢复草稿且不生成用户气泡", async () => {
    const handle = makeSocket();
    handle.socket.send.mockImplementation(() => {
      throw new Error("claude transport down");
    });
    const fetchThreads = vi.fn();
    mockUseClaudeCodeWS.mockReturnValue({
      socket: handle.socket,
      state: "open",
    });
    mockApiGet.mockResolvedValue({ messages: [] });
    useThreadsStore.setState({
      initialMessage: initialDraft("需要恢复的 Claude 首条消息"),
      fetchThreads,
    });

    render(
      <MemoryRouter>
        <ClaudeCodeView
          threadId="thread-1"
          thread={{ claude_thread_id: "sdk-1" } as ThreadMetadataDTO}
        />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(screen.getByLabelText("消息输入")).toHaveValue(
        "需要恢复的 Claude 首条消息",
      ),
    );
    expect(fetchThreads).not.toHaveBeenCalled();
    expect(screen.getAllByText("需要恢复的 Claude 首条消息")).toHaveLength(1);
  });

  it("历史消息复用通用聊天 renderer，并把 tool_result 合并到工具卡片", async () => {
    const socket = {
      on: vi.fn(() => () => {}),
      send: vi.fn(),
    };
    const history: NormalizedMessage[] = [
      {
        frame_type: "text",
        role: "assistant",
        content: "**历史回复**",
        timestamp: "2026-05-02T00:00:00Z",
      },
      {
        frame_type: "tool_use",
        toolId: "tool-1",
        toolName: "Shell",
        toolInput: { cmd: "ls" },
        timestamp: "2026-05-02T00:00:01Z",
      },
      {
        frame_type: "tool_result",
        toolId: "tool-1",
        content: "done",
        timestamp: "2026-05-02T00:00:02Z",
      },
    ];

    mockUseClaudeCodeWS.mockReturnValue({
      socket,
      state: "open",
    });
    mockApiGet.mockResolvedValue({ messages: history });

    render(
      <MemoryRouter>
        <ClaudeCodeView
          threadId="thread-1"
          thread={{ claude_thread_id: "sdk-1" } as ThreadMetadataDTO}
        />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(mockApiGet).toHaveBeenCalledWith(
        "/api/threads/thread-1/claude_history",
      ),
    );

    expect(await screen.findByText("历史回复")).toBeInTheDocument();

    const toolButton = await screen.findByRole("button", { name: /Shell/ });
    await userEvent.click(toolButton);

    expect(screen.getByText("done")).toBeInTheDocument();
    expect(screen.getByLabelText("消息输入")).toBeInTheDocument();
    expect(screen.getByTestId("claude-code-layout").className).toContain("min-h-0");
    expect(screen.getByTestId("claude-code-viewport").className).toContain("overflow-hidden");
  });

  /**
   * interrupt-claude-channel-v0.1 dev-checklist #6：覆盖 #4+#5 改造后 Stop 按钮契约。
   *
   * 测的是 Composer 收到 `disabled+isRunning+onInterrupt` 三条件 → showStopButton=true
   * 的整链路（ClaudeCodeView 推 isRunning/onInterrupt 给 Composer）。
   */
  describe("Stop 按钮（interrupt-claude-channel-v0.1 #4+#5）", () => {
    it("(a) streaming 中 Stop 按钮可见，Send 按钮被替换", async () => {
      const handle = makeSocket();
      await mountStreamingView(handle);

      // streamPhase=responding → isRunning=true → showStopButton=true
      await waitFor(() => {
        expect(screen.queryByTestId("composer-stop")).not.toBeNull();
      });
      // Stop 渲染时 Send 不渲染（同位置二选一）
      expect(screen.queryByTestId("composer-send")).toBeNull();
    });

    it("(b) 点击 Stop 按钮 → 发 abort-session 帧，sessionId 优先 claude_thread_id", async () => {
      const handle = makeSocket();
      await mountStreamingView(handle, { claude_thread_id: "sdk-real-1" });

      const stopBtn = await screen.findByTestId("composer-stop");
      await userEvent.click(stopBtn);

      // socket.send 被调一次，参数为 abort-session 帧
      expect(handle.socket.send).toHaveBeenCalledTimes(1);
      expect(handle.socket.send).toHaveBeenCalledWith({
        frame_type: "abort-session",
        sessionId: "sdk-real-1",
      });
    });

    it("(c) 收 complete.aborted=true → Stop 消失、Send 复现", async () => {
      const handle = makeSocket();
      await mountStreamingView(handle);

      // 前提：Stop 已可见
      await waitFor(() => {
        expect(screen.queryByTestId("composer-stop")).not.toBeNull();
      });

      // 模拟 SDK 收到 complete.aborted=true → streamPhase 切回 idle → isRunning=false
      handle.inject({
        frame_type: "complete",
        aborted: true,
      } as unknown as NormalizedMessage);

      // 下一个 render：Stop 应消失，Send 应可见
      await waitFor(() => {
        expect(screen.queryByTestId("composer-stop")).toBeNull();
      });
      expect(screen.queryByTestId("composer-send")).not.toBeNull();
    });

    it("(c2) reconnect 后 session-status=false 会清掉 running 态", async () => {
      const handle = makeSocket();
      await mountStreamingView(handle);

      await waitFor(() => {
        expect(screen.queryByTestId("composer-stop")).not.toBeNull();
      });

      handle.inject({
        frame_type: "session-status",
        sessionId: "sdk-1",
        isProcessing: false,
      } as unknown as NormalizedMessage);
      // chat-running-state-unify #3：reconnect 真实环境下，后端在 session-status
      // isProcessing=false 同时也会通过 /ws/thread-status 广播 phase=idle，让
      // useThreadRunning(threadId)=false → Stop 消失。单元测试显式模拟这个同步。
      act(() => {
        useThreadStatusStore.getState().applyStatus({
          frame_type: "thread-status",
          threadId: "thread-1",
          phase: "idle",
          sequence: 2,
          runId: "run-1",
          runGeneration: 1,
        }, 1);
      });

      await waitFor(() => {
        expect(screen.queryByTestId("composer-stop")).toBeNull();
      });
      expect(screen.queryByTestId("composer-send")).not.toBeNull();
    });

    it("(d) 幂等：300ms 内连点 3 次只发 1 个 abort-session 帧", async () => {
      const handle = makeSocket();
      await mountStreamingView(handle);

      const stopBtn = await screen.findByTestId("composer-stop");

      // 同步连点 3 次（同一 tick 内 Date.now() 不变 → onInterrupt 内 300ms gate 触发）
      // 用 fireEvent.click 而非 userEvent，避免 userEvent 每次 click 内部插入 await。
      fireEvent.click(stopBtn);
      fireEvent.click(stopBtn);
      fireEvent.click(stopBtn);

      // 300ms 节流：socket.send 只调 1 次
      expect(handle.socket.send).toHaveBeenCalledTimes(1);
      expect(handle.socket.send).toHaveBeenCalledWith({
        frame_type: "abort-session",
        sessionId: "sdk-1",
      });
    });
  });

  /**
   * fix-claude-stop-button-stuck：复现 stop 按钮卡死 bug 及修复后行为。
   *
   * 真因：原 `isRunning` 仅依赖 `streamPhase !== "idle" || streaming===true`，
   * 当后端在 `complete` 帧到达后仍触发非 idle 信号（竞态/补帧/异常），按钮永不消失。
   *
   * 修复：引入 `isConversationEnded`（items 末尾倒查到 complete 即 true，遇 user 截断）
   * 作为 "对话是否结束" 的唯一语义真源；`isRunning` 出态严判 → complete 到达即回 send。
   */
  describe("fix-claude-stop-button-stuck", () => {
    it("(e) complete 帧到达后即使再有 stream_status 帧，Stop 按钮也不重新出现", async () => {
      const handle = makeSocket();
      await mountStreamingView(handle);

      // 前提：Stop 已可见
      await waitFor(() => {
        expect(screen.queryByTestId("composer-stop")).not.toBeNull();
      });

      // complete 帧 → "对话结束" 气泡显示 → Stop 应消失
      handle.inject({
        frame_type: "complete",
        aborted: false,
      } as unknown as NormalizedMessage);

      await waitFor(() => {
        expect(screen.queryByTestId("composer-stop")).toBeNull();
      });
      expect(screen.queryByTestId("composer-send")).not.toBeNull();

      // 关键回归：模拟后端竞态——complete 后又来 stream_status(responding)。
      // 修复前：streamPhase 被设回 "responding" → isRunning=true → Stop 重新出现（bug）
      // 修复后：isConversationEnded=true → isRunning 短路返回 false → Stop 保持隐藏
      handle.inject({
        frame_type: "stream_status",
        phase: "responding",
      } as unknown as NormalizedMessage);

      // 给 React 一个 tick 重新渲染
      await new Promise((resolve) => setTimeout(resolve, 0));

      expect(screen.queryByTestId("composer-stop")).toBeNull();
      expect(screen.queryByTestId("composer-send")).not.toBeNull();
    });

    it("(f) complete 后用户发新消息，新一轮 Stop 按钮可正常出现", async () => {
      const handle = makeSocket();
      await mountStreamingView(handle);

      // 第一轮：complete 关闭
      handle.inject({
        frame_type: "complete",
        aborted: false,
      } as unknown as NormalizedMessage);
      await waitFor(() => {
        expect(screen.queryByTestId("composer-stop")).toBeNull();
      });

      // 用户发新消息 → text role=user 注入（模拟新一轮开始）
      // 注：实际触发来自 Composer onSubmit，这里直接喂 text 帧等价 items 出现 user kind
      handle.inject({
        frame_type: "text",
        role: "user",
        content: "再来一轮",
        timestamp: "2026-05-25T00:00:00Z",
      } as unknown as NormalizedMessage);

      // 新一轮 streaming → Stop 应正常出现（isConversationEnded 被 user item 截断）
      handle.inject({
        frame_type: "stream_status",
        phase: "responding",
      } as unknown as NormalizedMessage);

      await waitFor(() => {
        expect(screen.queryByTestId("composer-stop")).not.toBeNull();
      });
      expect(screen.queryByTestId("composer-send")).toBeNull();
    });

    it("(g) stream_end 帧同步清 streamPhase（对称漏洞修复）", async () => {
      const handle = makeSocket();
      await mountStreamingView(handle);

      // streamPhase=responding → 顶部 "生成中..." 提示可见
      await waitFor(() => {
        expect(screen.getByText("生成中...")).toBeInTheDocument();
      });

      // stream_end 帧 → streamPhase 应被清回 idle → 提示应消失
      handle.inject({
        frame_type: "stream_end",
      } as unknown as NormalizedMessage);

      await waitFor(() => {
        expect(screen.queryByText("生成中...")).not.toBeInTheDocument();
      });
    });
  });
});
