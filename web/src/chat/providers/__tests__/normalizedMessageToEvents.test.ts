import { describe, expect, it } from "vitest";
import { normalizedMessageToEvents } from "../normalizedMessageToEvents";
import type { NormalizedMessage } from "@/protocol";

function msg(partial: NormalizedMessage): NormalizedMessage {
  return {
    provider: "claude",
    sessionId: "session-1",
    timestamp: "2026-06-04T00:00:00.000Z",
    ...partial,
  };
}

describe("normalizedMessageToEvents", () => {
  it("stream_status(tool_calling) 产 pending tool_call_started", () => {
    const events = normalizedMessageToEvents(
      "claude",
      "thread-1",
      msg({ frame_type: "stream_status", phase: "tool_calling", toolId: "tool-1" }),
      100,
    );

    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      provider: "claude",
      threadId: "thread-1",
      turnId: "session-1",
      kind: "tool_call_started",
      toolCallId: "tool-1",
      payload: { pending: true },
    });
    expect(events[0].createdAt).toBe(Date.parse("2026-06-04T00:00:00.000Z"));
  });

  it("stream_delta(input_json) 产 tool_call_delta partialInputDelta", () => {
    const events = normalizedMessageToEvents(
      "claude",
      "thread-1",
      msg({
        frame_type: "stream_delta",
        deltaType: "input_json",
        toolId: "tool-1",
        content: '{"path":"/tmp/a"}',
      }),
      100,
    );

    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      kind: "tool_call_delta",
      toolCallId: "tool-1",
      payload: { partialInputDelta: '{"path":"/tmp/a"}' },
    });
  });

  it("非 tool_calling status 与非 input_json delta 不进时间线", () => {
    expect(
      normalizedMessageToEvents(
        "claude",
        "thread-1",
        msg({ frame_type: "stream_status", phase: "responding", toolId: "tool-1" }),
        100,
      ),
    ).toEqual([]);

    expect(
      normalizedMessageToEvents(
        "claude",
        "thread-1",
        msg({ frame_type: "stream_delta", deltaType: "text", toolId: "tool-1", content: "hello" }),
        100,
      ),
    ).toEqual([]);
  });
});
