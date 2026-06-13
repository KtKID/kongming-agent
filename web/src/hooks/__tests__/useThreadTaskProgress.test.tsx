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
    schema_version: 1,
    session_id: threadId,
    updated_at_ms: 1781190000000,
    source: "workflow",
    tasks,
    counts: {
      pending: tasks.filter((item) => item.status === "pending").length,
      in_progress: tasks.filter((item) => item.status === "in_progress").length,
      completed: tasks.filter((item) => item.status === "completed").length,
      total: tasks.length,
    },
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

  it("maps snapshot tasks to sorted compact checklist view model", () => {
    const snapshot = makeSnapshot("thread-1", [
      {
        id: "wf:2",
        orchestration_task_id: "wf:2",
        task_id: "implement",
        task_run_id: "2",
        desc: "实现前端进度入口",
        status: "in_progress",
        display_order: 2,
      },
      {
        id: "wf:1",
        orchestration_task_id: "wf:1",
        task_id: "contract",
        task_run_id: "1",
        desc: "梳理接口合同",
        status: "completed",
        display_order: 1,
      },
      {
        id: "wf:3",
        orchestration_task_id: "wf:3",
        task_id: "verify",
        task_run_id: "3",
        desc: "补充轮询测试",
        status: "pending",
        display_order: 3,
      },
    ]);

    const viewModel = toThreadTaskProgressViewModel(snapshot);

    expect(viewModel.items.map((item) => item.desc)).toEqual([
      "梳理接口合同",
      "实现前端进度入口",
      "补充轮询测试",
    ]);
    expect(viewModel.items.map((item) => item.status_label)).toEqual([
      "已完成",
      "进行中",
      "未完成",
    ]);
    expect(viewModel.items.map((item) => item.icon_variant)).toEqual([
      "check_circle",
      "active_ring",
      "ring",
    ]);
    expect(viewModel.items[0].aria_label).toBe("已完成：梳理接口合同");
  });

  it("caps rendered items and long desc text", () => {
    const longDesc = "x".repeat(1200);
    const snapshot = makeSnapshot(
      "thread-1",
      Array.from({ length: 129 }, (_, index) => ({
        id: `wf:${index}`,
        orchestration_task_id: `wf:${index}`,
        task_id: `task-${index}`,
        task_run_id: `${index}`,
        desc: index === 0 ? longDesc : `任务 ${index}`,
        status: "pending",
        display_order: index,
      })),
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

    expect(mockApiGetThreadTaskProgress).toHaveBeenCalledTimes(1);
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
      {
        initialProps: { threadId: "thread-1", enabled: true },
      },
    );

    await flushPromises();

    expect(mockApiGetThreadTaskProgress).toHaveBeenCalledWith("thread-1");

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
