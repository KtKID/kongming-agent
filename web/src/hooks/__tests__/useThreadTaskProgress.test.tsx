import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  toThreadTaskProgressViewModel,
  useThreadTaskProgress,
} from "@/hooks/useThreadTaskProgress";
import { apiGetThreadTaskProgress } from "@/lib/api";
import type { ThreadTaskProgressSnapshot } from "@/protocol";

vi.mock("@/lib/api", () => ({
  apiGetThreadTaskProgress: vi.fn(),
}));

const mockApiGetThreadTaskProgress = vi.mocked(apiGetThreadTaskProgress);

async function flushPromises() {
  await act(async () => {
    await Promise.resolve();
  });
}

function makeSnapshot(
  threadId: string,
  tasks: ThreadTaskProgressSnapshot["tasks"],
): ThreadTaskProgressSnapshot {
  return {
    schema_version: 2,
    session_id: threadId,
    workflow_id: tasks.length > 0 ? "wf-current" : null,
    title: tasks.length > 0 ? "当前计划" : null,
    control_mode: tasks.length > 0 ? "llm_steps" : null,
    updated_at_ms: 1781190000000,
    tasks,
    counts: {
      pending: tasks.filter((item) => item.status === "pending").length,
      in_progress: tasks.filter((item) => item.status === "in_progress").length,
      completed: tasks.filter((item) => item.status === "completed").length,
      failed: tasks.filter((item) => item.status === "failed").length,
      cancelled: tasks.filter((item) => item.status === "cancelled").length,
      total: tasks.length,
    },
  };
}

function task(
  taskId: string,
  status: ThreadTaskProgressSnapshot["tasks"][number]["status"],
  displayOrder: number,
  desc = taskId,
): ThreadTaskProgressSnapshot["tasks"][number] {
  return {
    task_id: taskId,
    task_run_id: `${displayOrder + 1}-${taskId}`,
    desc,
    depends_on: [],
    status,
    display_order: displayOrder,
    error_message: status === "failed" ? "child failed" : null,
    updated_at_ms: 1781190000000 + displayOrder,
  };
}

describe("useThreadTaskProgress", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mockApiGetThreadTaskProgress.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("maps v2 tasks to sorted compact checklist with terminal labels", () => {
    const snapshot = makeSnapshot("thread-1", [
      task("implement", "in_progress", 2, "实现前端进度入口"),
      task("contract", "completed", 1, "梳理接口合同"),
      task("verify", "failed", 3, "补充轮询测试"),
      task("cleanup", "cancelled", 4, "清理旧入口"),
    ]);

    const viewModel = toThreadTaskProgressViewModel(snapshot);

    expect(viewModel.items.map((item) => item.desc)).toEqual([
      "梳理接口合同",
      "实现前端进度入口",
      "补充轮询测试",
      "清理旧入口",
    ]);
    expect(viewModel.items.map((item) => item.status_label)).toEqual([
      "已完成",
      "进行中",
      "失败",
      "已取消",
    ]);
    expect(viewModel.items.map((item) => item.icon_variant)).toEqual([
      "check_circle",
      "active_ring",
      "error_circle",
      "error_circle",
    ]);
    expect(viewModel.items[0].key).toBe("wf-current:contract");
  });

  it("caps rendered items and long desc text", () => {
    const longDesc = "x".repeat(1200);
    const snapshot = makeSnapshot(
      "thread-1",
      Array.from({ length: 129 }, (_, index) =>
        task(`task-${index}`, "pending", index, index === 0 ? longDesc : `任务 ${index}`),
      ),
    );

    const viewModel = toThreadTaskProgressViewModel(snapshot);

    expect(viewModel.items).toHaveLength(128);
    expect(viewModel.items[0].desc).toHaveLength(1000);
  });

  it("fetches immediately and refreshes every 2 seconds while open", async () => {
    const snapshot = makeSnapshot("thread-1", []);
    mockApiGetThreadTaskProgress.mockResolvedValue(snapshot);

    const { result } = renderHook(() =>
      useThreadTaskProgress("thread-1", { enabled: true }),
    );

    expect(mockApiGetThreadTaskProgress).toHaveBeenCalledWith("thread-1");
    await flushPromises();
    expect(result.current.snapshot).toEqual(snapshot);

    await act(async () => {
      vi.advanceTimersByTime(2000);
      await Promise.resolve();
    });
    expect(mockApiGetThreadTaskProgress).toHaveBeenCalledTimes(2);
  });

  it("stops refreshing when closed and starts against the next thread", async () => {
    mockApiGetThreadTaskProgress.mockImplementation((threadId: string) =>
      Promise.resolve(makeSnapshot(threadId, [])),
    );
    const { rerender } = renderHook(
      ({ threadId, enabled }) =>
        useThreadTaskProgress(threadId, { enabled, refreshMs: 2000 }),
      { initialProps: { threadId: "thread-1", enabled: true } },
    );

    await flushPromises();
    rerender({ threadId: "thread-1", enabled: false });
    const callCountAfterClose = mockApiGetThreadTaskProgress.mock.calls.length;
    await act(async () => {
      vi.advanceTimersByTime(4000);
      await Promise.resolve();
    });
    expect(mockApiGetThreadTaskProgress).toHaveBeenCalledTimes(callCountAfterClose);

    rerender({ threadId: "thread-2", enabled: true });
    await flushPromises();
    expect(mockApiGetThreadTaskProgress).toHaveBeenCalledWith("thread-2");
  });
});
