import { afterEach, describe, expect, it } from "vitest";
import fixture from "../__fixtures__/streaming-render-oom-v01.json";
import { ChatManager } from "../ChatManager";
import { toViewModel } from "../ChatRenderAdapter";
import { ChatTimelineStore } from "../ChatTimelineStore";
import { clearChatLog, getChatLog, setChatConsoleLog } from "../logger";
import type { NetworkHandle, RawFrameEnvelope } from "../types";

const chunkCount = fixture.chunks.reduce((total, chunk) => total + chunk.repeat, 0);

function makeManager(): { manager: ChatManager; store: ChatTimelineStore } {
  const store = new ChatTimelineStore(fixture.thread_id);
  const handle: NetworkHandle = { connectionId: fixture.thread_id, send: () => {}, close: () => {} };
  return {
    store,
    manager: new ChatManager({
      ensureThread: async (request) => request.provider.threadId,
      resolveHandle: () => handle,
      timelineFor: () => store,
    }),
  };
}

function envelope(frame: unknown, receivedAt: number): RawFrameEnvelope {
  return {
    connectionId: fixture.thread_id,
    channel: "generic",
    threadId: fixture.thread_id,
    frame,
    receivedAt,
  };
}

function replayOnce(): { content: string; reasoning: string; logJson: string } {
  const { manager, store } = makeManager();
  let timestamp = 1;
  manager.ingestFrame(envelope({
    frame_type: "turn.start",
    timestamp_ms: timestamp,
    turn: fixture.turn,
    run_id: fixture.run_id,
  }, timestamp));
  for (const chunk of fixture.chunks) {
    for (let index = 0; index < chunk.repeat; index += 1) {
      timestamp += chunk.interval_ms;
      manager.ingestFrame(envelope({
        frame_type: chunk.channel === "content" ? "content.delta" : "reasoning.delta",
        timestamp_ms: timestamp,
        turn: fixture.turn,
        run_id: fixture.run_id,
        delta: chunk.delta,
        seq: 0,
      }, timestamp));
    }
  }
  timestamp += fixture.terminal.relative_delay_ms;
  manager.ingestFrame(envelope({
    frame_type: fixture.terminal.kind,
    timestamp_ms: timestamp,
    turn: fixture.turn,
    run_id: fixture.run_id,
  }, timestamp));

  const assistant = toViewModel(store.getSnapshot()).items.find(
    (item) => item.kind === "message" && item.role === "assistant",
  );
  if (!assistant || assistant.kind !== "message") throw new Error("missing replay assistant");
  return {
    content: assistant.content,
    reasoning: assistant.reasoning ?? "",
    logJson: JSON.stringify(getChatLog()),
  };
}

describe("streaming-render-oom-v01 replay fixture", () => {
  afterEach(() => {
    clearChatLog();
    setChatConsoleLog(true);
  });

  it("脱敏 fixture 展开为 1557 个 delta，warm-up 后连续三次走真实 ChatManager 主链", () => {
    expect(fixture.schema_version).toBe(1);
    expect(chunkCount).toBe(1557);
    expect(JSON.stringify(fixture)).not.toMatch(/user_input|credential|token|\/Users\//i);

    setChatConsoleLog(false);
    const expectedContent = fixture.chunks
      .filter((chunk) => chunk.channel === "content")
      .map((chunk) => chunk.delta.repeat(chunk.repeat))
      .join("");
    const expectedReasoning = fixture.chunks
      .filter((chunk) => chunk.channel === "reasoning")
      .map((chunk) => chunk.delta.repeat(chunk.repeat))
      .join("");

    const warmup = replayOnce();
    expect(warmup.content).toBe(expectedContent);
    expect(warmup.reasoning).toBe(expectedReasoning);

    clearChatLog();
    const runs = [replayOnce(), replayOnce(), replayOnce()];
    for (const run of runs) {
      expect(run.content).toBe(expectedContent);
      expect(run.reasoning).toBe(expectedReasoning);
      expect(run.logJson).not.toContain(fixture.chunks[0]?.delta ?? "");
    }
  });
});
