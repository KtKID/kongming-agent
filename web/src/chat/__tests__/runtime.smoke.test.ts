/**
 * 骨架闭环 smoke：ChatManager + ChatTimelineStore + GenericChatProvider 串起来能跑。
 *
 * 证明骨架不是空架子：一条 generic 消息能发出 user.input 帧；一串 generic S2C
 * 入站帧能被归并成一条 completed assistant 消息且文本正确拼接。
 * 全面的 provider/首发时机/session 测试归 #6。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { ChatManager } from "../ChatManager";
import { ChatTimelineStore } from "../ChatTimelineStore";
import { toViewModel, toGenericRenderItems } from "../ChatRenderAdapter";
import { makeCronTimelineKey } from "../runtimeWiring";
import type {
  NetworkHandle,
  RawFrameEnvelope,
  SendRequest,
  ConversationReferenceDTO,
  UserInputAttachment,
} from "../types";
import { useThreadStatusStore } from "@/stores/threadStatus";
import { useThreadDispatchStore } from "@/stores/threadDispatch";

beforeEach(() => {
  useThreadStatusStore.setState({ statuses: {} });
  useThreadDispatchStore.getState().clear();
});

function makeManager(threadId: string) {
  const store = new ChatTimelineStore(threadId);
  const sent: unknown[] = [];
  const handle: NetworkHandle = {
    connectionId: threadId,
    send: (frame) => sent.push(frame),
    close: vi.fn(),
  };
  const manager = new ChatManager({
    resolveHandle: () => handle,
    ensureThread: async (req) => req.provider.threadId,
    timelineFor: () => store,
  });
  return { manager, store, sent };
}

const skillReference: ConversationReferenceDTO = {
  id: "ref-1",
  kind: "skill",
  ref: "skill:skill-creator",
  label: "Skill Creator",
  activation: "inject_context",
};

function env(threadId: string, frame: unknown): RawFrameEnvelope {
  return {
    connectionId: threadId,
    channel: "generic",
    threadId,
    frame,
    receivedAt: 1_700_000_000_000,
  };
}

function pendingStartedFrame(
  content: string,
  metadata: Record<string, unknown> = {},
  timestamps: {
    timestampMs?: number;
    createdAtMs?: number;
    updatedAtMs?: number;
  } = {},
) {
  const timestampMs = timestamps.timestampMs ?? 1_700_000_000_000;
  const createdAtMs = timestamps.createdAtMs ?? 1_700_000_000_000;
  const updatedAtMs = timestamps.updatedAtMs ?? 1_700_000_000_001;
  return {
    frame_type: "pending-input.started",
    timestamp_ms: timestampMs,
    thread_id: "t1",
    pending_input_id: "pin-1",
    pending_input: {
      id: "pin-1",
      thread_id: "t1",
      source: "user_input",
      priority: "user_message",
      content,
      preview: content,
      status: "starting",
      created_at_ms: createdAtMs,
      updated_at_ms: updatedAtMs,
      sequence: 1,
      metadata,
    },
    run_id: "",
    version: 2,
  };
}

function pendingSteeredFrame(
  content: string,
  metadata: Record<string, unknown> = {},
  timestamps: {
    timestampMs?: number;
    createdAtMs?: number;
    updatedAtMs?: number;
    runId?: string;
    turn?: number | null;
    activeRunId?: string | null;
  } = {},
) {
  const timestampMs = timestamps.timestampMs ?? 1_700_000_000_000;
  const createdAtMs = timestamps.createdAtMs ?? 1_700_000_000_000;
  const updatedAtMs = timestamps.updatedAtMs ?? 1_700_000_000_001;
  const runId = timestamps.runId ?? "";
  const turn = timestamps.turn ?? null;
  return {
    frame_type: "pending-input.steered",
    timestamp_ms: timestampMs,
    thread_id: "t1",
    pending_input_id: "pin-1",
    pending_input: {
      id: "pin-1",
      thread_id: "t1",
      source: "user_input",
      priority: "user_message",
      content,
      preview: content,
      status: "starting",
      created_at_ms: createdAtMs,
      updated_at_ms: updatedAtMs,
      sequence: 1,
      metadata,
    },
    active_run_id: timestamps.activeRunId ?? (runId || null),
    run_id: runId,
    turn,
    version: 2,
  };
}

describe("chat 运行时骨架 · 闭环 smoke", () => {
  it("evolution 完成通知贯穿 provider 和 timeline 后保留处理入口字段", () => {
    const { manager, store } = makeManager("t1");

    manager.ingestFrame(
      env("t1", {
        frame_type: "system.notice",
        timestamp_ms: 1_700_000_000_000,
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
      }),
    );

    const items = toGenericRenderItems(toViewModel(store.snapshot()));

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      kind: "system",
      source: "self_evolution",
      status: "success",
      detailsData: {
        review_id: "evo-review:run-thread-demo-20",
        nutrients_written: 2,
      },
      icon: "success",
    });
  });

  it("generic 发送翻译成 user.input 帧", async () => {
    const { manager, sent } = makeManager("t1");
    const req: SendRequest = {
      common: { text: "你好", reasoningEffort: "high" },
      provider: { provider: "generic", threadId: "t1" },
    };
    await manager.sendMessage(req);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({
      frame_type: "user.input",
      text: "你好",
      reasoning_effort: "high",
    });
  });

  it("sendMessage 只写 dispatch 交互态，不写 canonical thread status", async () => {
    const { manager } = makeManager("t1");
    expect(useThreadStatusStore.getState().statuses["t1"]).toBeUndefined();
    await manager.sendMessage({
      common: { text: "hi" },
      provider: { provider: "generic", threadId: "t1" },
    });
    expect(useThreadStatusStore.getState().statuses["t1"]).toBeUndefined();
    expect(useThreadDispatchStore.getState().byThreadId["t1"]).toBeUndefined();
  });

  it("transport 失败保留 canonical idle 并把 dispatch 标记为可诊断 error", async () => {
    const store = new ChatTimelineStore("t1");
    const manager = new ChatManager({
      resolveHandle: () => ({
        connectionId: "t1",
        send: () => {
          throw new Error("socket closed");
        },
        close: vi.fn(),
      }),
      ensureThread: async (req) => req.provider.threadId,
      timelineFor: () => store,
    });

    await expect(
      manager.sendMessage({
        common: { text: "retry me" },
        provider: { provider: "generic", threadId: "t1" },
      }),
    ).rejects.toThrow("socket closed");

    expect(useThreadStatusStore.getState().statuses["t1"]).toBeUndefined();
    expect(useThreadDispatchStore.getState().byThreadId["t1"]).toEqual({
      phase: "error",
      message: "socket closed",
    });
  });

  it("generic sendMessage 等待 pending-input.started 后再生成用户气泡", async () => {
    const { manager, store } = makeManager("t1");
    await manager.sendMessage({
      common: { text: "555" },
      provider: { provider: "generic", threadId: "t1" },
    });
    expect(store.snapshot().orderedMessageIds).toHaveLength(0);

    manager.ingestFrame(env("t1", pendingStartedFrame("5555555")));
    const state = store.snapshot();
    const userRecords = Object.values(state.messagesById).filter(
      (m) => m.role === "user",
    );
    expect(userRecords).toHaveLength(1);
    expect(userRecords[0].parts).toContainEqual({ type: "text", text: "5555555" });
    // 投影回 GenericChatItem：一条 user 项，内容正确
    const items = toGenericRenderItems(toViewModel(state));
    const userItem = items.find((i) => i.kind === "user");
    expect(userItem).toBeDefined();
    expect(userItem && "content" in userItem ? userItem.content : "").toBe(
      "5555555",
    );
  });

  it("pending-input.started 携带 attachments → 投影回 GenericChatItem.attachments", async () => {
    const { manager, store } = makeManager("t1");
    const attachment: UserInputAttachment = {
      kind: "image",
      asset_id: "asset-1",
      mime_type: "image/png",
      preview_url: "/api/uploads/asset-1",
    } as UserInputAttachment;
    manager.ingestFrame(env("t1", pendingStartedFrame("看图", { attachments: [attachment] })));
    const items = toGenericRenderItems(toViewModel(store.snapshot()));
    const userItem = items.find((i) => i.kind === "user");
    expect(userItem).toBeDefined();
    if (userItem && userItem.kind === "user") {
      expect(userItem.attachments).toBeDefined();
      expect(userItem.attachments).toHaveLength(1);
      expect(userItem.attachments?.[0].asset_id).toBe("asset-1");
    }
  });

  it("pending-input.started 携带 references → 投影回 GenericChatItem.references", async () => {
    const { manager, store } = makeManager("t1");
    manager.ingestFrame(env("t1", pendingStartedFrame("", { references: [skillReference] })));
    const items = toGenericRenderItems(toViewModel(store.snapshot()));
    const userItem = items.find((i) => i.kind === "user");
    expect(userItem).toBeDefined();
    if (userItem && userItem.kind === "user") {
      expect(userItem.references).toEqual([skillReference]);
    }
  });

  it("pending-input.steered 立即生成已插队用户气泡并和后续 started 去重", () => {
    const { manager, store } = makeManager("t1");

    manager.ingestFrame(env("t1", pendingSteeredFrame("插队消息")));
    let items = toGenericRenderItems(toViewModel(store.snapshot()));
    let userItems = items.filter((i) => i.kind === "user");
    expect(userItems).toHaveLength(1);
    expect(
      userItems[0] && "content" in userItems[0] ? userItems[0].content : "",
    ).toBe("插队消息");
    expect(
      userItems[0] && "deliveryStatus" in userItems[0]
        ? userItems[0].deliveryStatus
        : undefined,
    ).toBe("steered");

    manager.ingestFrame(env("t1", pendingStartedFrame("插队消息")));
    items = toGenericRenderItems(toViewModel(store.snapshot()));
    userItems = items.filter((i) => i.kind === "user");
    expect(userItems).toHaveLength(1);
    expect(
      userItems[0] && "deliveryStatus" in userItems[0]
        ? userItems[0].deliveryStatus
        : undefined,
    ).toBeUndefined();
  });

  it("pending-input.steered 到达后立即生成气泡，后续同 id started 原位更新", () => {
    const { manager, store } = makeManager("t1");

    manager.ingestFrame(env("t1", {
      frame_type: "turn.start",
      timestamp_ms: 200,
      turn: 2,
      run_id: "run-2",
    }));
    manager.ingestFrame(env("t1", pendingSteeredFrame("插队功能2", {}, {
      timestampMs: 260,
      createdAtMs: 120,
      updatedAtMs: 150,
      runId: "run-2",
      turn: 2,
    })));
    manager.ingestFrame(env("t1", {
      frame_type: "assistant.final",
      timestamp_ms: 270,
      turn: 2,
      run_id: "run-2",
      content: "收到插队功能2",
    }));
    manager.ingestFrame(env("t1", pendingStartedFrame("插队功能2", {}, {
      timestampMs: 280,
      createdAtMs: 120,
      updatedAtMs: 280,
    })));

    const state = store.snapshot();
    expect(state.orderedMessageIds).toEqual(["pin-1", "run-2:turn-2:assistant"]);
    expect(state.messagesById["pin-1"].createdAt).toBe(260);
    expect(state.messagesById["pin-1"].turnId).toBe("run-2:turn-2");
    expect(state.messagesById["pin-1"].runId).toBe("run-2");
    expect(state.messagesById["pin-1"].turn).toBe(2);
  });

  it("pending-input.started 按到达顺序追加，普通排队气泡留在已完成回复后", () => {
    const { manager, store } = makeManager("t1");

    manager.ingestFrame(env("t1", {
      frame_type: "turn.start",
      timestamp_ms: 200,
      turn: 2,
      run_id: "run-2",
    }));
    manager.ingestFrame(env("t1", {
      frame_type: "assistant.final",
      timestamp_ms: 220,
      turn: 2,
      run_id: "run-2",
      content: "上一轮回复",
    }));
    manager.ingestFrame(env("t1", pendingStartedFrame("普通排队", {}, {
      timestampMs: 260,
      createdAtMs: 120,
      updatedAtMs: 150,
    })));

    const state = store.snapshot();
    expect(state.orderedMessageIds).toEqual(["run-2:turn-2:assistant", "pin-1"]);
    expect(state.messagesById["pin-1"].createdAt).toBe(260);
  });

  it("同一 run 的工具 turn 与后续回复 turn 分槽，插队用户气泡排在读取它的回复前", () => {
    const { manager, store } = makeManager("t1");

    manager.ingestFrame(env("t1", {
      frame_type: "turn.start",
      timestamp_ms: 100,
      turn: 1,
      run_id: "run-1",
    }));
    manager.ingestFrame(env("t1", {
      frame_type: "tool.call.start",
      timestamp_ms: 120,
      turn: 1,
      run_id: "run-1",
      tool_name: "read_file",
      call_id: "call-1",
      arguments: { path: "/tmp/a" },
    }));
    manager.ingestFrame(env("t1", {
      frame_type: "tool.call.end",
      timestamp_ms: 130,
      turn: 1,
      run_id: "run-1",
      call_id: "call-1",
      ok: true,
      content: "file body",
    }));
    manager.ingestFrame(env("t1", {
      frame_type: "turn.start",
      timestamp_ms: 200,
      turn: 2,
      run_id: "run-1",
    }));
    manager.ingestFrame(env("t1", pendingSteeredFrame("测试排队2", {}, {
      timestampMs: 210,
      createdAtMs: 150,
      updatedAtMs: 160,
      runId: "run-1",
      turn: 2,
    })));
    manager.ingestFrame(env("t1", {
      frame_type: "assistant.final",
      timestamp_ms: 220,
      turn: 2,
      run_id: "run-1",
      content: "收到测试排队2",
    }));

    const state = store.snapshot();
    expect(state.orderedMessageIds.indexOf("call-1")).toBeLessThan(
      state.orderedMessageIds.indexOf("pin-1"),
    );
    expect(state.orderedMessageIds.indexOf("pin-1")).toBeLessThan(
      state.orderedMessageIds.indexOf("run-1:turn-2:assistant"),
    );
    expect(state.messagesById["run-1:turn-1:assistant"]).toBeUndefined();
    expect(state.messagesById["run-1:turn-2:assistant"].parts).toEqual([
      { type: "text", text: "收到测试排队2" },
    ]);
  });

  it("generic 入站流式帧归并成一条 completed assistant 消息", () => {
    const { manager, store } = makeManager("t1");
    const env = (frame: unknown): RawFrameEnvelope => ({
      connectionId: "t1",
      channel: "generic",
      threadId: "t1",
      frame,
      receivedAt: 1_700_000_000_000,
    });

    manager.ingestFrame(env({ frame_type: "turn.start", timestamp_ms: 1, turn: 0, run_id: "r1" }));
    manager.ingestFrame(env({ frame_type: "content.delta", timestamp_ms: 2, delta: "Hello ", turn: 0, seq: 0, run_id: "r1" }));
    manager.ingestFrame(env({ frame_type: "content.delta", timestamp_ms: 3, delta: "world", turn: 0, seq: 1, run_id: "r1" }));
    manager.ingestFrame(env({ frame_type: "assistant.final", timestamp_ms: 4, content: "Hello world", turn: 0, run_id: "r1" }));
    manager.ingestFrame(env({
      frame_type: "turn.end",
      timestamp_ms: 5,
      turn: 0,
      run_id: "r1",
      history_index: 7,
      has_tool_calls: false,
    }));

    const state = store.snapshot();
    expect(state.orderedMessageIds).toHaveLength(1);
    const msg = state.messagesById[state.orderedMessageIds[0]];
    expect(msg.role).toBe("assistant");
    expect(msg.status).toBe("completed");
    expect(msg.parts).toEqual([{ type: "text", text: "Hello world" }]);
    expect(msg.forkHistoryIndex).toBe(7);
    expect(state.turnsById["r1:turn-0"].phase).toBe("completed");
    expect(state.activeStreamingTurnId).toBeNull();
  });

  it("run.interrupted 保留已到达正文并完成 turn", () => {
    const { manager, store } = makeManager("t1");
    manager.ingestFrame(env("t1", { frame_type: "turn.start", timestamp_ms: 1, turn: 3, run_id: "r1" }));
    manager.ingestFrame(env("t1", { frame_type: "content.delta", timestamp_ms: 2, delta: "保留这段", turn: 3, seq: 0, run_id: "r1" }));
    manager.ingestFrame(env("t1", {
      frame_type: "run.interrupted",
      timestamp_ms: 3,
      run_id: "r1",
      cancelled_at_turn: 3,
      cancel_reason: "user",
      cancelled_tool_call_id: null,
    }));

    const message = store.snapshot().messagesById["r1:turn-3:assistant"];
    expect(message).toMatchObject({ status: "completed", parts: [{ type: "text", text: "保留这段" }] });
  });

  it("llm_error 通过真实 ChatManager 主链丢弃不完整 assistant", () => {
    const { manager, store } = makeManager("t1");
    manager.ingestFrame(env("t1", { frame_type: "turn.start", timestamp_ms: 1, turn: 4, run_id: "r1" }));
    manager.ingestFrame(env("t1", { frame_type: "content.delta", timestamp_ms: 2, delta: "残缺", turn: 4, seq: 0, run_id: "r1" }));
    manager.ingestFrame(env("t1", {
      frame_type: "error",
      timestamp_ms: 3,
      turn: 4,
      error_code: "llm_error",
      message: "provider disconnected",
    }));

    const records = Object.values(store.snapshot().messagesById);
    expect(records.some((record) => record.role === "assistant")).toBe(false);
    expect(records.some((record) => record.role === "error" && record.error?.errorCode === "llm_error")).toBe(true);
    expect(store.snapshot().activeStreamingTurnId).toBeNull();
  });

  it("generic 工具帧聚合成 ChatToolRecord", () => {
    const { manager, store } = makeManager("t1");
    const env = (frame: unknown): RawFrameEnvelope => ({
      connectionId: "t1",
      channel: "generic",
      threadId: "t1",
      frame,
      receivedAt: 1_700_000_000_000,
    });
    manager.ingestFrame(env({ frame_type: "turn.start", timestamp_ms: 1, turn: 0, run_id: "r1" }));
    manager.ingestFrame(env({ frame_type: "tool.call.start", timestamp_ms: 2, tool_name: "read_file", call_id: "c1", turn: 0, arguments: { path: "/x" }, run_id: "r1" }));
    manager.ingestFrame(env({ frame_type: "tool.call.end", timestamp_ms: 3, call_id: "c1", turn: 0, ok: true, content: "file body", run_id: "r1" }));

    const state = store.snapshot();
    const tool = state.toolsById["c1"];
    expect(tool.toolName).toBe("read_file");
    expect(tool.status).toBe("completed");
    expect(tool.outputText).toBe("file body");
    expect(state.turnsById["r1:turn-0"].toolCallIds).toEqual(["c1"]);
  });

  it("cron.message.appended writes to the run timeline without touching the parent thread timeline", () => {
    const parentStore = new ChatTimelineStore("t1");
    const runKey = makeCronTimelineKey("t1", "run-1");
    const runStore = new ChatTimelineStore(runKey);
    const manager = new ChatManager({
      resolveHandle: () => ({
        connectionId: "t1",
        send: vi.fn(),
        close: vi.fn(),
      }),
      ensureThread: async (req) => req.provider.threadId,
      timelineFor: (threadId) => (threadId === runKey ? runStore : parentStore),
    });

    manager.ingestFrame({
      connectionId: "t1",
      channel: "generic",
      threadId: "t1",
      frame: {
        frame_type: "cron.message.appended",
        timestamp_ms: 1_700_000_000_000,
        thread_id: "t1",
        task_id: "task-1",
        run_id: "run-1",
        session_id: "session-run-1",
        task_name: "daily",
        message_id: "cron-msg-1",
        content: "done",
      },
      receivedAt: 1_700_000_000_000,
    });

    expect(parentStore.snapshot().orderedMessageIds).toHaveLength(0);
    expect(runStore.snapshot().orderedMessageIds).toEqual(["cron-msg-1"]);
    expect(runStore.snapshot().messagesById["cron-msg-1"].parts).toEqual([
      { type: "text", text: "done" },
    ]);
  });
});
