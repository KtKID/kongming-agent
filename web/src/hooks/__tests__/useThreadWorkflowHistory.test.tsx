import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  toThreadWorkflowHistoryItems,
  useThreadWorkflowHistory,
} from "@/hooks/useThreadWorkflowHistory";
import { fetchAgentWorkflows } from "@/modules/agent-workflow-viewer/api";
import type {
  WorkflowListDTO,
  WorkflowListItemDTO,
} from "@/modules/agent-workflow-viewer/types";

vi.mock("@/modules/agent-workflow-viewer/api", () => ({
  fetchAgentWorkflows: vi.fn(),
}));

const mockFetchAgentWorkflows = vi.mocked(fetchAgentWorkflows);

async function flushPromises() {
  await act(async () => {
    await Promise.resolve();
  });
}

function makeWorkflow(
  workflowId: string,
  title: string,
  status: string,
): WorkflowListItemDTO {
  return {
    workflow_id: workflowId,
    thread_id: "thread-1",
    mode: "parallel",
    status,
    started_at: "2026-06-12T01:00:00Z",
    finished_at: status === "completed" ? "2026-06-12T01:05:00Z" : null,
    desc: title,
    title,
    report_count: 1,
    has_mode_panel: false,
    usage: {
      source: "workflow",
      totals: {},
      provider_totals: {},
      records: [],
      diagnostics: [],
    },
    diagnostics: [],
  };
}

function makeList(workflows: WorkflowListItemDTO[]): WorkflowListDTO {
  return {
    thread_id: "thread-1",
    workflows,
  };
}

describe("useThreadWorkflowHistory", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mockFetchAgentWorkflows.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("maps workflow list items to compact history items", () => {
    const items = toThreadWorkflowHistoryItems([
      makeWorkflow("wf-1", "统一回复 hi", "completed"),
      makeWorkflow("wf-2", "继续实现", "running"),
    ]);

    expect(items).toEqual([
      {
        workflow_id: "wf-1",
        title: "统一回复 hi",
        status: "completed",
        status_label: "已完成",
        started_at: "2026-06-12T01:00:00Z",
        finished_at: "2026-06-12T01:05:00Z",
      },
      {
        workflow_id: "wf-2",
        title: "继续实现",
        status: "running",
        status_label: "进行中",
        started_at: "2026-06-12T01:00:00Z",
        finished_at: null,
      },
    ]);
  });

  it("fetches immediately and refreshes while enabled", async () => {
    mockFetchAgentWorkflows.mockResolvedValue(
      makeList([makeWorkflow("wf-1", "统一回复 hi", "completed")]),
    );

    const { result } = renderHook(() =>
      useThreadWorkflowHistory("thread-1", { enabled: true }),
    );

    expect(mockFetchAgentWorkflows).toHaveBeenCalledTimes(1);
    expect(mockFetchAgentWorkflows).toHaveBeenCalledWith("thread-1");

    await flushPromises();

    expect(result.current.items[0].title).toBe("统一回复 hi");

    await act(async () => {
      vi.advanceTimersByTime(5000);
      await Promise.resolve();
    });

    expect(mockFetchAgentWorkflows).toHaveBeenCalledTimes(2);
  });

  it("clears history and stops refreshing when disabled", async () => {
    mockFetchAgentWorkflows.mockResolvedValue(
      makeList([makeWorkflow("wf-1", "统一回复 hi", "completed")]),
    );

    const { result, rerender } = renderHook(
      ({ enabled }) =>
        useThreadWorkflowHistory("thread-1", { enabled, refreshMs: 1000 }),
      { initialProps: { enabled: true } },
    );

    await flushPromises();
    expect(result.current.items).toHaveLength(1);

    rerender({ enabled: false });
    const callCountAfterDisable = mockFetchAgentWorkflows.mock.calls.length;

    expect(result.current.items).toHaveLength(0);

    await act(async () => {
      vi.advanceTimersByTime(3000);
      await Promise.resolve();
    });

    expect(mockFetchAgentWorkflows).toHaveBeenCalledTimes(
      callCountAfterDisable,
    );
  });
});
