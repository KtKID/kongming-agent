import { describe, expect, it } from "vitest";
import { formatLogLines } from "../formatter";

describe("formatLogLines", () => {
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
