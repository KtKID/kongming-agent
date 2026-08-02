import { beforeEach, describe, expect, it } from "vitest";

import type { ThreadStatusFrame } from "@/protocol";
import { useThreadStatusStore } from "@/stores/threadStatus";

function frame(
  threadId: string,
  sequence: number,
  phase: ThreadStatusFrame["phase"] = "responding",
): ThreadStatusFrame {
  return {
    frame_type: "thread-status",
    threadId,
    phase,
    sequence,
    runId: `run-${threadId}`,
    runGeneration: 1,
  };
}

beforeEach(() => {
  useThreadStatusStore.setState({
    statuses: {},
    connectionGeneration: 0,
    lastSequence: 0,
  });
});

describe("threadStatus server projection", () => {
  it("reconnect snapshot atomically replaces stale running entries", () => {
    const store = useThreadStatusStore.getState();
    store.beginConnection(1);
    store.applySnapshot(
      {
        frame_type: "thread-status.snapshot",
        watermark: 4,
        items: [frame("stale", 4)],
      },
      1,
    );
    store.beginConnection(2);
    store.applySnapshot(
      {
        frame_type: "thread-status.snapshot",
        watermark: 8,
        items: [frame("current", 8, "thinking")],
      },
      2,
    );

    expect(Object.keys(useThreadStatusStore.getState().statuses)).toEqual([
      "current",
    ]);
  });

  it("ignores late snapshot and delta from an older connection generation", () => {
    const store = useThreadStatusStore.getState();
    store.beginConnection(1);
    store.beginConnection(2);
    store.applySnapshot(
      {
        frame_type: "thread-status.snapshot",
        watermark: 10,
        items: [],
      },
      2,
    );

    store.applyStatus(frame("stale", 11), 1);
    store.applySnapshot(
      {
        frame_type: "thread-status.snapshot",
        watermark: 20,
        items: [frame("stale", 20)],
      },
      1,
    );

    expect(useThreadStatusStore.getState().statuses).toEqual({});
    expect(useThreadStatusStore.getState().lastSequence).toBe(10);
  });

  it("applies monotonic deltas and terminal removes the active entry", () => {
    const store = useThreadStatusStore.getState();
    store.beginConnection(1);
    store.applySnapshot(
      {
        frame_type: "thread-status.snapshot",
        watermark: 2,
        items: [frame("thread-a", 2)],
      },
      1,
    );
    store.applyStatus(frame("thread-a", 4, "tool_calling"), 1);
    store.applyStatus(frame("thread-a", 3, "thinking"), 1);
    expect(
      useThreadStatusStore.getState().statuses["thread-a"]?.phase,
    ).toBe("tool_calling");

    store.applyStatus(frame("thread-a", 5, "complete"), 1);
    expect(useThreadStatusStore.getState().statuses["thread-a"]).toBeUndefined();
    expect(useThreadStatusStore.getState().lastSequence).toBe(5);
  });
});
