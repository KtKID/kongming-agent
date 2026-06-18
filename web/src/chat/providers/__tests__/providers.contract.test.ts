/**
 * #6 provider 契约测试：锁定三频道 provider 的 send 帧 / 入站翻译 / interrupt /
 * checkSessionStatus 行为。generic 端到端闭环另见 `chat/__tests__/runtime.smoke.test.ts`。
 */
import { describe, it, expect, vi } from "vitest";
import { getChatProvider } from "../index";
import type { NetworkHandle, RawFrameEnvelope, SendRequest } from "@/chat/types";

function fakeHandle(): { handle: NetworkHandle; sent: any[] } {
  const sent: any[] = [];
  return {
    handle: { connectionId: "t1", send: (f) => sent.push(f), close: vi.fn() },
    sent,
  };
}

describe("GenericChatProvider", () => {
  const p = getChatProvider("generic");

  it("send → user.input 帧（带 reasoning_effort）", async () => {
    const { handle, sent } = fakeHandle();
    const req: SendRequest = {
      common: { text: "hi", reasoningEffort: "high" },
      provider: { provider: "generic", threadId: "t1" },
    };
    await p.send(handle, req);
    expect(sent[0]).toMatchObject({
      frame_type: "user.input",
      text: "hi",
      reasoning_effort: "high",
    });
  });

  it("mapInboundFrame：content.delta → assistant_message_delta", () => {
    const env: RawFrameEnvelope = {
      connectionId: "t1",
      channel: "generic",
      threadId: "t1",
      frame: { frame_type: "content.delta", timestamp_ms: 5, delta: "x", turn: 0, seq: 0, run_id: "r1" },
      receivedAt: 5,
    };
    const events = p.mapInboundFrame(env);
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ kind: "assistant_message_delta", provider: "generic" });
    expect(events[0].payload.delta).toBe("x");
  });

  it("interrupt → interrupt 帧", async () => {
    const { handle, sent } = fakeHandle();
    await p.interrupt(handle, { threadId: "t1", provider: "generic" });
    expect(sent[0]).toMatchObject({ frame_type: "interrupt" });
  });

  it("submitChoice → choice.submit 帧", async () => {
    const { handle, sent } = fakeHandle();
    await p.submitChoice?.(handle, {
      provider: "generic",
      threadId: "t1",
      frame: {
        frame_type: "choice.submit",
        request_id: "call-1",
        answers: [
          {
            question_id: "scope",
            option_id: "minimal",
            option_label: "最小实现",
            custom_text: null,
          },
        ],
      },
    });
    expect(sent[0]).toMatchObject({
      frame_type: "choice.submit",
      request_id: "call-1",
    });
  });

  it("checkSessionStatus 固定 active:false（generic 无 session）", async () => {
    const s = await p.checkSessionStatus({ threadId: "t1", provider: "generic" });
    expect(s.active).toBe(false);
  });
});

describe("ClaudeChatProvider", () => {
  const p = getChatProvider("claude");

  it("send → claude-command 帧（options 空对象等价现状）", async () => {
    const { handle, sent } = fakeHandle();
    await p.send(handle, {
      common: { text: "你好" },
      provider: { provider: "claude", threadId: "t1" },
    });
    expect(sent[0].frame_type).toBe("claude-command");
    expect(sent[0].command).toBe("你好");
    // 全 undefined option 序列化后等价 {}（不污染现状 wire 形态）
    expect(JSON.parse(JSON.stringify(sent[0].options))).toEqual({});
  });

  it("mapInboundFrame：NormalizedMessage text → assistant_message_delta", () => {
    const env: RawFrameEnvelope = {
      connectionId: "t1",
      channel: "claude",
      threadId: "t1",
      frame: { frame_type: "text", role: "assistant", content: "hello" },
      receivedAt: 9,
    };
    const events = p.mapInboundFrame(env);
    expect(events[0]).toMatchObject({ kind: "assistant_message_delta", provider: "claude" });
  });

  it("mapInboundFrame：session-status → status 事件", () => {
    const env: RawFrameEnvelope = {
      connectionId: "t1",
      channel: "claude",
      threadId: "t1",
      frame: { frame_type: "session-status", sessionId: "s1", isProcessing: true },
      receivedAt: 9,
    };
    const events = p.mapInboundFrame(env);
    expect(events[0]).toMatchObject({ kind: "status" });
    expect(events[0].payload.isProcessing).toBe(true);
  });

  it("#8 send → 透传 common.reasoningEffort 到 wire options.reasoningEffort", async () => {
    const { handle, sent } = fakeHandle();
    await p.send(handle, {
      common: { text: "x", reasoningEffort: "medium" },
      provider: { provider: "claude", threadId: "t1" },
    });
    expect(sent[0].options.reasoningEffort).toBe("medium");
  });

  it("interrupt → abort-session（带 sessionId）；无 sessionId 不发", async () => {
    const a = fakeHandle();
    await p.interrupt(a.handle, { threadId: "t1", provider: "claude", sessionId: "sess-9" });
    expect(a.sent[0]).toMatchObject({ frame_type: "abort-session", sessionId: "sess-9" });

    const b = fakeHandle();
    await p.interrupt(b.handle, { threadId: "t1", provider: "claude" });
    expect(b.sent).toHaveLength(0);
  });
});

describe("CodexChatProvider", () => {
  const p = getChatProvider("codex");

  it("send → codex-command 帧（透传 permissionMode）", async () => {
    const { handle, sent } = fakeHandle();
    await p.send(handle, {
      common: { text: "run" },
      provider: { provider: "codex", threadId: "t1", permissionMode: "bypassPermissions" },
    });
    expect(sent[0].frame_type).toBe("codex-command");
    expect(sent[0].command).toBe("run");
    expect(sent[0].options.permissionMode).toBe("bypassPermissions");
  });

  it("#8 send → 透传 common.reasoningEffort 到 wire options（前端契约贯通；后端待消费）", async () => {
    const { handle, sent } = fakeHandle();
    await p.send(handle, {
      common: { text: "x", reasoningEffort: "high" },
      provider: { provider: "codex", threadId: "t1" },
    });
    expect(sent[0].options.reasoningEffort).toBe("high");
  });

  it("#8 reasoningEffort=null → wire 帧 options 不带（向后兼容）", async () => {
    const { handle, sent } = fakeHandle();
    await p.send(handle, {
      common: { text: "x", reasoningEffort: null },
      provider: { provider: "codex", threadId: "t1" },
    });
    expect(sent[0].options.reasoningEffort).toBeUndefined();
  });

  it("interrupt → abort-session", async () => {
    const { handle, sent } = fakeHandle();
    await p.interrupt(handle, { threadId: "t1", provider: "codex", sessionId: "c-1" });
    expect(sent[0]).toMatchObject({ frame_type: "abort-session", sessionId: "c-1" });
  });
});
