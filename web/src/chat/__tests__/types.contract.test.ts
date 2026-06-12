/**
 * #1 共享输入契约的编译期 + 运行期形状锁定。
 *
 * 这里只验证 `chat/types.ts` 的契约形状（联合分发、真源复用），
 * provider 行为 / 首发创建时机 / session status 等运行时测试归 #6。
 */
import { describe, it, expect, expectTypeOf } from "vitest";
import type {
  SendRequest,
  CommonSendInput,
  ProviderSendOptions,
  ChatEvent,
  ChatHistoryBatch,
  ChatTimelineState,
  UserInputAttachment,
  ReasoningEffort,
} from "../types";

describe("chat/types · 发送契约", () => {
  it("CommonSendInput 公用字段只认一份，可只带 text", () => {
    const minimal: CommonSendInput = { text: "hello" };
    expect(minimal.text).toBe("hello");
  });

  it("SendRequest 支持 generic / claude / codex 三种 provider 变体", () => {
    const common: CommonSendInput = {
      text: "hi",
      reasoningEffort: "high",
      cwd: "/tmp",
      metadata: { source: "test" },
    };
    const generic: SendRequest = {
      common,
      provider: { provider: "generic", threadId: "t1" },
    };
    const claude: SendRequest = {
      common,
      provider: { provider: "claude", threadId: "t1", resume: true, sessionId: "s1" },
    };
    const codex: SendRequest = {
      common,
      provider: {
        provider: "codex",
        threadId: "t1",
        permissionMode: "acceptEdits",
      },
    };
    expect([generic, claude, codex].map((r) => r.provider.provider)).toEqual([
      "generic",
      "claude",
      "codex",
    ]);
  });

  it("ProviderSendOptions 按 provider 字段判别（discriminated union）", () => {
    const opt: ProviderSendOptions = {
      provider: "codex",
      threadId: "t1",
      permissionMode: "bypassPermissions",
    };
    if (opt.provider === "codex") {
      // narrow 后才能访问 codex 独有字段
      expect(opt.permissionMode).toBe("bypassPermissions");
    }
  });
});

describe("chat/types · 真源复用（不出现第二份同名定义）", () => {
  it("UserInputAttachment 来自 @/protocol（asset_id / mime_type 等真源字段）", () => {
    expectTypeOf<UserInputAttachment>().toHaveProperty("asset_id");
    expectTypeOf<UserInputAttachment>().toHaveProperty("mime_type");
    expectTypeOf<UserInputAttachment>().toHaveProperty("preview_url");
  });

  it("ReasoningEffort 复用 Composer 的 low/medium/high union", () => {
    const efforts: ReasoningEffort[] = ["low", "medium", "high"];
    expect(efforts).toHaveLength(3);
  });
});

describe("chat/types · 事件与时间线", () => {
  it("ChatEvent 用毫秒时间戳（number）", () => {
    const ev: ChatEvent = {
      kind: "assistant_message_delta",
      provider: "claude",
      threadId: "t1",
      turnId: "turn1",
      createdAt: 1_700_000_000_000,
      payload: { text: "δ" },
    };
    expectTypeOf(ev.createdAt).toBeNumber();
    expect(ev.kind).toBe("assistant_message_delta");
  });

  it("ChatHistoryBatch 与实时 event 共用 ChatEvent", () => {
    const batch: ChatHistoryBatch = {
      threadId: "t1",
      provider: "generic",
      events: [],
      hasMore: false,
    };
    expect(batch.events).toEqual([]);
  });

  it("ChatTimelineState 是字典 + 顺序数组结构", () => {
    const state: ChatTimelineState = {
      threadId: "t1",
      historyLoaded: false,
      orderedMessageIds: [],
      messagesById: {},
      toolsById: {},
      pendingTools: {},
      turnsById: {},
    };
    expect(state.historyLoaded).toBe(false);
  });
});
