import { describe, expect, it } from "vitest";
import type {
  ThreadTaskProgressSnapshot,
  ThreadTaskProgressStatus,
} from "@/protocol";

const allStatuses = [
  "pending",
  "in_progress",
  "completed",
  "failed",
  "cancelled",
] as const satisfies readonly ThreadTaskProgressStatus[];

function validFixture(): ThreadTaskProgressSnapshot {
  return {
    schema_version: 2,
    session_id: "thread-abcdef123456",
    workflow_id: "wf-current",
    title: "当前任务流",
    control_mode: "llm_steps",
    updated_at_ms: 1,
    tasks: allStatuses.map((status, displayOrder) => ({
      task_id: `step-${displayOrder + 1}`,
      task_run_id: `00${displayOrder + 1}-step-${displayOrder + 1}`,
      desc: `步骤 ${displayOrder + 1}`,
      depends_on: [],
      status,
      error_message: status === "failed" ? "child failed" : null,
      display_order: displayOrder,
      updated_at_ms: displayOrder + 1,
    })),
    counts: {
      pending: 1,
      in_progress: 1,
      completed: 1,
      failed: 1,
      cancelled: 1,
      total: 5,
    },
  };
}

function assertTypeRejections(snapshot: ThreadTaskProgressSnapshot): void {
  const unknownField: ThreadTaskProgressSnapshot = {
    ...snapshot,
    // @ts-expect-error v2 wire 不再接受旧 source 字段。
    source: "workflow",
  };
  const missingCounts: ThreadTaskProgressSnapshot = {
    ...snapshot,
    // @ts-expect-error v2 counts 必须包含五态和 total。
    counts: { total: 0 },
  };
  void unknownField;
  void missingCounts;
}

describe("task progress v2 TypeScript wire contract", () => {
  it("accepts five statuses and rejects legacy or incomplete fixtures at compile time", () => {
    const snapshot = validFixture();

    assertTypeRejections(snapshot);
    expect(snapshot.tasks.map((task) => task.status)).toEqual(allStatuses);
    expect(snapshot.tasks.find((task) => task.status === "failed")?.error_message).toBe(
      "child failed",
    );
  });
});
