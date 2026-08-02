/**
 * chat-receive-side-unify #4 + #5（provider 部分）测试
 *
 * #4 loadHistory 真实实现：
 *   - Claude：apiGet /api/threads/{threadId}/claude_history → NormalizedMessage[]
 *     → normalizedMessageToEvents → ChatHistoryBatch.events
 *   - Codex：apiGet /api/codex/sessions/{codex_thread_id}/history（codex_thread_id
 *     来自 thread metadata，经 request 透传）→ 同上
 *   - Generic：保持空 batch（历史走 thread.history 帧）
 *
 * #5 GenericChatProvider.mapInboundFrame 帧覆盖补全：
 *   - usage → status 事件（noticeKey usage:{turn}，携带 ThreadUsage）
 *   - run.interrupted → turn_completed 事件（复位 activeStreamingTurnId / streaming=false）
 *   - cell.evicted → toast-only 副作用，不进时间线（返回 []）
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import type {
  NormalizedMessage,
  UsageFrame,
  RunInterruptedFrame,
  CellEvictedFrame,
  ClaudeUsage,
  SystemNoticeFrame,
} from "@/protocol";
import type { HistoryLoadRequest, RawFrameEnvelope } from "@/chat/types";

// apiGet mock（loadHistory 走网络）
vi.mock("@/lib/api", () => ({
  apiGet: vi.fn(),
}));
import { apiGet } from "@/lib/api";
import { getChatProvider } from "../index";

const apiGetMock = apiGet as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  apiGetMock.mockReset();
});

function genericEnv(frame: unknown, threadId = "t1"): RawFrameEnvelope {
  return {
    connectionId: threadId,
    channel: "generic",
    threadId,
    frame,
    receivedAt: 100,
  };
}

// ===========================================================================
// #4 Claude loadHistory
// ===========================================================================

describe("ClaudeChatProvider.loadHistory（#4）", () => {
  const p = getChatProvider("claude");

  it("拉 /api/threads/{threadId}/claude_history 并翻译成 ChatHistoryBatch.events", async () => {
    const messages: NormalizedMessage[] = [
      { frame_type: "text", role: "assistant", content: "hello", sessionId: "s1" },
      { frame_type: "tool_use", toolId: "tu1", toolName: "Read", toolInput: { path: "x" }, sessionId: "s1" },
    ];
    apiGetMock.mockResolvedValue({ messages });

    const req: HistoryLoadRequest = { threadId: "t1", provider: "claude" };
    const batch = await p.loadHistory(req);

    expect(apiGetMock).toHaveBeenCalledWith("/api/threads/t1/claude_history");
    expect(batch.threadId).toBe("t1");
    expect(batch.provider).toBe("claude");
    expect(batch.hasMore).toBe(false);
    // 2 条 NormalizedMessage → 至少 text + tool_use 各一事件
    expect(batch.events.length).toBe(2);
    expect(batch.events[0]).toMatchObject({ kind: "assistant_message_delta", provider: "claude" });
    expect(batch.events[1]).toMatchObject({ kind: "tool_call_started", provider: "claude", toolCallId: "tu1" });
  });

  it("空历史 → 空 events", async () => {
    apiGetMock.mockResolvedValue({ messages: [] });
    const batch = await p.loadHistory({ threadId: "t9", provider: "claude" });
    expect(batch.events).toEqual([]);
  });
});

// ===========================================================================
// #4 Codex loadHistory（codex_thread_id 来自 metadata，经 request 透传）
// ===========================================================================

describe("CodexChatProvider.loadHistory（#4）", () => {
  const p = getChatProvider("codex");

  it("拉 /api/codex/sessions/{codex_thread_id}/history（codexThreadId 经 request 透传）", async () => {
    const messages: NormalizedMessage[] = [
      { frame_type: "text", role: "assistant", content: "done", sessionId: "cx-sess" },
    ];
    apiGetMock.mockResolvedValue({ messages });

    const req = {
      threadId: "t1",
      provider: "codex" as const,
      codexThreadId: "cx-9",
    } as HistoryLoadRequest & { codexThreadId: string };
    const batch = await p.loadHistory(req);

    expect(apiGetMock).toHaveBeenCalledWith("/api/codex/sessions/cx-9/history");
    expect(batch.provider).toBe("codex");
    expect(batch.events[0]).toMatchObject({ kind: "assistant_message_delta", provider: "codex" });
  });

  it("缺 codexThreadId → 不发请求，返回空 batch", async () => {
    const batch = await p.loadHistory({ threadId: "t1", provider: "codex" });
    expect(apiGetMock).not.toHaveBeenCalled();
    expect(batch.events).toEqual([]);
    expect(batch.hasMore).toBe(false);
  });
});

// ===========================================================================
// #4 Generic loadHistory（保持空 batch，历史走 thread.history 帧）
// ===========================================================================

describe("GenericChatProvider.loadHistory（#4）", () => {
  const p = getChatProvider("generic");

  it("不发网络请求，返回空 batch（历史由 thread.history 帧推送）", async () => {
    const batch = await p.loadHistory({ threadId: "t1", provider: "generic" });
    expect(apiGetMock).not.toHaveBeenCalled();
    expect(batch.events).toEqual([]);
    expect(batch.hasMore).toBe(false);
  });
});

// ===========================================================================
// #5 GenericChatProvider.mapInboundFrame 帧覆盖
// ===========================================================================

describe("GenericChatProvider.mapInboundFrame 帧覆盖（#5）", () => {
  const p = getChatProvider("generic");

  it("system.notice 完整保留进化处理入口所需字段并归一完成状态", () => {
    const frame: SystemNoticeFrame = {
      frame_type: "system.notice",
      timestamp_ms: 40,
      notice_key: "self_evolution.review",
      source: "self_evolution",
      status: "completed",
      title: "进化复盘",
      message: "发现 2 条进化养料",
      details: {
        review_id: "evo-review:run-thread-demo-20",
        nutrients_written: 2,
      },
      icon: "success",
      run_id: "run-thread-demo-20",
    };

    const events = p.mapInboundFrame(genericEnv(frame));

    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ kind: "status", provider: "generic" });
    expect(events[0].payload).toMatchObject({
      noticeKey: "self_evolution.review",
      source: "self_evolution",
      status: "success",
      title: "进化复盘",
      message: "发现 2 条进化养料",
      details: {
        review_id: "evo-review:run-thread-demo-20",
        nutrients_written: 2,
      },
      icon: "success",
    });
    expect(events[0].runId).toBe("run-thread-demo-20");
  });

  it("usage 帧 → status 事件（noticeKey usage:{turn}，携带 ThreadUsage）", () => {
    const usage: ClaudeUsage = {
      provider: "claude",
      input_tokens: 10,
      output_tokens: 20,
      cache_read_input_tokens: 0,
      cache_creation_input_tokens: 0,
      cache_creation: { ephemeral_1h_input_tokens: 0, ephemeral_5m_input_tokens: 0 },
      context_usage: 30,
      model: "claude-x",
      context_window: 200000,
    };
    const frame: UsageFrame = {
      frame_type: "usage",
      timestamp_ms: 50,
      turn: 2,
      run_id: "r1",
      usage,
    };
    const events = p.mapInboundFrame(genericEnv(frame));
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ kind: "status", provider: "generic" });
    expect(events[0].payload.noticeKey).toBe("usage:2");
    expect(events[0].payload.usage).toEqual(usage);
    expect(events[0].turnId).toBe("r1:turn-2");
    expect(events[0].runId).toBe("r1");
    expect(events[0].turn).toBe(2);
  });

  it("run.interrupted 帧 → turn_completed 事件（turnId=run+turn，复位 streaming）", () => {
    const frame: RunInterruptedFrame = {
      frame_type: "run.interrupted",
      timestamp_ms: 70,
      run_id: "r5",
      cancelled_at_turn: 4,
      cancelled_tool_call_id: null,
      cancel_reason: "user_interrupt",
    };
    const events = p.mapInboundFrame(genericEnv(frame));
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ kind: "turn_completed", provider: "generic" });
    expect(events[0].turnId).toBe("r5:turn-4");
    expect(events[0].runId).toBe("r5");
    expect(events[0].turn).toBe(4);
    expect(events[0].payload.cancelled).toBe(true);
    expect(events[0].payload.cancelReason).toBe("user_interrupt");
  });

  it("cell.evicted 帧 → toast-only 副作用，不进时间线（返回 []）", () => {
    const frame: CellEvictedFrame = {
      frame_type: "cell.evicted",
      timestamp_ms: 80,
      thread_id: "t1",
      reason: "idle",
      message: "回收",
    };
    const events = p.mapInboundFrame(genericEnv(frame));
    expect(events).toEqual([]);
  });

  it("pong 帧 → 心跳，不进时间线（返回 []）", () => {
    const events = p.mapInboundFrame(genericEnv({ frame_type: "pong", timestamp_ms: 1 }));
    expect(events).toEqual([]);
  });
});
