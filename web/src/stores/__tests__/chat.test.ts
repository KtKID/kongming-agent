import { describe, it, expect, beforeEach } from "vitest";
import { useChatStore } from "@/stores/chat";

describe("stores/chat", () => {
  beforeEach(() => {
    useChatStore.setState({ itemsByThread: {}, usageByThread: {} });
  });

  it("appendUser → push user item", () => {
    useChatStore.getState().appendUser("t1", "hi there");
    const items = useChatStore.getState().itemsByThread["t1"]!;
    expect(items[0]!.kind).toBe("user");
  });

  it("setHistory → 历史消息映射成 ChatItem", () => {
    useChatStore.getState().setHistory("t1", [
      {
        id: "h-user-1",
        frame_type: "text",
        role: "user",
        content: "u",
        provider: "generic_chat",
        timestamp: "2026-06-04T00:00:00.100Z",
      },
      {
        id: "h-assistant-1",
        frame_type: "text",
        role: "assistant",
        content: "a",
        provider: "generic_chat",
        timestamp: "2026-06-04T00:00:00.110Z",
      },
    ]);
    const items = useChatStore.getState().itemsByThread["t1"]!;
    expect(items).toHaveLength(2);
    expect(items[0]!.kind).toBe("user");
    expect(items[1]!.kind).toBe("assistant");
  });

  it("setHistory → tool_result 历史映射成 tool ChatItem", () => {
    useChatStore.getState().setHistory("t1", [
      {
        id: "h-tool-1",
        frame_type: "tool_result",
        toolId: "call-1",
        toolName: "read_file",
        content: "file body",
        isError: false,
        provider: "generic_chat",
        timestamp: "2026-06-04T00:00:00.250Z",
      },
      {
        id: "h-tool-2",
        frame_type: "tool_result",
        toolId: "call-2",
        toolName: "shell",
        content: "exit 1",
        isError: true,
        provider: "generic_chat",
        timestamp: "2026-06-04T00:00:00.300Z",
      },
    ]);

    const items = useChatStore.getState().itemsByThread["t1"]!;
    expect(items).toHaveLength(2);
    const okTool = items[0]!;
    const failedTool = items[1]!;
    if (okTool.kind !== "tool" || failedTool.kind !== "tool") {
      throw new Error("expected tool items");
    }
    expect(okTool.id).toBe("h-tool-1");
    expect(okTool.callId).toBe("call-1");
    expect(okTool.toolName).toBe("read_file");
    expect(okTool.ok).toBe(true);
    expect(okTool.result).toBe("file body");
    expect(okTool.timestampMs).toBe(Date.parse("2026-06-04T00:00:00.250Z"));
    expect(failedTool.ok).toBe(false);
    expect(failedTool.errorMessage).toBe("exit 1");
  });

  it("appendAssistantFinal 用 frame.run_id 区分同 turn 不同 run", () => {
    const store = useChatStore.getState();
    store.appendAssistantFinal("t1", {
      frame_type: "assistant.final",
      content: "first",
      turn: 1,
      run_id: "run-1",
      timestamp_ms: 100,
    });
    store.appendAssistantFinal("t1", {
      frame_type: "assistant.final",
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
      frame_type: "system.notice",
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
      frame_type: "system.notice",
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
      frame_type: "system.notice",
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
      frame_type: "system.notice",
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
      frame_type: "system.notice",
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

  it("appendUsage 把 v2 UsageFrame.usage 写到 usageByThread", () => {
    const store = useChatStore.getState();
    store.appendUsage("t1", {
      frame_type: "usage",
      timestamp_ms: 100,
      turn: 1,
      run_id: "run-1",
      usage: {
        provider: "claude",
        input_tokens: 100,
        output_tokens: 40,
        cache_read_input_tokens: 0,
        cache_creation_input_tokens: 0,
        cache_creation: {
          ephemeral_1h_input_tokens: 0,
          ephemeral_5m_input_tokens: 0,
        },
        context_usage: 100,
        model: "claude-opus-4",
        context_window: 1000000,
      },
    });
    store.appendUsage("t1", {
      frame_type: "usage",
      timestamp_ms: 200,
      turn: 2,
      run_id: "run-1",
      usage: {
        provider: "claude",
        input_tokens: 60,
        output_tokens: 25,
        cache_read_input_tokens: 0,
        cache_creation_input_tokens: 0,
        cache_creation: {
          ephemeral_1h_input_tokens: 0,
          ephemeral_5m_input_tokens: 0,
        },
        context_usage: 60,
        model: "claude-opus-4",
        context_window: 1000000,
      },
    });

    const usage = useChatStore.getState().usageByThread["t1"]!;
    if (usage.provider !== "claude") throw new Error("expected claude");
    // 第二次 append 覆盖第一次
    expect(usage.input_tokens).toBe(60);
    expect(usage.output_tokens).toBe(25);
  });

  it("appendUsage 用 runId + turn 回填对应 assistant item", () => {
    const store = useChatStore.getState();
    store.appendAssistantFinal("t1", {
      frame_type: "assistant.final",
      content: "first",
      turn: 1,
      run_id: "run-1",
      timestamp_ms: 100,
    });
    store.appendAssistantFinal("t1", {
      frame_type: "assistant.final",
      content: "second",
      turn: 1,
      run_id: "run-2",
      timestamp_ms: 200,
    });

    store.appendUsage("t1", {
      frame_type: "usage",
      timestamp_ms: 210,
      turn: 1,
      run_id: "run-2",
      usage: {
        provider: "claude",
        input_tokens: 120,
        output_tokens: 30,
        cache_read_input_tokens: 0,
        cache_creation_input_tokens: 0,
        cache_creation: {
          ephemeral_1h_input_tokens: 0,
          ephemeral_5m_input_tokens: 0,
        },
        context_usage: 120,
        model: "claude-opus-4",
        context_window: 1000000,
      },
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

  it("fetchThreadUsage 成功：把端点返回的 ThreadUsage 写入 usageByThread", async () => {
    const tid = "thread-cccccccccccc";
    const fakeUsage = {
      provider: "claude" as const,
      input_tokens: 42,
      output_tokens: 17,
      cache_read_input_tokens: 100,
      cache_creation_input_tokens: 50,
      cache_creation: {
        ephemeral_1h_input_tokens: 30,
        ephemeral_5m_input_tokens: 20,
      },
      context_usage: 192,
      model: "claude-sonnet-4-5",
      context_window: 200000,
    };
    const originalFetch = globalThis.fetch;
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ usage: fakeUsage }), {
        status: 200,
        headers: { "content-type": "application/json" },
      })) as typeof fetch;
    try {
      await useChatStore.getState().fetchThreadUsage(tid);
      const stored = useChatStore.getState().usageByThread[tid]!;
      if (stored.provider !== "claude") throw new Error("expected claude");
      expect(stored.input_tokens).toBe(42);
      expect(stored.model).toBe("claude-sonnet-4-5");
      expect(stored.cache_creation.ephemeral_1h_input_tokens).toBe(30);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it("fetchThreadUsage：usage=null 不写 store（保持 undefined → 底栏「等待对话」）", async () => {
    const tid = "thread-dddddddddddd";
    const originalFetch = globalThis.fetch;
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ usage: null }), {
        status: 200,
        headers: { "content-type": "application/json" },
      })) as typeof fetch;
    try {
      await useChatStore.getState().fetchThreadUsage(tid);
      expect(useChatStore.getState().usageByThread[tid]).toBeUndefined();
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it("fetchThreadUsage：404 静默失败，store 保持 undefined", async () => {
    const tid = "thread-eeeeeeeeeeee";
    const originalFetch = globalThis.fetch;
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ detail: "not found" }), {
        status: 404,
        headers: { "content-type": "application/json" },
      })) as typeof fetch;
    try {
      await useChatStore.getState().fetchThreadUsage(tid);
      expect(useChatStore.getState().usageByThread[tid]).toBeUndefined();
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it("v2 setState 模拟 WS usage_summary_updated 帧更新 usageByThread", () => {
    // v2：直接把 ThreadUsage DTO 写入 store（不再有 summary / lastSnapshot 分离）
    const tid = "thread-bbbbbbbbbbbb";
    const usage = {
      provider: "claude" as const,
      input_tokens: 5000,
      output_tokens: 800,
      cache_read_input_tokens: 3000,
      cache_creation_input_tokens: 200,
      cache_creation: {
        ephemeral_1h_input_tokens: 200,
        ephemeral_5m_input_tokens: 0,
      },
      context_usage: 8200,
      model: "claude-opus-4",
      context_window: 1000000,
    };
    useChatStore.setState({
      usageByThread: {
        ...useChatStore.getState().usageByThread,
        [tid]: usage,
      },
    });
    const stored = useChatStore.getState().usageByThread[tid]!;
    if (stored.provider !== "claude") throw new Error("expected claude");
    expect(stored.input_tokens).toBe(5000);
    expect(stored.model).toBe("claude-opus-4");
    expect(stored.context_window).toBe(1000000);
  });
});
