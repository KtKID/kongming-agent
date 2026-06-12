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
import type {
  NetworkHandle,
  RawFrameEnvelope,
  SendRequest,
  UserInputAttachment,
} from "../types";
import { useThreadStatusStore } from "@/stores/threadStatus";

beforeEach(() => {
  // chat-running-state-unify #6：sendMessage 会预设 phase=responding，
  // 测试间需清 store 防污染。
  useThreadStatusStore.setState({ statuses: {} });
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

describe("chat 运行时骨架 · 闭环 smoke", () => {
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

  it("#6 sendMessage 瞬间预设 phase=responding（前置入态，三频道一致）", async () => {
    const { manager } = makeManager("t1");
    expect(useThreadStatusStore.getState().statuses["t1"]).toBeUndefined();
    await manager.sendMessage({
      common: { text: "hi" },
      provider: { provider: "generic", threadId: "t1" },
    });
    // sendMessage 同步阶段已写入 store——按钮立刻显示停止，无需等 turn.start
    expect(useThreadStatusStore.getState().statuses["t1"]?.phase).toBe("responding");
  });

  it("#5 sendMessage 往时间线灌 user_message，用户气泡立即上屏", async () => {
    const { manager, store } = makeManager("t1");
    await manager.sendMessage({
      common: { text: "我的问题" },
      provider: { provider: "generic", threadId: "t1" },
    });
    const state = store.snapshot();
    const userRecords = Object.values(state.messagesById).filter(
      (m) => m.role === "user",
    );
    expect(userRecords).toHaveLength(1);
    expect(userRecords[0].parts).toContainEqual({ type: "text", text: "我的问题" });
    // 投影回 GenericChatItem：一条 user 项，内容正确
    const items = toGenericRenderItems(toViewModel(state));
    const userItem = items.find((i) => i.kind === "user");
    expect(userItem).toBeDefined();
    expect(userItem && "content" in userItem ? userItem.content : "").toBe(
      "我的问题",
    );
  });

  it("#5 user_message 携带 attachments → 投影回 GenericChatItem.attachments（缩略图零回归）", async () => {
    const { manager, store } = makeManager("t1");
    const attachment: UserInputAttachment = {
      kind: "image",
      asset_id: "asset-1",
      mime_type: "image/png",
      preview_url: "/api/uploads/asset-1",
    } as UserInputAttachment;
    await manager.sendMessage({
      common: { text: "看图", attachments: [attachment] },
      provider: { provider: "generic", threadId: "t1" },
    });
    const items = toGenericRenderItems(toViewModel(store.snapshot()));
    const userItem = items.find((i) => i.kind === "user");
    expect(userItem).toBeDefined();
    if (userItem && userItem.kind === "user") {
      expect(userItem.attachments).toBeDefined();
      expect(userItem.attachments).toHaveLength(1);
      expect(userItem.attachments?.[0].asset_id).toBe("asset-1");
    }
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
    manager.ingestFrame(env({ frame_type: "turn.end", timestamp_ms: 5, turn: 0, run_id: "r1" }));

    const state = store.snapshot();
    expect(state.orderedMessageIds).toHaveLength(1);
    const msg = state.messagesById[state.orderedMessageIds[0]];
    expect(msg.role).toBe("assistant");
    expect(msg.status).toBe("completed");
    expect(msg.parts).toEqual([{ type: "text", text: "Hello world" }]);
    expect(state.turnsById["r1"].phase).toBe("completed");
    expect(state.activeStreamingTurnId).toBeNull();
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
    expect(state.turnsById["r1"].toolCallIds).toEqual(["c1"]);
  });
});
