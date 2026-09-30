/**
 * track-timeline-v0.1 · 轨迹纯派生函数测试。
 *
 * 覆盖：搜索过滤（命中 / 无命中 / 空 keyword）、轮次分组与起止时间推导、
 * 账本行展平（折叠 / 展开）、时长与 token 格式化。
 */

import { describe, expect, it } from "vitest";
import type { TrajectoryRecord } from "@/protocol";
import {
  buildLedgerRows,
  buildTurnGroups,
  filterRecords,
  formatDurationLabel,
  formatTokensLabel,
  laneForKind,
} from "../derive";

function record(overrides: Partial<TrajectoryRecord>): TrajectoryRecord {
  return {
    seq: 0,
    kind: "llm",
    run_index: 1,
    run_id: "t-1",
    turn: 1,
    summary: "",
    started_at_ms: 1000,
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
    ...overrides,
  };
}

describe("filterRecords", () => {
  it("按摘要 / 工具名 / 入参 / 输出 / 错误命中（大小写不敏感）", () => {
    const records = [
      record({ seq: 1, summary: "读配置" }),
      record({ seq: 2, kind: "tool", tool_name: "read_file" }),
      record({ seq: 3, kind: "tool", input_detail: '{"path": "runner.py"}' }),
      record({ seq: 4, kind: "tool", output_detail: "debug: true" }),
      record({ seq: 5, kind: "tool", ok: false, error_message: "boom" }),
    ];
    expect(filterRecords(records, "runner.py").map((r) => r.seq)).toEqual([3]);
    expect(filterRecords(records, "READ_FILE").map((r) => r.seq)).toEqual([2]);
    expect(filterRecords(records, "boom").map((r) => r.seq)).toEqual([5]);
  });

  it("空关键词返回原列表；无命中返回空列表", () => {
    const records = [record({ seq: 1 })];
    expect(filterRecords(records, "  ")).toBe(records);
    expect(filterRecords(records, "不存在的词")).toEqual([]);
  });
});

describe("buildTurnGroups / buildLedgerRows", () => {
  const records = [
    record({ seq: 1, kind: "user_input", turn: 1, started_at_ms: 1000 }),
    record({ seq: 2, kind: "llm", turn: 1, started_at_ms: 1100, duration_ms: 400 }),
    record({ seq: 3, kind: "tool", turn: 1, started_at_ms: 1600, duration_ms: 100 }),
    record({ seq: 4, kind: "llm", turn: 2, started_at_ms: 2000, duration_ms: 300 }),
    record({ seq: 5, kind: "llm", run_index: 2, turn: 1, started_at_ms: 5000 }),
  ];

  it("按 run × turn 分组并推导起止时间", () => {
    const groups = buildTurnGroups(records);
    expect(groups.map((g) => g.key)).toEqual(["1:1", "1:2", "2:1"]);
    expect(groups[0].startedAtMs).toBe(1000);
    expect(groups[0].endedAtMs).toBe(1700);
    expect(groups[2].runIndex).toBe(2);
  });

  it("折叠分组后只保留轮次头行", () => {
    const groups = buildTurnGroups(records);
    const full = buildLedgerRows(groups, new Set());
    expect(full).toHaveLength(8); // 3 头 + 5 记录
    const collapsed = buildLedgerRows(groups, new Set(["1:1"]));
    expect(collapsed).toHaveLength(5); // 3 头 + 2 记录（1:1 的 3 条折叠）
    expect(collapsed[1].type).toBe("turn-header");
    expect(collapsed[2].type).toBe("record");
    expect(collapsed[2].record?.seq).toBe(4);
  });
});

describe("格式化与泳道", () => {
  it("时长 / token 标签", () => {
    expect(formatDurationLabel(null)).toBe("—");
    expect(formatDurationLabel(540)).toBe("540ms");
    expect(formatDurationLabel(1_234)).toBe("1.2s");
    expect(formatDurationLabel(123_000)).toBe("2m03s");
    expect(formatTokensLabel(null)).toBe("—");
    expect(formatTokensLabel(999)).toBe("999");
    expect(formatTokensLabel(1_234)).toBe("1.2k");
  });

  it("三泳道归属：输入上 / 模型中 / 工具下", () => {
    expect(laneForKind("system")).toBe(0);
    expect(laneForKind("user_input")).toBe(0);
    expect(laneForKind("corrupted")).toBe(0);
    expect(laneForKind("llm")).toBe(1);
    expect(laneForKind("tool")).toBe(2);
  });
});
