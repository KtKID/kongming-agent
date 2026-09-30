/**
 * track-timeline-v0.1 · TrajectoryPanel 组件交互测试。
 *
 * mock `@/lib/api`（apiGet / ApiError），走真实 TrajectoryManager →
 * TrajectoryStore → Panel 全链路。覆盖：
 * 加载态 / 空态（feat01 场景2）/ 不存在错误态 + 返回入口（场景5）/
 * 完整渲染与统计（feat06 场景1-2）/ 损坏占位与不完整提示（feat01 场景3）/
 * 轮次折叠（feat02 场景2）/ 工具调用收起（场景3）/ 时间线点选联动（feat03
 * 场景3）/ 详情超长截断展开（feat04 场景4）/ 搜索无命中与清空恢复
 * （feat05 场景1-3）/ 刷新按钮（feat01 场景4）。
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TrajectoryResponse } from "@/protocol";

const apiGetMock = vi.fn();

vi.mock("@/lib/api", () => {
  class ApiError extends Error {
    constructor(
      public status: number,
      public errorCode: string,
      public detail: string,
    ) {
      super(detail);
    }
  }
  return { apiGet: (...args: unknown[]) => apiGetMock(...args), ApiError };
});

import { TrajectoryPanel } from "../TrajectoryPanel";
import { useTrajectoryStore } from "../TrajectoryStore";

function fullResponse(): TrajectoryResponse {
  return {
    thread_id: "thread-abcdef012345",
    summary: {
      status: "running",
      total_duration_ms: 2113,
      turn_count: 1,
      tool_call_count: 1,
      corrupted_count: 1,
      tokens: { input_total: 100, output_total: 30 },
    },
    runs: [
      {
        run_index: 1,
        run_id: "thread-abcdef012345-1",
        status: "running",
        started_at_ms: 1_700_000_000_000,
        ended_at_ms: null,
        duration_ms: null,
        turn_count: 1,
        tokens: { input_total: 100, output_total: 30 },
      },
    ],
    records: [
      {
        seq: -1,
        kind: "system",
        run_index: 0,
        run_id: "",
        turn: null,
        summary: "系统提示词 · 1200 字",
        started_at_ms: 1_699_999_999_000,
        duration_ms: null,
        ok: null,
        tool_name: null,
        call_id: null,
        ttft_ms: null,
        finish_reason: null,
        tokens: null,
        input_detail: "# 孔明系统提示词全文",
        output_detail: null,
        reasoning_detail: null,
        error_message: null,
      },
      {
        seq: -2,
        kind: "user_input",
        run_index: 1,
        run_id: "thread-abcdef012345-1",
        turn: 1,
        summary: "帮我读一下配置文件",
        started_at_ms: 1_700_000_000_000,
        duration_ms: null,
        ok: null,
        tool_name: null,
        call_id: null,
        ttft_ms: null,
        finish_reason: null,
        tokens: null,
        input_detail: "帮我读一下配置文件",
        output_detail: null,
        reasoning_detail: null,
        error_message: null,
      },
      {
        seq: 3,
        kind: "llm",
        run_index: 1,
        run_id: "thread-abcdef012345-1",
        turn: 1,
        summary: "我来读取配置文件",
        started_at_ms: 1_700_000_000_100,
        duration_ms: 800,
        ok: null,
        tool_name: null,
        call_id: null,
        ttft_ms: 120,
        finish_reason: "tool_calls",
        tokens: { input_total: 100, output_total: 30, reasoning: 5 },
        input_detail: null,
        output_detail: "我来读取配置文件",
        reasoning_detail: "用户要读配置",
        error_message: null,
      },
      {
        seq: 4,
        kind: "tool",
        run_index: 1,
        run_id: "thread-abcdef012345-1",
        turn: 1,
        summary: "read_file",
        started_at_ms: 1_700_000_000_950,
        duration_ms: 150,
        ok: true,
        tool_name: "read_file",
        call_id: "call_1",
        ttft_ms: null,
        finish_reason: null,
        tokens: null,
        input_detail: '{"path": "setting.yaml"}',
        output_detail: "debug: true",
        reasoning_detail: null,
        error_message: null,
      },
      {
        seq: 5,
        kind: "corrupted",
        run_index: 0,
        run_id: "",
        turn: null,
        summary: "记录损坏（无法解析）",
        started_at_ms: null,
        duration_ms: null,
        ok: null,
        tool_name: null,
        call_id: null,
        ttft_ms: null,
        finish_reason: null,
        tokens: null,
        input_detail: null,
        output_detail: null,
        reasoning_detail: null,
        error_message: null,
      },
    ],
  };
}

function renderPanel(threadId = "thread-abcdef012345"): void {
  render(
    <MemoryRouter>
      <TrajectoryPanel threadId={threadId} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  apiGetMock.mockReset();
  useTrajectoryStore.setState({
    dataByThread: {},
    loadingByThread: {},
    errorByThread: {},
    selectedSeqByThread: {},
    searchByThread: {},
    collapsedTurnsByThread: {},
    toolsCollapsedByThread: {},
  });
});

describe("TrajectoryPanel 状态", () => {
  it("加载中显示加载态", async () => {
    apiGetMock.mockImplementation(() => new Promise(() => undefined));
    renderPanel();
    expect(screen.getByTestId("trajectory-loading")).toBeDefined();
  });

  it("空轨迹显示空态与引导（feat01 场景2）", async () => {
    apiGetMock.mockResolvedValue({
      thread_id: "thread-abcdef012345",
      summary: {
        status: "completed",
        total_duration_ms: 0,
        turn_count: 0,
        tool_call_count: 0,
        corrupted_count: 0,
        tokens: {},
      },
      runs: [],
      records: [],
    });
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-empty")).toBeDefined(),
    );
    expect(screen.getByText("暂无轨迹数据")).toBeDefined();
  });

  it("404 显示会话不存在 + 返回会话列表入口（feat01 场景5）", async () => {
    const { ApiError } = await import("@/lib/api");
    apiGetMock.mockRejectedValue(new ApiError(404, "internal", "not found"));
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-not-found")).toBeDefined(),
    );
    expect(screen.getByText("返回会话列表")).toBeDefined();
  });
});

describe("TrajectoryPanel 数据渲染与交互", () => {
  beforeEach(() => {
    apiGetMock.mockResolvedValue(fullResponse());
  });

  it("渲染统计（进行中 / 总时长 / 调用数 / 损坏提示）与运行分段（feat06）", async () => {
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-summary")).toBeDefined(),
    );
    expect(screen.getByTestId("trajectory-status-badge").textContent).toContain(
      "进行中",
    );
    expect(screen.getByTestId("trajectory-summary").textContent).toContain(
      "1 次调用",
    );
    expect(screen.getByTestId("trajectory-summary").textContent).toContain(
      "轨迹数据不完整",
    );
    expect(screen.getByTestId("trajectory-runs").textContent).toContain("#1");
  });

  it("损坏占位记录保留在列表且不可展开（feat01 场景3）", async () => {
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-record-5")).toBeDefined(),
    );
    await userEvent.click(screen.getByTestId("trajectory-record-5"));
    const inspector = screen.getByTestId("trajectory-inspector");
    expect(inspector.textContent).toContain("记录损坏");
    expect(inspector.textContent).toContain("不可展开");
  });

  it("轮次折叠：一键收起后只留轮次头摘要行（feat02 场景2）", async () => {
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-record--2")).toBeDefined(),
    );
    await userEvent.click(screen.getByTestId("trajectory-toggle-turns"));
    expect(screen.queryByTestId("trajectory-record--2")).toBeNull();
    expect(screen.getByTestId("trajectory-turn-header-1:1").textContent).toContain(
      "3 条记录",
    );
    await userEvent.click(screen.getByTestId("trajectory-toggle-turns"));
    expect(screen.getByTestId("trajectory-record--2")).toBeDefined();
  });

  it("调用一键收起：工具记录隐藏（feat02 场景3）", async () => {
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-record-4")).toBeDefined(),
    );
    await userEvent.click(screen.getByTestId("trajectory-toggle-tools"));
    expect(screen.queryByTestId("trajectory-record-4")).toBeNull();
    expect(screen.getByTestId("trajectory-record--2")).toBeDefined();
  });

  it("时间线点选联动：选中模型段 → 账本高亮 + 详情展示 TTFT 与思考（feat03 场景3、feat04 场景2）", async () => {
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-timeline-seg-3")).toBeDefined(),
    );
    await userEvent.click(screen.getByTestId("trajectory-timeline-seg-3"));
    const inspector = screen.getByTestId("trajectory-inspector");
    expect(inspector.textContent).toContain("模型回复");
    expect(inspector.textContent).toContain("120ms");
    expect(inspector.textContent).toContain("用户要读配置");
    expect(screen.getByTestId("trajectory-record-3").className).toContain(
      "primary",
    );
  });

  it("工具详情展示完整入参与输出（feat04 场景1、feat07 场景2）", async () => {
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-record-4")).toBeDefined(),
    );
    await userEvent.click(screen.getByTestId("trajectory-record-4"));
    const inspector = screen.getByTestId("trajectory-inspector");
    expect(inspector.textContent).toContain("read_file");
    expect(inspector.textContent).toContain('{"path": "setting.yaml"}');
    expect(inspector.textContent).toContain("debug: true");
  });

  it("超长输出默认截断 + 展开全部（feat04 场景4）", async () => {
    const long = "x".repeat(3000);
    const response = fullResponse();
    response.records[2].output_detail = long; // llm 记录（seq=3）超长回复
    apiGetMock.mockResolvedValue(response);
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-record-3")).toBeDefined(),
    );
    await userEvent.click(screen.getByTestId("trajectory-record-3"));
    const expand = screen.getByTestId("trajectory-detail-expand-回复全文");
    expect(expand.textContent).toContain("3000 字符");
    await userEvent.click(expand);
    const pre = document.querySelector(
      '[data-testid="trajectory-detail-回复全文"] pre',
    );
    expect(pre?.textContent?.length).toBe(3000);
  });

  it("搜索：命中过滤高亮 / 无命中提示 / 清空恢复（feat05 场景1-3）", async () => {
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-record--2")).toBeDefined(),
    );
    await userEvent.type(
      screen.getByTestId("trajectory-search-input"),
      "runner.py",
    );
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-search-empty")).toBeDefined(),
    );
    expect(screen.queryByTestId("trajectory-record--2")).toBeNull();

    // 命中：工具入参含 setting.yaml
    await userEvent.clear(screen.getByTestId("trajectory-search-input"));
    await userEvent.type(
      screen.getByTestId("trajectory-search-input"),
      "setting.yaml",
    );
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-record-4")).toBeDefined(),
    );
    expect(screen.queryByTestId("trajectory-record--2")).toBeNull();

    // 清空恢复完整列表
    await userEvent.clear(screen.getByTestId("trajectory-search-input"));
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-record--2")).toBeDefined(),
    );
  });

  it("系统提示词记录：会话信息分组最前，详情展示全文（feat02 场景6）", async () => {
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-record--1")).toBeDefined(),
    );
    expect(screen.getByTestId("trajectory-turn-header-0:0").textContent).toContain(
      "会话信息",
    );
    await userEvent.click(screen.getByTestId("trajectory-record--1"));
    const inspector = screen.getByTestId("trajectory-inspector");
    expect(inspector.textContent).toContain("系统提示词");
    expect(inspector.textContent).toContain("# 孔明系统提示词全文");
  });

  it("刷新按钮再次拉取（feat01 场景4）", async () => {
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-refresh")).toBeDefined(),
    );
    expect(apiGetMock).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByTestId("trajectory-refresh"));
    expect(apiGetMock).toHaveBeenCalledTimes(2);
  });
});
