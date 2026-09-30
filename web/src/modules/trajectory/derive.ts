/**
 * track-timeline-v0.1 · 轨迹时间线纯派生工具。
 *
 * 从 wire DTO（`@/protocol` 的 TrajectoryResponse）派生 UI 需要的分组 /
 * 过滤 / 泳道 / 格式化结果。全部为纯函数，便于单测与组件复用。
 */

import type {
  TrajectoryRecord,
  TrajectoryRecordKind,
  TrajectoryResponse,
} from "@/protocol";

/** 轮次分组键：同一 run 内的同一 turn。 */
export type TurnGroupKey = string;

export interface TurnGroup {
  key: TurnGroupKey;
  runIndex: number;
  turn: number | null;
  records: TrajectoryRecord[];
  /** 该轮最早开始时间（毫秒），缺测取组内最早的 started_at_ms。 */
  startedAtMs: number | null;
  /** 该轮最后结束时间（毫秒）。 */
  endedAtMs: number | null;
}

export interface LedgerRow {
  type: "turn-header" | "record";
  key: string;
  group?: TurnGroup;
  record?: TrajectoryRecord;
}

/** 泳道归属：输入（user/corrupted）上、模型中、工具下（对齐 dsh 三泳道）。 */
export function laneForKind(kind: TrajectoryRecordKind): 0 | 1 | 2 {
  if (kind === "tool") return 2;
  if (kind === "llm") return 1;
  return 0;
}

/** 记录在搜索过滤中暴露的文本（摘要 / 工具名 / 入参 / 输出 / 错误）。 */
export function searchableText(record: TrajectoryRecord): string {
  return [
    record.summary,
    record.tool_name ?? "",
    record.input_detail ?? "",
    record.output_detail ?? "",
    record.error_message ?? "",
    record.reasoning_detail ?? "",
  ]
    .join("\n")
    .toLowerCase();
}

/** 按关键词过滤记录（大小写不敏感；空关键词返回原列表）。 */
export function filterRecords(
  records: TrajectoryRecord[],
  query: string,
): TrajectoryRecord[] {
  const q = query.trim().toLowerCase();
  if (!q) return records;
  return records.filter((record) => searchableText(record).includes(q));
}

/** 按顺序把记录切成 run × turn 分组（保持原始顺序，不排序）。 */
export function buildTurnGroups(records: TrajectoryRecord[]): TurnGroup[] {
  const groups: TurnGroup[] = [];
  const byKey = new Map<TurnGroupKey, TurnGroup>();
  for (const record of records) {
    const key = `${record.run_index}:${record.turn ?? 0}`;
    let group = byKey.get(key);
    if (!group) {
      group = {
        key,
        runIndex: record.run_index,
        turn: record.turn,
        records: [],
        startedAtMs: record.started_at_ms,
        endedAtMs: null,
      };
      byKey.set(key, group);
      groups.push(group);
    }
    group.records.push(record);
    if (
      record.started_at_ms !== null &&
      (group.startedAtMs === null || record.started_at_ms < group.startedAtMs)
    ) {
      group.startedAtMs = record.started_at_ms;
    }
    const end =
      record.started_at_ms !== null && record.duration_ms !== null
        ? record.started_at_ms + record.duration_ms
        : record.started_at_ms;
    if (end !== null && (group.endedAtMs === null || end > group.endedAtMs)) {
      group.endedAtMs = end;
    }
  }
  return groups;
}

/** 把分组展平成账本行（含轮次头），collapseKeys 控制折叠轮次。 */
export function buildLedgerRows(
  groups: TurnGroup[],
  collapsedKeys: ReadonlySet<TurnGroupKey>,
): LedgerRow[] {
  const rows: LedgerRow[] = [];
  for (const group of groups) {
    rows.push({ type: "turn-header", key: `header:${group.key}`, group });
    if (collapsedKeys.has(group.key)) continue;
    for (const record of group.records) {
      rows.push({ type: "record", key: `record:${record.seq}`, record });
    }
  }
  return rows;
}

/** 毫秒 → 人类可读时长（如 "540ms" / "1.2s" / "2m03s"）。 */
export function formatDurationLabel(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "—";
  if (ms < 1_000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1_000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1_000);
  return `${minutes}m${String(seconds).padStart(2, "0")}s`;
}

/** token 数 → 紧凑标签（如 "1.2k" / "3.4M"）。 */
export function formatTokensLabel(count: number | null | undefined): string {
  if (count === null || count === undefined) return "—";
  if (count < 1_000) return String(count);
  if (count < 1_000_000) return `${(count / 1_000).toFixed(1)}k`;
  return `${(count / 1_000_000).toFixed(1)}M`;
}

/** 时间戳（毫秒）→ HH:MM:SS 本地时刻标签。 */
export function formatClockLabel(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  const date = new Date(ms);
  return `${String(date.getHours()).padStart(2, "0")}:${String(
    date.getMinutes(),
  ).padStart(2, "0")}:${String(date.getSeconds()).padStart(2, "0")}`;
}

/** 运行状态的中文标签与语义色（tailwind class 由消费方组合）。 */
export function runStatusLabel(status: TrajectoryResponse["summary"]["status"]): {
  label: string;
  tone: "running" | "ok" | "error" | "muted";
} {
  switch (status) {
    case "running":
      return { label: "进行中", tone: "running" };
    case "completed":
      return { label: "完成", tone: "ok" };
    case "failed":
      return { label: "失败", tone: "error" };
    case "cancelled":
      return { label: "取消", tone: "muted" };
  }
}
