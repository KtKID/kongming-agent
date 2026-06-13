/**
 * chat-receive-side-unify #1 · ChatTimelineStore 状态机地基测试
 *
 * 严格 TDD：每个能力先红后绿。覆盖响应式订阅、批量 notify、getSnapshot 纯净性、
 * reasoning 分轨、usage、tool 富字段、claude pending tool 生命周期、system/error
 * 落 record、history 真实展开、history/realtime 幂等去重。
 *
 * 设计目标：ChatTimelineState 能无损重建等价于 stores/chat.ts 的 ChatItem[]，
 * 供 #2 ChatRenderAdapter 投影回 GenericChatItem 保证 UI 零回归。
 */
import { describe, it, expect, vi } from "vitest";
import { ChatTimelineStore } from "../ChatTimelineStore";
import type { ChatEvent, ChatHistoryBatch } from "../types";
import type { HistoryMessageDTO } from "@/protocol";

const T = "t1";

function ev(partial: Partial<ChatEvent> & Pick<ChatEvent, "kind">): ChatEvent {
  return {
    provider: "generic",
    threadId: T,
    turnId: partial.turnId ?? "r1",
    createdAt: partial.createdAt ?? 1,
    payload: partial.payload ?? {},
    ...partial,
  };
}

describe("ChatTimelineStore · subscribe / 响应式", () => {
  it("subscribe 注册的 listener 在 applyEvent 后被调用，unsubscribe 后不再调用", () => {
    const store = new ChatTimelineStore(T);
    const fn = vi.fn();
    const unsub = store.subscribe(fn);
    store.applyEvent(ev({ kind: "user_message", payload: { text: "hi" } }));
    expect(fn).toHaveBeenCalledTimes(1);
    unsub();
    store.applyEvent(ev({ kind: "user_message", messageId: "m2", payload: { text: "bye" } }));
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("getSnapshot 在 state 未变时返回同一引用（useSyncExternalStore 纯净性）", () => {
    const store = new ChatTimelineStore(T);
    const a = store.getSnapshot();
    const b = store.getSnapshot();
    expect(a).toBe(b);
    store.applyEvent(ev({ kind: "user_message", payload: { text: "hi" } }));
    const c = store.getSnapshot();
    expect(c).not.toBe(a);
    expect(store.getSnapshot()).toBe(c);
  });
});

describe("ChatTimelineStore · 批量 notify 合并", () => {
  it("applyEvent 单次调用只触发 listener 一次", () => {
    const store = new ChatTimelineStore(T);
    const fn = vi.fn();
    store.subscribe(fn);
    store.applyEvent(ev({ kind: "assistant_message_started" }));
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("applyHistory 灌 100 条历史只触发 listener 一次", () => {
    const store = new ChatTimelineStore(T);
    const fn = vi.fn();
    store.subscribe(fn);
    const events: ChatEvent[] = [];
    for (let i = 0; i < 100; i++) {
      events.push(ev({ kind: "user_message", messageId: `m${i}`, payload: { text: `q${i}` } }));
    }
    const batch: ChatHistoryBatch = {
      threadId: T,
      provider: "generic",
      events,
      hasMore: false,
    };
    store.applyHistory(batch);
    expect(fn).toHaveBeenCalledTimes(1);
    expect(store.getSnapshot().orderedMessageIds).toHaveLength(100);
  });
});

describe("ChatTimelineStore · reasoning 分轨", () => {
  it("reasoningDelta 累加到 reasoning 字段，不污染正文 text part", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(ev({ kind: "assistant_message_started" }));
    store.applyEvent(ev({ kind: "assistant_message_delta", payload: { reasoningDelta: "想了" } }));
    store.applyEvent(ev({ kind: "assistant_message_delta", payload: { reasoningDelta: "一下" } }));
    store.applyEvent(ev({ kind: "assistant_message_delta", payload: { delta: "答案" } }));
    const state = store.getSnapshot();
    const id = state.orderedMessageIds[0];
    const msg = state.messagesById[id];
    expect(msg.reasoning).toBe("想了一下");
    expect(msg.parts).toEqual([{ type: "text", text: "答案" }]);
  });

  it("纯 reasoningDelta（无正文）也能建消息且正文 text part 为空", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(ev({ kind: "assistant_message_delta", payload: { reasoningDelta: "纯思考" } }));
    const state = store.getSnapshot();
    const msg = state.messagesById[state.orderedMessageIds[0]];
    expect(msg.reasoning).toBe("纯思考");
    const textPart = msg.parts.find((p) => p.type === "text");
    expect(textPart && textPart.type === "text" ? textPart.text : null).toBe("");
  });
});

describe("ChatTimelineStore · usage", () => {
  it("assistant_message_completed 带 usage 写入 message.usage", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(ev({ kind: "assistant_message_started" }));
    store.applyEvent(
      ev({
        kind: "assistant_message_completed",
        payload: { content: "done", usage: { prompt: 10, completion: 5, total: 15 } },
      }),
    );
    const state = store.getSnapshot();
    const msg = state.messagesById[state.orderedMessageIds[0]];
    expect(msg.usage).toEqual({ prompt: 10, completion: 5, total: 15 });
  });
});

describe("ChatTimelineStore · tool 富字段", () => {
  it("tool_call_started 写 arguments，tool_call_completed 写 resultData/errorMessage", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(ev({ kind: "assistant_message_started" }));
    store.applyEvent(
      ev({
        kind: "tool_call_started",
        toolCallId: "c1",
        payload: { toolName: "read_file", arguments: { path: "/x" } },
      }),
    );
    store.applyEvent(
      ev({
        kind: "tool_call_completed",
        toolCallId: "c1",
        payload: { ok: false, content: "boom", data: { code: 1 }, errorMessage: "failed" },
      }),
    );
    const tool = store.getSnapshot().toolsById["c1"];
    expect(tool.arguments).toEqual({ path: "/x" });
    expect(tool.status).toBe("failed");
    expect(tool.errorMessage).toBe("failed");
    expect(tool.resultData).toEqual({ code: 1 });
    expect(tool.outputText).toBe("boom");
  });
});

describe("ChatTimelineStore · claude pending tool 全生命周期", () => {
  it("占位（pending=true）→ partialInput 累积 → resolve 进 toolsById", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(ev({ kind: "assistant_message_started" }));
    // 1) stream_status(phase=tool_calling) → 占位
    store.applyEvent(
      ev({ kind: "tool_call_started", toolCallId: "c1", payload: { pending: true } }),
    );
    let pending = store.getSnapshot().pendingTools["c1"];
    expect(pending).toBeDefined();
    expect(pending.partialInput).toBe("");
    // 2) stream_delta(input_json) → 累积 partialInput
    store.applyEvent(
      ev({ kind: "tool_call_delta", toolCallId: "c1", payload: { partialInputDelta: '{"pa' } }),
    );
    store.applyEvent(
      ev({ kind: "tool_call_delta", toolCallId: "c1", payload: { partialInputDelta: 'th":"/x"}' } }),
    );
    pending = store.getSnapshot().pendingTools["c1"];
    expect(pending.partialInput).toBe('{"path":"/x"}');
    // 3) tool_use → resolve（pending 清出，进 toolsById）
    store.applyEvent(
      ev({
        kind: "tool_call_started",
        toolCallId: "c1",
        payload: { pending: false, toolName: "read_file", arguments: { path: "/x" } },
      }),
    );
    const state = store.getSnapshot();
    expect(state.pendingTools["c1"]).toBeUndefined();
    const tool = state.toolsById["c1"];
    expect(tool.toolName).toBe("read_file");
    expect(tool.pending).toBe(false);
    expect(tool.arguments).toEqual({ path: "/x" });
    expect(state.turnsById["r1"].toolCallIds).toContain("c1");
  });
});

describe("ChatTimelineStore · system.notice / error 落 record", () => {
  it("status 事件落 system record，承载 noticeKey/title/message/status/icon", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(
      ev({
        kind: "status",
        payload: {
          noticeKey: "compact.start",
          source: "compactor",
          title: "压缩中",
          message: "正在压缩历史",
          status: "running",
          icon: "running",
          details: ["a", "b"],
        },
      }),
    );
    const state = store.getSnapshot();
    expect(state.orderedMessageIds).toHaveLength(1);
    const rec = state.messagesById[state.orderedMessageIds[0]];
    expect(rec.role).toBe("system");
    expect(rec.notice).toMatchObject({
      noticeKey: "compact.start",
      source: "compactor",
      title: "压缩中",
      message: "正在压缩历史",
      status: "running",
      icon: "running",
    });
    expect(rec.notice?.details).toEqual(["a", "b"]);
  });

  it("同 (runId, noticeKey) 的 status 事件更新而不新增 record", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(
      ev({ kind: "status", payload: { noticeKey: "compact.start", status: "running" } }),
    );
    store.applyEvent(
      ev({ kind: "status", payload: { noticeKey: "compact.start", status: "success" } }),
    );
    const state = store.getSnapshot();
    expect(state.orderedMessageIds).toHaveLength(1);
    expect(state.messagesById[state.orderedMessageIds[0]].notice?.status).toBe("success");
  });

  it("error 事件落 error record，承载 errorCode/message", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(
      ev({ kind: "error", payload: { errorCode: "E_TIMEOUT", message: "超时了" } }),
    );
    const state = store.getSnapshot();
    expect(state.orderedMessageIds).toHaveLength(1);
    const rec = state.messagesById[state.orderedMessageIds[0]];
    expect(rec.role).toBe("error");
    expect(rec.error).toEqual({ errorCode: "E_TIMEOUT", message: "超时了" });
  });
});

describe("ChatTimelineStore · history 真实展开", () => {
  function histBatch(messages: HistoryMessageDTO[]): ChatHistoryBatch {
    return {
      threadId: T,
      provider: "generic",
      events: [
        ev({
          kind: "history_batch_loaded",
          turnId: `${T}-history`,
          payload: { messages },
        }),
      ],
      hasMore: false,
    };
  }

  it("history 展开 user / assistant / tool 三类 record", () => {
    const store = new ChatTimelineStore(T);
    store.applyHistory(
      histBatch([
        { role: "user", content: "问题", turn: 1, timestamp_ms: 10 },
        { role: "assistant", content: "回答", turn: 1, timestamp_ms: 11 },
        {
          role: "tool",
          content: "文件内容",
          turn: 1,
          timestamp_ms: 12,
          tool_name: "read_file",
          tool_call_id: "tc1",
          ok: true,
          data: { size: 100 },
        },
      ]),
    );
    const state = store.getSnapshot();
    expect(state.historyLoaded).toBe(true);
    expect(state.orderedMessageIds).toHaveLength(3);

    const [uid, aid, tid] = state.orderedMessageIds;
    const u = state.messagesById[uid];
    expect(u.role).toBe("user");
    expect(u.parts).toEqual([{ type: "text", text: "问题" }]);

    const a = state.messagesById[aid];
    expect(a.role).toBe("assistant");
    expect(a.parts).toEqual([{ type: "text", text: "回答" }]);
    expect(a.status).toBe("completed");

    const tRec = state.messagesById[tid];
    expect(tRec.role).toBe("tool");
    const tool = state.toolsById["tc1"];
    expect(tool.toolName).toBe("read_file");
    expect(tool.outputText).toBe("文件内容");
    expect(tool.resultData).toEqual({ size: 100 });
    expect(tool.status).toBe("completed");
  });

  it("history tool ok=false 时 status=failed，error_message 落字段", () => {
    const store = new ChatTimelineStore(T);
    store.applyHistory(
      histBatch([
        {
          role: "tool",
          content: "",
          turn: 1,
          timestamp_ms: 12,
          tool_name: "shell",
          tool_call_id: "tc2",
          ok: false,
          error_message: "exit 1",
        },
      ]),
    );
    const tool = store.getSnapshot().toolsById["tc2"];
    expect(tool.status).toBe("failed");
    expect(tool.errorMessage).toBe("exit 1");
  });
});

describe("ChatTimelineStore · history / realtime 幂等去重", () => {
  // 同源约束：history 展开的 turnId = `${threadId}-turn-${turn}`，与 generic
  // provider 无 run_id 时的实时 turnId 同源；messageKey = `${turnId}:${role}`。
  it("history user 与同 turn 实时 user 合并后只一条（messageKey 同源）", () => {
    const store = new ChatTimelineStore(T);
    // 先实时 user：turnId 用与 history turn=1 同源的键
    store.applyEvent(ev({ kind: "user_message", turnId: `${T}-turn-1`, payload: { text: "问题" } }));
    expect(store.getSnapshot().orderedMessageIds).toHaveLength(1);
    // 再 history 含同一 turn 的 user（同源 id → 合并）
    store.applyHistory({
      threadId: T,
      provider: "generic",
      events: [
        ev({
          kind: "history_batch_loaded",
          turnId: `${T}-history`,
          payload: {
            messages: [{ role: "user", content: "问题", turn: 1, timestamp_ms: 10 }],
          },
        }),
      ],
      hasMore: false,
    });
    const state = store.getSnapshot();
    expect(state.orderedMessageIds).toHaveLength(1);
    expect(state.messagesById[state.orderedMessageIds[0]].role).toBe("user");
  });

  it("已有 streaming assistant 时，history 同 turnId 的 assistant 被跳过保留实时版本", () => {
    const store = new ChatTimelineStore(T);
    // 实时：assistant 在 turnId=`${T}-turn-3` streaming（已收到 delta）
    const turnId = `${T}-turn-3`;
    store.applyEvent(ev({ kind: "assistant_message_started", turnId }));
    store.applyEvent(ev({ kind: "assistant_message_delta", turnId, payload: { delta: "实时版本" } }));
    expect(store.getSnapshot().orderedMessageIds).toHaveLength(1);
    // history 灌入同 turn=3 的 assistant：应跳过，不覆盖实时累积的内容
    store.applyHistory({
      threadId: T,
      provider: "generic",
      events: [
        ev({
          kind: "history_batch_loaded",
          turnId: `${T}-history`,
          payload: {
            messages: [{ role: "assistant", content: "历史版本", turn: 3, timestamp_ms: 5 }],
          },
        }),
      ],
      hasMore: false,
    });
    const state = store.getSnapshot();
    expect(state.orderedMessageIds).toHaveLength(1);
    const msg = state.messagesById[state.orderedMessageIds[0]];
    const textPart = msg.parts.find((p) => p.type === "text");
    expect(textPart && textPart.type === "text" ? textPart.text : "").toBe("实时版本");
  });

  it("history 重复灌入两次同一批不产生重复 record（幂等）", () => {
    const store = new ChatTimelineStore(T);
    const batch: ChatHistoryBatch = {
      threadId: T,
      provider: "generic",
      events: [
        ev({
          kind: "history_batch_loaded",
          turnId: `${T}-history`,
          payload: {
            messages: [
              { role: "user", content: "问题", turn: 1, timestamp_ms: 10 },
              { role: "assistant", content: "回答", turn: 1, timestamp_ms: 11 },
            ],
          },
        }),
      ],
      hasMore: false,
    };
    store.applyHistory(batch);
    store.applyHistory(batch);
    expect(store.getSnapshot().orderedMessageIds).toHaveLength(2);
  });

  it("history 展开后实时同 turn 的 assistant delta 合并进同一条（不重复）", () => {
    const store = new ChatTimelineStore(T);
    // history 含 turn=2 的 assistant
    store.applyHistory({
      threadId: T,
      provider: "generic",
      events: [
        ev({
          kind: "history_batch_loaded",
          turnId: `${T}-history`,
          payload: {
            messages: [{ role: "assistant", content: "历史回答", turn: 2, timestamp_ms: 20 }],
          },
        }),
      ],
      hasMore: false,
    });
    const afterHistory = store.getSnapshot().orderedMessageIds.length;
    expect(afterHistory).toBe(1);
    // 实时 delta 落在同 turnId（history assistant 用 `${T}-turn-2` 同源键）
    store.applyEvent(
      ev({ kind: "assistant_message_delta", turnId: `${T}-turn-2`, payload: { delta: "追加" } }),
    );
    const state = store.getSnapshot();
    expect(state.orderedMessageIds).toHaveLength(1);
    const msg = state.messagesById[state.orderedMessageIds[0]];
    const textPart = msg.parts.find((p) => p.type === "text");
    expect(textPart && textPart.type === "text" ? textPart.text : "").toContain("追加");
  });
});
