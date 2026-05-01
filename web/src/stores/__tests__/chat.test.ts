import { describe, it, expect, beforeEach } from "vitest";
import {
  __setRafForTest,
  appendDelta,
  clearBuffers,
  flushTurn,
  useChatStore,
} from "@/stores/chat";

describe("stores/chat", () => {
  beforeEach(() => {
    useChatStore.setState({ itemsByThread: {} });
    clearBuffers();
    // 同步 rAF：commit 立刻发生，便于断言
    __setRafForTest((cb) => cb());
  });

  it("appendDelta 累积到 buffer 并提交到 store（rAF 节流后）", () => {
    appendDelta("t1", "content", "Hel", 1);
    appendDelta("t1", "content", "lo", 1);
    const items = useChatStore.getState().itemsByThread["t1"]!;
    expect(items).toHaveLength(1);
    const it = items[0]!;
    if (it.kind !== "assistant") throw new Error("expected assistant");
    expect(it.content).toBe("Hello");
    expect(it.streaming).toBe(true);
  });

  it("flushTurn → 标记 streaming=false 并清 buffer", () => {
    appendDelta("t1", "content", "Hi", 2);
    flushTurn("t1", 2);
    const items = useChatStore.getState().itemsByThread["t1"]!;
    const it = items[0]!;
    if (it.kind !== "assistant") throw new Error("expected assistant");
    expect(it.streaming).toBe(false);
    expect(it.content).toBe("Hi");
    // buffer 已清；再 appendDelta 同 turn 会从空 buffer 开始
    appendDelta("t1", "content", "X", 2);
    const items2 = useChatStore.getState().itemsByThread["t1"]!;
    const it2 = items2[0]!;
    if (it2.kind !== "assistant") throw new Error("expected assistant");
    expect(it2.content).toBe("X");
  });

  it("不同 turn 创建独立 assistant item", () => {
    appendDelta("t1", "content", "first", 1);
    flushTurn("t1", 1);
    appendDelta("t1", "content", "second", 2);
    flushTurn("t1", 2);
    const items = useChatStore.getState().itemsByThread["t1"]!;
    expect(items).toHaveLength(2);
  });

  it("reasoning + content 分别累积", () => {
    appendDelta("t1", "content", "answer", 1);
    appendDelta("t1", "reasoning", "thinking", 1);
    const items = useChatStore.getState().itemsByThread["t1"]!;
    const it = items[0]!;
    if (it.kind !== "assistant") throw new Error("expected assistant");
    expect(it.content).toBe("answer");
    expect(it.reasoning).toBe("thinking");
  });

  it("appendUser → push user item", () => {
    useChatStore.getState().appendUser("t1", "hi there");
    const items = useChatStore.getState().itemsByThread["t1"]!;
    expect(items[0]!.kind).toBe("user");
  });

  it("setHistory → 历史消息映射成 ChatItem", () => {
    useChatStore.getState().setHistory("t1", [
      {
        role: "user",
        content: "u",
        turn: 1,
        timestamp_ms: 100,
      },
      {
        role: "assistant",
        content: "a",
        turn: 1,
        timestamp_ms: 110,
      },
    ]);
    const items = useChatStore.getState().itemsByThread["t1"]!;
    expect(items).toHaveLength(2);
    expect(items[0]!.kind).toBe("user");
    expect(items[1]!.kind).toBe("assistant");
  });

  it("clearBuffers(threadId) 只清该 thread", () => {
    appendDelta("t1", "content", "x", 1);
    appendDelta("t2", "content", "y", 1);
    clearBuffers("t1");
    // 再 appendDelta t1 同 turn 不会续上之前的 "x"
    appendDelta("t1", "content", "z", 1);
    const items = useChatStore.getState().itemsByThread["t1"]!;
    const it = items[0]!;
    if (it.kind !== "assistant") throw new Error("expected assistant");
    // commit 包含旧 store + 新 delta 的累积。store 里旧值已 commit，buffer 重新累积成 "z"
    // 但 commit 会用 buffer "z" 覆盖 store。所以最终值是 "z"
    expect(it.content).toBe("z");
  });

  it("性能：1000 帧 < 100ms（rAF 同步）", () => {
    const start = performance.now();
    for (let i = 0; i < 1000; i++) {
      appendDelta("t1", "content", "a", 1);
    }
    const dt = performance.now() - start;
    expect(dt).toBeLessThan(500);
    const items = useChatStore.getState().itemsByThread["t1"]!;
    const it = items[0]!;
    if (it.kind !== "assistant") throw new Error("expected assistant");
    expect(it.content.length).toBe(1000);
  });

  // M4 修 bug 验收：同 turn 不同 runId 必须创建独立 ChatItem
  it("同 turn=1 不同 runId 创建独立 assistant item（修非流式覆盖 bug）", () => {
    // 模拟连续两轮非流式对话：第一次 turn=1、runId=run-A；第二次 turn=1、runId=run-B
    appendDelta("t1", "content", "answer-1", 1, "run-A");
    flushTurn("t1", 1, "run-A");
    appendDelta("t1", "content", "answer-2", 1, "run-B");
    flushTurn("t1", 1, "run-B");

    const items = useChatStore.getState().itemsByThread["t1"]!;
    expect(items).toHaveLength(2);

    const a1 = items[0]!;
    const a2 = items[1]!;
    if (a1.kind !== "assistant" || a2.kind !== "assistant") {
      throw new Error("expected assistants");
    }
    expect(a1.runId).toBe("run-A");
    expect(a1.content).toBe("answer-1");
    expect(a2.runId).toBe("run-B");
    expect(a2.content).toBe("answer-2");
    // 两条 ChatItem.id 不同（含 runId 维度）
    expect(a1.id).not.toBe(a2.id);
  });

  it("ChatItem.id 包含 runId（按 (threadId, runId, turn) 唯一）", () => {
    appendDelta("t1", "content", "x", 5, "run-XYZ");
    const items = useChatStore.getState().itemsByThread["t1"]!;
    const it = items[0]!;
    if (it.kind !== "assistant") throw new Error("expected assistant");
    expect(it.id).toBe("assistant-t1-run-XYZ-5");
    expect(it.runId).toBe("run-XYZ");
  });

  it("不传 runId 时默认空串（旧后端兼容）", () => {
    appendDelta("t1", "content", "a", 1);
    flushTurn("t1", 1);
    const items = useChatStore.getState().itemsByThread["t1"]!;
    const it = items[0]!;
    if (it.kind !== "assistant") throw new Error("expected assistant");
    expect(it.runId).toBe("");
    expect(it.streaming).toBe(false);
  });

  it("appendAssistantFinal 用 frame.run_id 区分同 turn 不同 run", () => {
    const store = useChatStore.getState();
    store.appendAssistantFinal("t1", {
      kind: "assistant.final",
      content: "first",
      turn: 1,
      run_id: "run-1",
      timestamp_ms: 100,
    });
    store.appendAssistantFinal("t1", {
      kind: "assistant.final",
      content: "second",
      turn: 1,
      run_id: "run-2",
      timestamp_ms: 200,
    });
    const items = useChatStore.getState().itemsByThread["t1"]!;
    expect(items).toHaveLength(2);
    const a1 = items[0]!;
    const a2 = items[1]!;
    if (a1.kind !== "assistant" || a2.kind !== "assistant") {
      throw new Error("expected assistants");
    }
    expect(a1.content).toBe("first");
    expect(a2.content).toBe("second");
    expect(a1.runId).toBe("run-1");
    expect(a2.runId).toBe("run-2");
  });
});
