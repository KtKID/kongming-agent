/**
 * Sitian manager + useSitian 派生 selectors 测试
 *
 * 覆盖：
 *  - manager.open()  fetch 成功 → store.report 写入、isOpen=true、loading=false
 *  - manager.open()  fetch 404  → store.noReport=true、report=null、isOpen=true
 *  - manager.open()  fetch 5xx  → store.error 非空、report=null、isOpen=true
 *  - manager.close() 全字段 reset
 *  - manager.refresh() 不改 isOpen，但写 report
 *  - useSitian().sortedAlerts 按 priority P0>P1>P2、同 priority 按 severity high>medium>low 排序
 *  - useSitian().dedupedProjects 按 projectId 去重，保留首次出现的
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { sitianManager } from "@/modules/sitian/manager";
import { useSitianStore } from "@/modules/sitian/store";
import { useSitian } from "@/modules/sitian/hooks/useSitian";
import type {
  ProjectStatus,
  SitianReport,
  TopAlert,
} from "@/modules/sitian/types";

const originalFetch = globalThis.fetch;

function makeReport(overrides: Partial<SitianReport> = {}): SitianReport {
  return {
    reportId: "sitian_test",
    generatedAt: "2026-05-16T00:00:00Z",
    modelName: "test-model",
    summary: "test summary",
    errors: [],
    topAlerts: [],
    projects: [],
    ...overrides,
  };
}

function makeAlert(
  alertId: string,
  priority: TopAlert["priority"],
  severity: TopAlert["severity"],
): TopAlert {
  return {
    alertId,
    dedupeKey: alertId,
    priority,
    severity,
    title: `title-${alertId}`,
    displayMessage: `msg-${alertId}`,
    recommendation: `rec-${alertId}`,
    sessionRef: {
      projectId: "/p",
      projectName: "p",
      sessionId: "sid",
    },
    evidence: [],
  };
}

function makeProject(projectId: string): ProjectStatus {
  return {
    projectId,
    projectName: projectId.split("/").pop() ?? projectId,
    statusByRule: "active",
    statusReason: "test",
    narrative: "narrative",
    alertIds: [],
    sessionStats: {
      total: 1,
      activeWithin1h: 0,
      activeWithin24h: 0,
      activeWithin3d: 0,
    },
  };
}

function mockFetchOnce(response: Response): void {
  globalThis.fetch = vi
    .fn()
    .mockResolvedValueOnce(response) as unknown as typeof fetch;
}

describe("sitian manager", () => {
  beforeEach(() => {
    useSitianStore.getState().reset();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.resetAllMocks();
    useSitianStore.getState().reset();
  });

  it("open() 成功路径写入 report 并保持 isOpen", async () => {
    const report = makeReport({ summary: "ok" });
    mockFetchOnce(
      new Response(JSON.stringify(report), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await sitianManager.open();

    const state = useSitianStore.getState();
    expect(state.isOpen).toBe(true);
    expect(state.loading).toBe(false);
    expect(state.error).toBeNull();
    expect(state.noReport).toBe(false);
    expect(state.report).toEqual(report);
  });

  it("open() 404 → noReport=true，不写 error", async () => {
    mockFetchOnce(
      new Response(
        JSON.stringify({
          error_code: "internal",
          message: "sitian report not generated yet",
          details: { reason: "no_report" },
        }),
        {
          status: 404,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );

    await sitianManager.open();

    const state = useSitianStore.getState();
    expect(state.isOpen).toBe(true);
    expect(state.loading).toBe(false);
    expect(state.noReport).toBe(true);
    expect(state.report).toBeNull();
    expect(state.error).toBeNull();
  });

  it("open() 5xx → error 非空，不写 noReport", async () => {
    mockFetchOnce(
      new Response(
        JSON.stringify({
          error_code: "internal",
          message: "sitian report file corrupted",
          details: { reason: "report_corrupted" },
        }),
        {
          status: 500,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );

    await sitianManager.open();

    const state = useSitianStore.getState();
    expect(state.isOpen).toBe(true);
    expect(state.loading).toBe(false);
    expect(state.noReport).toBe(false);
    expect(state.report).toBeNull();
    expect(state.error).not.toBeNull();
    expect(state.error).toContain("司天报告获取失败");
  });

  it("close() 重置所有字段", () => {
    useSitianStore.setState({
      isOpen: true,
      loading: true,
      error: "old",
      noReport: true,
      report: makeReport(),
    });

    sitianManager.close();

    const state = useSitianStore.getState();
    expect(state.isOpen).toBe(false);
    expect(state.loading).toBe(false);
    expect(state.error).toBeNull();
    expect(state.noReport).toBe(false);
    expect(state.report).toBeNull();
  });

  it("refresh() 不修改 isOpen，但拉报告", async () => {
    useSitianStore.setState({ isOpen: false });
    const report = makeReport({ summary: "refreshed" });
    mockFetchOnce(
      new Response(JSON.stringify(report), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await sitianManager.refresh();

    const state = useSitianStore.getState();
    expect(state.isOpen).toBe(false);
    expect(state.report).toEqual(report);
  });
});

describe("useSitian 派生 selectors", () => {
  beforeEach(() => {
    useSitianStore.getState().reset();
  });

  afterEach(() => {
    useSitianStore.getState().reset();
  });

  it("sortedAlerts 按 P0>P1>P2 + 同优先级 high>medium>low 排序", () => {
    const alerts: TopAlert[] = [
      makeAlert("a", "P2", "low"),
      makeAlert("b", "P0", "medium"),
      makeAlert("c", "P1", "high"),
      makeAlert("d", "P0", "high"),
      makeAlert("e", "P1", "low"),
    ];
    const { result } = renderHook(() => useSitian());
    act(() => {
      useSitianStore.setState({
        report: makeReport({ topAlerts: alerts }),
      });
    });

    expect(result.current.sortedAlerts.map((a) => a.alertId)).toEqual([
      "d", // P0 high
      "b", // P0 medium
      "c", // P1 high
      "e", // P1 low
      "a", // P2 low
    ]);
  });

  it("dedupedProjects 按 projectId 去重，保留首次出现", () => {
    const projects: ProjectStatus[] = [
      makeProject("/path/a"),
      makeProject("/path/b"),
      makeProject("/path/a"), // 重复
      makeProject("/path/c"),
      makeProject("/path/b"), // 重复
    ];
    const { result } = renderHook(() => useSitian());
    act(() => {
      useSitianStore.setState({
        report: makeReport({ projects }),
      });
    });

    expect(result.current.dedupedProjects.map((p) => p.projectId)).toEqual([
      "/path/a",
      "/path/b",
      "/path/c",
    ]);
  });

  it("report=null 时 sortedAlerts/dedupedProjects 返回空数组", () => {
    useSitianStore.setState({ report: null });

    const { result } = renderHook(() => useSitian());

    expect(result.current.sortedAlerts).toEqual([]);
    expect(result.current.dedupedProjects).toEqual([]);
  });

  it("报告 schema 漂移（topAlerts/projects 字段缺失）时不崩，返回空数组", () => {
    // 模拟手工编辑或老版本写盘缺字段的脏数据
    const driftedReport = {
      reportId: "drift",
      generatedAt: "2026-05-16T00:00:00Z",
      modelName: "test",
      summary: "drifted",
      errors: [],
      // topAlerts / projects 故意缺失
    } as unknown as SitianReport;

    const { result } = renderHook(() => useSitian());
    act(() => {
      useSitianStore.setState({ report: driftedReport });
    });

    expect(result.current.sortedAlerts).toEqual([]);
    expect(result.current.dedupedProjects).toEqual([]);
  });
});
