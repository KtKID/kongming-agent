import { describe, expect, it } from "vitest";
import { PendingInputQueueManager } from "../PendingInputQueueManager";
import type {
  PendingInputChangedFrame,
  PendingInputDTO,
  PendingInputSnapshotFrame,
  PendingInputStartedFrame,
} from "@/protocol";

function item(id: string, sequence: number): PendingInputDTO {
  return {
    id,
    thread_id: "thread-aaaaaaaaaaaa",
    source: "user_input",
    priority: "user_message",
    content: id,
    preview: id,
    status: "queued",
    created_at_ms: sequence,
    updated_at_ms: sequence,
    sequence,
    metadata: {},
  };
}

describe("PendingInputQueueManager", () => {
  it("applies snapshot and ignores stale versions", () => {
    const state = PendingInputQueueManager.empty("thread-aaaaaaaaaaaa");
    const frame: PendingInputSnapshotFrame = {
      frame_type: "pending-input.snapshot",
      timestamp_ms: 1,
      thread_id: "thread-aaaaaaaaaaaa",
      items: [item("pin-1", 1)],
      max_items: 20,
      active_run_id: null,
      version: 2,
    };
    const applied = PendingInputQueueManager.applySnapshot(state, frame);
    const stale = PendingInputQueueManager.applySnapshot(applied, {
      ...frame,
      items: [item("pin-old", 1)],
      version: 1,
    });

    expect(applied.items).toHaveLength(1);
    expect(stale.items[0]?.id).toBe("pin-1");
  });

  it("applies changed and started frames", () => {
    const changed: PendingInputChangedFrame = {
      frame_type: "pending-input.changed",
      timestamp_ms: 1,
      thread_id: "thread-aaaaaaaaaaaa",
      items: [item("pin-1", 1), item("pin-2", 2)],
      max_items: 20,
      reason: "added",
      active_run_id: null,
      version: 1,
    };
    const started: PendingInputStartedFrame = {
      frame_type: "pending-input.started",
      timestamp_ms: 2,
      thread_id: "thread-aaaaaaaaaaaa",
      pending_input_id: "pin-1",
      pending_input: { ...item("pin-1", 1), content: "updated", preview: "updated" },
      run_id: "run-1",
      version: 2,
    };

    const queued = PendingInputQueueManager.applyChanged(
      PendingInputQueueManager.empty("thread-aaaaaaaaaaaa"),
      changed,
    );
    const next = PendingInputQueueManager.applyStarted(queued, started);

    expect(next.items.map((entry) => entry.id)).toEqual(["pin-2"]);
    expect(next.activeRunId).toBe("run-1");
    expect(next.lastStartedId).toBe("pin-1");
  });

  it("builds operation frames", () => {
    expect(PendingInputQueueManager.buildUpdateFrame("pin-1", "updated")).toEqual({
      frame_type: "pending-input.update",
      pending_input_id: "pin-1",
      content: "updated",
    });
    expect(PendingInputQueueManager.buildCancelFrame("pin-1")).toEqual({
      frame_type: "pending-input.cancel",
      pending_input_id: "pin-1",
    });
    expect(PendingInputQueueManager.buildSendNowFrame("pin-1")).toEqual({
      frame_type: "pending-input.send-now",
      pending_input_id: "pin-1",
      request_id: null,
    });
    expect(PendingInputQueueManager.buildReorderFrame(["pin-2", "pin-1"])).toEqual({
      frame_type: "pending-input.reorder",
      ordered_ids: ["pin-2", "pin-1"],
    });
  });
});
