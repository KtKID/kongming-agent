import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  __setChatLogPolicyForTest,
  clearChatLog,
  flushChatDeltaLog,
  getChatLog,
  logChatDelta,
  setChatConsoleLog,
} from "../logger";

describe("chat logger · delta 摘要", () => {
  let restorePolicy: (() => void) | null = null;

  beforeEach(() => {
    vi.useFakeTimers();
    clearChatLog();
    setChatConsoleLog(false);
    restorePolicy = __setChatLogPolicyForTest(1000);
  });

  afterEach(() => {
    restorePolicy?.();
    restorePolicy = null;
    clearChatLog();
    setChatConsoleLog(true);
    vi.useRealTimers();
  });

  it("1000 个 delta 在一个窗口只留下无正文摘要", () => {
    for (let index = 0; index < 1000; index += 1) {
      logChatDelta({ threadId: "t1", runId: "run-1", turnId: "turn-1", content: "secret" });
    }
    expect(getChatLog()).toEqual([]);

    vi.advanceTimersByTime(1000);
    const entries = getChatLog();
    expect(entries).toHaveLength(1);
    expect(entries[0]?.detail).toMatchObject({
      eventCount: 1000,
      contentChars: 6000,
      reasoningChars: 0,
      reason: "interval",
    });
    expect(JSON.stringify(entries[0]?.detail)).not.toContain("secret");
  });

  it("terminal 在 interval 前立即提交剩余摘要并取消旧 timer", () => {
    logChatDelta({ threadId: "t1", runId: "run-1", turnId: "turn-1", reasoning: "thinking" });
    flushChatDeltaLog("t1", "run-1", "turn-1", "terminal");
    vi.advanceTimersByTime(1000);

    const entries = getChatLog();
    expect(entries).toHaveLength(1);
    expect(entries[0]?.detail).toMatchObject({
      eventCount: 1,
      contentChars: 0,
      reasoningChars: 8,
      reason: "terminal",
    });
  });

  it("generic llm_error 缺少 run_id 时按当前 thread 收口摘要", () => {
    logChatDelta({ threadId: "t1", runId: "run-1", turnId: "run-1:turn-4", content: "partial" });
    flushChatDeltaLog("t1", "", "t1-turn-4", "stream-error");

    expect(getChatLog()).toHaveLength(1);
    expect(getChatLog()[0]?.detail).toMatchObject({
      runId: "run-1",
      turnId: "run-1:turn-4",
      reason: "stream-error",
    });
  });
});
