import { describe, expect, it } from "vitest";
import { formatLogLines } from "../formatter";

describe("formatLogLines", () => {
  it("formats session conversation rows with message_id and role", () => {
    const parsed = {
      schema_version: "0.1.2",
      record_type: "message",
      session_id: "thread-abcdef123456",
      model_name: "MiniMax-M3",
      message_id: "b0f6d4a8-c310-4f54-9942-737f56dafc1f",
      parent_message_id: "cccdeb49-fdff-4e1b-8533-aee6f16510e2",
      created_at: 1783525587.371499,
      message: {
        role: "assistant",
        content: "我已经掌握了这个项目的全貌。",
        tool_calls: null,
        metadata: {},
      },
    };

    const view = formatLogLines(
      [{ line_no: 12, raw: JSON.stringify(parsed), parsed }],
      "jsonl",
      { sourceType: "session_conversation" },
    );

    expect(view).toHaveLength(1);
    expect(view[0]).toMatchObject({
      key: "session-12",
      kind: "json",
      level: "info",
      summary: "b0f6d4a8 · ASSISTANT · 我已经掌握了这个项目的全貌。",
      badges: ["ASSISTANT", "parent:cccdeb49"],
    });
    expect(view[0].time).toMatch(/\d{2}:\d{2}:\d{2}\.\d{3}/);
    expect(view[0].prettyJson).toContain('"message_id"');
  });

  it("keeps session summaries stable when records contain metadata turn", () => {
    const parsed = {
      schema_version: "0.1.2",
      record_type: "message",
      session_id: "thread-abcdef123456",
      message_id: "abcdef12-3456-7890-abcd-ef1234567890",
      parent_message_id: null,
      created_at: 1783525587.371499,
      message: {
        role: "user",
        content: "hello",
        metadata: { turn: 3 },
      },
    };

    const view = formatLogLines(
      [{ line_no: 1, raw: JSON.stringify(parsed), parsed }],
      "jsonl",
      { sourceType: "session_conversation" },
    );

    expect(view[0].summary).toBe("abcdef12 · USER · hello");
    expect(view[0].badges).toEqual(["USER"]);
  });

  it("stops traceback blocks before bare uvicorn INFO lines", () => {
    const lines = [
      { raw: "Traceback (most recent call last):" },
      { raw: '  File "src/web/run.py", line 1, in <module>' },
      { raw: "ModuleNotFoundError: No module named 'claude_agent_sdk'" },
      { raw: "INFO:     Started server process [42852]" },
      { raw: "INFO:     Application startup complete." },
    ];

    const view = formatLogLines(lines, "plain");

    expect(view).toHaveLength(3);
    expect(view[0]).toMatchObject({
      kind: "traceback",
      level: "error",
    });
    expect(view[0].raw).not.toContain("Started server process");
    expect(view[1]).toMatchObject({
      kind: "plain",
      level: "info",
    });
    expect(view[2]).toMatchObject({
      kind: "plain",
      level: "info",
    });
  });

  it("stops traceback blocks before timestamped server lines", () => {
    const lines = [
      { raw: "Traceback (most recent call last):" },
      { raw: "RuntimeError: failed" },
      { raw: "2026-06-04 17:30:00 INFO:     Started server process [1]" },
    ];

    const view = formatLogLines(lines, "plain");

    expect(view).toHaveLength(2);
    expect(view[0].kind).toBe("traceback");
    expect(view[1]).toMatchObject({
      kind: "plain",
      level: "info",
      time: "17:30:00.000",
    });
  });
});
