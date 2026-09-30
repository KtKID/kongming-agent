/**
 * 轨迹时间线 wire 协议契约测试（track-timeline-v0.1，约束 17 双侧漂移证据）。
 *
 * 与 Python 侧 `tests/unit/test_web_protocol_round_trip.py`
 * `_make_trajectory_response` fixture 结构逐字段对齐：
 * 字段名 / 嵌套 / 可空性与 Python Pydantic DTO 一致。
 * Python 侧改字段必须同步改本 fixture（反之亦然）。
 */
import { describe, expect, it } from "vitest";
import type {
  TrajectoryRecord,
  TrajectoryRecordKind,
  TrajectoryResponse,
  TrajectoryRun,
  TrajectoryRunStatus,
  TrajectorySummary,
  TrajectoryTokens,
} from "@/protocol";

const allRecordKinds = [
  "system",
  "user_input",
  "llm",
  "tool",
  "corrupted",
] as const satisfies readonly TrajectoryRecordKind[];

const allRunStatuses = [
  "running",
  "completed",
  "failed",
  "cancelled",
] as const satisfies readonly TrajectoryRunStatus[];

function validTokens(): TrajectoryTokens {
  return {
    input_total: 100,
    output_total: 20,
    reasoning: 5,
    cache_read: null,
    cache_write: null,
  };
}

function validSummary(): TrajectorySummary {
  return {
    status: "running",
    total_duration_ms: 1500,
    turn_count: 1,
    tool_call_count: 1,
    corrupted_count: 0,
    tokens: validTokens(),
  };
}

function validRun(): TrajectoryRun {
  return {
    run_index: 1,
    run_id: "thread-abcdef012345-1",
    status: "running",
    started_at_ms: 1700000000000,
    ended_at_ms: null,
    duration_ms: null,
    turn_count: 1,
    tokens: { input_total: 100, output_total: 20 },
  };
}

function validRecords(): TrajectoryRecord[] {
  return [
    {
      seq: -1,
      kind: "system",
      run_index: 0,
      run_id: "",
      turn: null,
      summary: "系统提示词 · 1200 字",
      started_at_ms: 1699999999000,
      duration_ms: null,
      ok: null,
      tool_name: null,
      call_id: null,
      ttft_ms: null,
      finish_reason: null,
      tokens: null,
      input_detail: "# 孔明系统提示词",
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
      started_at_ms: 1700000000000,
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
      seq: 4,
      kind: "llm",
      run_index: 1,
      run_id: "thread-abcdef012345-1",
      turn: 1,
      summary: "我来读取配置文件",
      started_at_ms: 1700000000100,
      duration_ms: 800,
      ok: null,
      tool_name: null,
      call_id: null,
      ttft_ms: 120,
      finish_reason: "tool_calls",
      tokens: { input_total: 100, output_total: 20, reasoning: 5 },
      input_detail: null,
      output_detail: "我来读取配置文件",
      reasoning_detail: "用户要读配置",
      error_message: null,
    },
    {
      seq: 6,
      kind: "tool",
      run_index: 1,
      run_id: "thread-abcdef012345-1",
      turn: 1,
      summary: "read_file",
      started_at_ms: 1700000000950,
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
  ];
}

function validResponse(): TrajectoryResponse {
  return {
    thread_id: "thread-abcdef012345",
    summary: validSummary(),
    runs: [validRun()],
    records: validRecords(),
  };
}

describe("TrajectoryResponse wire 契约", () => {
  it("完整 fixture 满足 TS 类型（与 Python fixture 结构对齐）", () => {
    const response: TrajectoryResponse = validResponse();
    expect(response.thread_id).toBe("thread-abcdef012345");
    expect(response.records).toHaveLength(4);
    expect(response.runs[0]?.run_index).toBe(1);
  });

  it("kind / status 取值集合与 Python StrEnum 对齐", () => {
    expect(allRecordKinds).toHaveLength(5);
    expect(allRunStatuses).toHaveLength(4);
    const kinds = new Set(validRecords().map((record) => record.kind));
    expect(kinds.has("system")).toBe(true);
    expect(kinds.has("user_input")).toBe(true);
    expect(kinds.has("llm")).toBe(true);
    expect(kinds.has("tool")).toBe(true);
  });

  it("缺必填字段时类型拒绝（seq / kind / summary）", () => {
    const record = validRecords()[0] as unknown as Record<string, unknown>;
    delete record.seq;
    // @ts-expect-error 缺 seq 不满足 TrajectoryRecord 契约
    const broken: TrajectoryRecord = record;
    expect(broken).toBeDefined();
  });

  it("空轨迹合法：runs / records 空数组", () => {
    const empty: TrajectoryResponse = {
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
    };
    expect(empty.records).toHaveLength(0);
  });
});
