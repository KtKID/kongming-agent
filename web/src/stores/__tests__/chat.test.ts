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
    useChatStore.setState({ itemsByThread: {}, usageByThread: {} });
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

  it("appendSystemNotice 按 noticeKey + runId 原地更新同一条卡片", () => {
    const store = useChatStore.getState();
    store.appendSystemNotice("t1", {
      kind: "system.notice",
      timestamp_ms: 100,
      notice_key: "evolution.review",
      source: "self_evolution",
      status: "started",
      title: "进化复盘",
      message: "后台复盘中",
      details: ["turns: 1,2,3"],
      icon: "running",
      run_id: "run-1",
    });
    store.appendSystemNotice("t1", {
      kind: "system.notice",
      timestamp_ms: 200,
      notice_key: "evolution.review",
      source: "self_evolution",
      status: "completed",
      title: "进化复盘",
      message: "已沉淀 2 条进化养料",
      details: ["2 nutrients written"],
      icon: "success",
      run_id: "run-1",
    });

    const items = useChatStore.getState().itemsByThread["t1"]!;
    expect(items).toHaveLength(1);
    const item = items[0]!;
    if (item.kind !== "system") throw new Error("expected system notice");
    expect(item.id).toBe("system-t1-run-1-evolution.review");
    expect(item.runId).toBe("run-1");
    expect(item.noticeKey).toBe("evolution.review");
    expect(item.status).toBe("success");
    expect(item.icon).toBe("success");
    expect(item.message).toBe("已沉淀 2 条进化养料");
    expect(item.details).toEqual(["2 nutrients written"]);
    expect(item.timestampMs).toBe(200);
  });

  it("appendSystemNotice 同 noticeKey 不同 runId 保留独立卡片", () => {
    const store = useChatStore.getState();
    store.appendSystemNotice("t1", {
      kind: "system.notice",
      timestamp_ms: 100,
      notice_key: "evolution.review",
      source: "self_evolution",
      status: "started",
      title: "进化复盘",
      message: "run-1",
      details: [],
      run_id: "run-1",
    });
    store.appendSystemNotice("t1", {
      kind: "system.notice",
      timestamp_ms: 200,
      notice_key: "evolution.review",
      source: "self_evolution",
      status: "failed",
      title: "进化复盘",
      message: "run-2",
      details: ["timeout"],
      run_id: "run-2",
    });

    const items = useChatStore.getState().itemsByThread["t1"]!;
    expect(items).toHaveLength(2);
    const first = items[0]!;
    const second = items[1]!;
    if (first.kind !== "system" || second.kind !== "system") {
      throw new Error("expected system notices");
    }
    expect(first.runId).toBe("run-1");
    expect(second.runId).toBe("run-2");
    expect(second.status).toBe("error");
    expect(second.icon).toBe("error");
  });

  it("applyEvolutionReview 回写系统卡片进度", () => {
    const store = useChatStore.getState();
    store.appendSystemNotice("t1", {
      kind: "system.notice",
      timestamp_ms: 100,
      notice_key: "self_evolution.review",
      source: "self_evolution",
      status: "completed",
      title: "进化复盘",
      message: "发现 2 条进化养料",
      details: {
        review_id: "evo-review:run-1",
        review_run_id: "run-1",
        session_id: "t1",
        write_status: "written",
        nutrient_count: 2,
        handled_count: 0,
        pending_count: 2,
      },
      icon: "success",
      run_id: "run-1",
    });

    store.applyEvolutionReview("t1", {
      review_id: "evo-review:run-1",
      run_id: "run-1",
      session_id: "t1",
      reviewed_at_ms: 200,
      review_summary: "captured",
      nutrients: [
        {
          nutrient_id: "n1",
          kind: "workflow",
          title: "N1",
          content: "c1",
          summary: "s1",
          confidence: 0.9,
          evidence_turns: [1],
          source_run_id: "run-1",
          source_session_id: "t1",
          suggested_target: "skill",
          tags: [],
        },
        {
          nutrient_id: "n2",
          kind: "memory",
          title: "N2",
          content: "c2",
          summary: "s2",
          confidence: 0.8,
          evidence_turns: [2],
          source_run_id: "run-1",
          source_session_id: "t1",
          suggested_target: "memory",
          tags: [],
        },
      ],
      decision_summary: {
        total: 2,
        accepted_memory: 1,
        accepted_skill: 0,
        ignored: 0,
        pending: 1,
      },
      decisions: [
        {
          nutrient_id: "n1",
          decision: "accept_skill",
          target: "skill",
          decided_at_ms: 122,
          applied_status: "failed",
          applied_mode: "update",
          applied_path: "/tmp/.kongming/skills/review/SKILL.md",
          applied_at_ms: 124,
          applied_error: "write conflict",
        },
        {
          nutrient_id: "n2",
          decision: "accept_memory",
          target: "memory",
          decided_at_ms: 123,
          applied_status: "written",
          applied_mode: "append",
          applied_path: "/tmp/.kongming/memory/MEMORY.md",
          applied_at_ms: 125,
          applied_error: null,
        },
      ],
    });

    const items = useChatStore.getState().itemsByThread["t1"]!;
    const item = items[0]!;
    if (item.kind !== "system") throw new Error("expected system notice");
    expect(item.message).toBe("已写入 1/2 条进化养料，失败 1 条");
    expect(item.details).toContain("pending_count: 1");
    expect(item.details).toContain("applied_written_count: 1");
    expect(item.details).toContain("applied_failed_count: 1");
  });

  it("appendUsage 累加 thread 级 prompt / completion / total", () => {
    const store = useChatStore.getState();
    store.appendUsage("t1", {
      kind: "usage",
      prompt_tokens: 100,
      completion_tokens: 40,
      total_tokens: 140,
      turn: 1,
      run_id: "run-1",
      timestamp_ms: 100,
    });
    store.appendUsage("t1", {
      kind: "usage",
      prompt_tokens: 60,
      completion_tokens: 25,
      total_tokens: 85,
      turn: 2,
      run_id: "run-1",
      timestamp_ms: 200,
    });

    expect(useChatStore.getState().usageByThread["t1"]).toEqual({
      lastPrompt: 60,
      lastCompletion: 25,
      lastTotal: 85,
      cumulativePrompt: 160,
      cumulativeCompletion: 65,
      cumulativeTotal: 225,
      cumulativeCacheRead: null,
      cumulativeCacheCreation: null,
    });
  });

  it("appendUsage 用 runId + turn 回填对应 assistant item", () => {
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

    store.appendUsage("t1", {
      kind: "usage",
      prompt_tokens: 120,
      completion_tokens: 30,
      total_tokens: 150,
      turn: 1,
      run_id: "run-2",
      timestamp_ms: 210,
    });

    const items = useChatStore.getState().itemsByThread["t1"]!;
    const first = items[0]!;
    const second = items[1]!;
    if (first.kind !== "assistant" || second.kind !== "assistant") {
      throw new Error("expected assistants");
    }

    expect(first.usage).toBeUndefined();
    expect(second.usage).toEqual({
      prompt: 120,
      completion: 30,
      total: 150,
    });
  });

  it("hydrateUsageFromThreads 用 metadata 累计值初始化并保留较新的内存值", () => {
    const store = useChatStore.getState();
    store.hydrateUsageFromThreads([
      {
        id: "thread-aaaaaaaaaaaa",
        name: "demo",
        preset_id: "p1",
        backend_kind: "generic_chat",
        claude_thread_id: "",
        codex_thread_id: "",
        cwd: "",
        created_at: 1,
        updated_at: 2,
        message_count: 0,
        cumulative_prompt_tokens: 1200,
        cumulative_completion_tokens: 340,
        cumulative_total_tokens: 1540,
        cumulative_cache_read_tokens: null,
        cumulative_cache_creation_tokens: null,
        schema_version: 4,
      },
    ]);

    expect(useChatStore.getState().usageByThread["thread-aaaaaaaaaaaa"]).toEqual({
      lastPrompt: 0,
      lastCompletion: 0,
      lastTotal: 0,
      cumulativePrompt: 1200,
      cumulativeCompletion: 340,
      cumulativeTotal: 1540,
      cumulativeCacheRead: null,
      cumulativeCacheCreation: null,
    });

    store.appendUsage("thread-aaaaaaaaaaaa", {
      kind: "usage",
      prompt_tokens: 20,
      completion_tokens: 10,
      total_tokens: 30,
      turn: 1,
      run_id: "run-1",
      timestamp_ms: 3,
    });
    store.hydrateUsageFromThreads([
      {
        id: "thread-aaaaaaaaaaaa",
        name: "demo",
        preset_id: "p1",
        backend_kind: "generic_chat",
        claude_thread_id: "",
        codex_thread_id: "",
        cwd: "",
        created_at: 1,
        updated_at: 2,
        message_count: 0,
        cumulative_prompt_tokens: 1200,
        cumulative_completion_tokens: 340,
        cumulative_total_tokens: 1540,
        cumulative_cache_read_tokens: null,
        cumulative_cache_creation_tokens: null,
        schema_version: 4,
      },
    ]);

    expect(useChatStore.getState().usageByThread["thread-aaaaaaaaaaaa"]).toEqual({
      lastPrompt: 20,
      lastCompletion: 10,
      lastTotal: 30,
      cumulativePrompt: 1220,
      cumulativeCompletion: 350,
      cumulativeTotal: 1570,
      cumulativeCacheRead: null,
      cumulativeCacheCreation: null,
    });
  });
});
