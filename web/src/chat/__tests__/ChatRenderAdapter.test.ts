/**
 * chat-receive-side-unify #2 · ChatRenderAdapter 渲染适配层测试
 *
 * 严格 TDD：每个能力先红后绿。两层验证：
 * 1) 核心层 `toViewModel(state)` —— 把 ChatTimelineState 纯投影成 ChatViewModel
 *    （user / assistant 含 reasoning+usage / tool 回查 toolsById 富字段 /
 *     system notice / error / pending tool / 顺序 / isStreaming / 空 state）。
 * 2) 适配层 `toGenericRenderItems(view)` —— 翻回 GenericChatItem，与现状
 *    stores/chat.ts ChatItem 逐字段等价（UI 零回归）；claude/codex 适配层
 *    产出正确类型 + 基本投影。
 *
 * 数据真源构造惯例对照 ChatTimelineStore.test.ts：直接走 store.applyEvent /
 * applyHistory 灌事件，再取 snapshot 作为 toViewModel 的输入，保证投影面对的是
 * 真实 store 产物而非手捏 state。
 */
import { describe, it, expect } from "vitest";
import { ChatTimelineStore } from "../ChatTimelineStore";
import {
  toViewModel,
  toGenericRenderItems,
  toClaudeRenderItems,
  toCodexRenderItems,
} from "../ChatRenderAdapter";
import type { ChatEvent } from "../types";

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

describe("toViewModel · 空 state", () => {
  it("空 state 投影成空 items + 不 streaming + 未加载历史", () => {
    const store = new ChatTimelineStore(T);
    const view = toViewModel(store.getSnapshot());
    expect(view.items).toEqual([]);
    expect(view.isStreaming).toBe(false);
    expect(view.historyLoaded).toBe(false);
  });
});

describe("toViewModel · user 消息", () => {
  it("user_message 投影成 kind=message / role=user / content 取 text part", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(
      ev({ kind: "user_message", turnId: `${T}-turn-1`, payload: { text: "你好" } }),
    );
    const view = toViewModel(store.getSnapshot());
    expect(view.items).toHaveLength(1);
    const item = view.items[0];
    expect(item.kind).toBe("message");
    if (item.kind === "message") {
      expect(item.role).toBe("user");
      expect(item.content).toBe("你好");
      expect(item.turn).toBe(1);
      expect(item.runId).toBe("");
    }
  });
});

describe("toViewModel · assistant 含 reasoning + usage", () => {
  it("assistant content / reasoning / usage / streaming 全投影", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(ev({ kind: "assistant_message_started", turnId: "r5" }));
    store.applyEvent(
      ev({ kind: "assistant_message_delta", turnId: "r5", payload: { reasoningDelta: "想一下" } }),
    );
    store.applyEvent(
      ev({ kind: "assistant_message_delta", turnId: "r5", payload: { delta: "答案" } }),
    );
    // streaming 中：先断言 streaming=true
    const streamingView = toViewModel(store.getSnapshot());
    const sItem = streamingView.items[0];
    expect(sItem.kind).toBe("message");
    if (sItem.kind === "message") {
      expect(sItem.streaming).toBe(true);
    }
    // 完成
    store.applyEvent(
      ev({
        kind: "assistant_message_completed",
        turnId: "r5",
        payload: { content: "答案", usage: { prompt: 10, completion: 5, total: 15 } },
      }),
    );
    const view = toViewModel(store.getSnapshot());
    const item = view.items[0];
    expect(item.kind).toBe("message");
    if (item.kind === "message") {
      expect(item.role).toBe("assistant");
      expect(item.content).toBe("答案");
      expect(item.reasoning).toBe("想一下");
      expect(item.usage).toEqual({ prompt: 10, completion: 5, total: 15 });
      expect(item.streaming).toBe(false);
      expect(item.runId).toBe("r5");
      expect(item.turn).toBe(0);
    }
  });
});

describe("toViewModel · tool 回查 toolsById 富字段", () => {
  it("tool 项从 toolsById 取 toolName/arguments/ok/errorMessage/result/resultData", () => {
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
    const view = toViewModel(store.getSnapshot());
    const toolItem = view.items.find((i) => i.kind === "tool");
    expect(toolItem).toBeDefined();
    if (toolItem && toolItem.kind === "tool") {
      expect(toolItem.toolName).toBe("read_file");
      expect(toolItem.callId).toBe("c1");
      expect(toolItem.arguments).toEqual({ path: "/x" });
      expect(toolItem.ok).toBe(false);
      expect(toolItem.errorMessage).toBe("failed");
      expect(toolItem.result).toBe("boom");
      expect(toolItem.resultData).toEqual({ code: 1 });
    }
  });

  it("tool running（未完成）ok=null", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(ev({ kind: "assistant_message_started" }));
    store.applyEvent(
      ev({
        kind: "tool_call_started",
        toolCallId: "c1",
        payload: { toolName: "shell", arguments: {} },
      }),
    );
    const view = toViewModel(store.getSnapshot());
    const toolItem = view.items.find((i) => i.kind === "tool");
    expect(toolItem && toolItem.kind === "tool" ? toolItem.ok : "x").toBeNull();
  });
});

describe("toViewModel · pending tool", () => {
  it("claude pending tool 占位投影成 tool 项 pending=true / ok=null", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(ev({ kind: "assistant_message_started" }));
    store.applyEvent(
      ev({ kind: "tool_call_started", toolCallId: "p1", payload: { pending: true } }),
    );
    store.applyEvent(
      ev({ kind: "tool_call_delta", toolCallId: "p1", payload: { partialInputDelta: '{"a":1}' } }),
    );
    const view = toViewModel(store.getSnapshot());
    const toolItem = view.items.find((i) => i.kind === "tool");
    expect(toolItem).toBeDefined();
    if (toolItem && toolItem.kind === "tool") {
      expect(toolItem.callId).toBe("p1");
      expect(toolItem.pending).toBe(true);
      expect(toolItem.ok).toBeNull();
      expect(toolItem.partialInput).toBe('{"a":1}');
    }
  });
});

describe("toViewModel · system notice 载荷", () => {
  it("status 事件投影成 kind=notice，搬运全字段", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(
      ev({
        kind: "status",
        turnId: "r9",
        payload: {
          noticeKey: "compact.start",
          source: "compactor",
          title: "压缩中",
          message: "正在压缩",
          status: "running",
          icon: "running",
          details: ["a", "b"],
        },
      }),
    );
    const view = toViewModel(store.getSnapshot());
    const item = view.items[0];
    expect(item.kind).toBe("notice");
    if (item.kind === "notice") {
      expect(item.noticeKey).toBe("compact.start");
      expect(item.source).toBe("compactor");
      expect(item.title).toBe("压缩中");
      expect(item.message).toBe("正在压缩");
      expect(item.status).toBe("running");
      expect(item.icon).toBe("running");
      expect(item.details).toEqual(["a", "b"]);
      expect(item.runId).toBe("r9");
    }
  });
});

describe("toViewModel · error 载荷", () => {
  it("error 事件投影成 kind=error，搬运 errorCode/message", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(
      ev({ kind: "error", payload: { errorCode: "E_TIMEOUT", message: "超时" } }),
    );
    const view = toViewModel(store.getSnapshot());
    const item = view.items[0];
    expect(item.kind).toBe("error");
    if (item.kind === "error") {
      expect(item.errorCode).toBe("E_TIMEOUT");
      expect(item.message).toBe("超时");
    }
  });
});

describe("toViewModel · 顺序 + isStreaming", () => {
  it("items 顺序 = orderedMessageIds 顺序", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(ev({ kind: "user_message", turnId: `${T}-turn-1`, payload: { text: "Q" } }));
    store.applyEvent(ev({ kind: "assistant_message_started", turnId: `${T}-turn-1` }));
    store.applyEvent(
      ev({ kind: "tool_call_started", turnId: `${T}-turn-1`, toolCallId: "c1", payload: { toolName: "x", arguments: {} } }),
    );
    const state = store.getSnapshot();
    const view = toViewModel(state);
    const ids = view.items.map((i) => i.id);
    expect(ids).toEqual(state.orderedMessageIds);
  });

  it("isStreaming 反映 activeStreamingTurnId", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(ev({ kind: "assistant_message_started", turnId: "r1" }));
    expect(toViewModel(store.getSnapshot()).isStreaming).toBe(true);
    store.applyEvent(
      ev({ kind: "assistant_message_completed", turnId: "r1", payload: { content: "done" } }),
    );
    expect(toViewModel(store.getSnapshot()).isStreaming).toBe(false);
  });
});

describe("toGenericRenderItems · 与现状 ChatItem 逐字段等价", () => {
  it("user 项翻成 GenericChatItem user kind", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(
      ev({ kind: "user_message", turnId: `${T}-turn-2`, payload: { text: "嗨" } }),
    );
    const items = toGenericRenderItems(toViewModel(store.getSnapshot()));
    expect(items).toHaveLength(1);
    const it = items[0];
    expect(it.kind).toBe("user");
    if (it.kind === "user") {
      expect(it.content).toBe("嗨");
      expect(it.threadId).toBe(T);
      expect(typeof it.timestampMs).toBe("number");
    }
  });

  it("assistant 项翻成 GenericChatItem assistant kind（turn/runId/reasoning/usage/streaming）", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(ev({ kind: "assistant_message_started", turnId: "r7" }));
    store.applyEvent(
      ev({ kind: "assistant_message_delta", turnId: "r7", payload: { reasoningDelta: "思考" } }),
    );
    store.applyEvent(
      ev({
        kind: "assistant_message_completed",
        turnId: "r7",
        payload: { content: "结论", usage: { prompt: 1, completion: 2, total: 3 } },
      }),
    );
    const items = toGenericRenderItems(toViewModel(store.getSnapshot()));
    const it = items[0];
    expect(it.kind).toBe("assistant");
    if (it.kind === "assistant") {
      expect(it.content).toBe("结论");
      expect(it.reasoning).toBe("思考");
      expect(it.usage).toEqual({ prompt: 1, completion: 2, total: 3 });
      expect(it.streaming).toBe(false);
      expect(it.runId).toBe("r7");
      expect(it.turn).toBe(0);
    }
  });

  it("tool 项翻成 GenericChatItem tool kind（ok/arguments/result/resultData/errorMessage）", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(ev({ kind: "assistant_message_started" }));
    store.applyEvent(
      ev({
        kind: "tool_call_started",
        toolCallId: "c2",
        payload: { toolName: "shell", arguments: { cmd: "ls" } },
      }),
    );
    store.applyEvent(
      ev({
        kind: "tool_call_completed",
        toolCallId: "c2",
        payload: { ok: true, content: "out", data: { lines: 3 } },
      }),
    );
    const items = toGenericRenderItems(toViewModel(store.getSnapshot()));
    const it = items.find((x) => x.kind === "tool");
    expect(it).toBeDefined();
    if (it && it.kind === "tool") {
      expect(it.toolName).toBe("shell");
      expect(it.callId).toBe("c2");
      expect(it.arguments).toEqual({ cmd: "ls" });
      expect(it.ok).toBe(true);
      expect(it.result).toBe("out");
      expect(it.resultData).toEqual({ lines: 3 });
    }
  });

  it("system notice 项翻成 GenericChatItem system kind（搬运 normalize 后字段）", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(
      ev({
        kind: "status",
        turnId: "r3",
        payload: {
          noticeKey: "k1",
          source: "src",
          title: "标题",
          message: "正文",
          status: "success",
          icon: "success",
          details: ["d1"],
        },
      }),
    );
    const items = toGenericRenderItems(toViewModel(store.getSnapshot()));
    const it = items[0];
    expect(it.kind).toBe("system");
    if (it.kind === "system") {
      expect(it.noticeKey).toBe("k1");
      expect(it.source).toBe("src");
      expect(it.title).toBe("标题");
      expect(it.message).toBe("正文");
      expect(it.status).toBe("success");
      expect(it.icon).toBe("success");
      expect(it.details).toEqual(["d1"]);
      expect(it.runId).toBe("r3");
    }
  });

  it("error 项翻成 GenericChatItem error kind", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(
      ev({ kind: "error", payload: { errorCode: "E_X", message: "炸了" } }),
    );
    const items = toGenericRenderItems(toViewModel(store.getSnapshot()));
    const it = items[0];
    expect(it.kind).toBe("error");
    if (it.kind === "error") {
      expect(it.errorCode).toBe("E_X");
      expect(it.message).toBe("炸了");
    }
  });

  it("整条时间线顺序保持（user → assistant → tool）", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(ev({ kind: "user_message", turnId: `${T}-turn-1`, payload: { text: "Q" } }));
    store.applyEvent(ev({ kind: "assistant_message_started", turnId: `${T}-turn-1` }));
    store.applyEvent(
      ev({ kind: "tool_call_started", turnId: `${T}-turn-1`, toolCallId: "c1", payload: { toolName: "x", arguments: {} } }),
    );
    const items = toGenericRenderItems(toViewModel(store.getSnapshot()));
    expect(items.map((i) => i.kind)).toEqual(["user", "assistant", "tool"]);
  });
});

describe("toClaudeRenderItems / toCodexRenderItems · 类型 + 基本投影", () => {
  it("toClaudeRenderItems 复用 generic 投影产出 GenericChatItem", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(
      ev({ kind: "user_message", turnId: `${T}-turn-1`, payload: { text: "hi" } }),
    );
    const items = toClaudeRenderItems(toViewModel(store.getSnapshot()));
    expect(items).toHaveLength(1);
    expect(items[0].kind).toBe("user");
  });

  it("toCodexRenderItems 复用 generic 投影产出 GenericChatItem", () => {
    const store = new ChatTimelineStore(T);
    store.applyEvent(
      ev({ kind: "user_message", turnId: `${T}-turn-1`, payload: { text: "hi" } }),
    );
    const items = toCodexRenderItems(toViewModel(store.getSnapshot()));
    expect(items).toHaveLength(1);
    expect(items[0].kind).toBe("user");
  });
});
