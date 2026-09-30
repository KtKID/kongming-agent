/**
 * track-timeline-v0.1 · 轨迹时间线总览（TrajectoryTimeline，时长视角）。
 *
 * 三泳道（输入 / 模型 / 工具）横向分段：每条记录一段横条，条长按该条
 * 耗时占全局最大耗时的比例绘制（无耗时 → 最小占位宽）；失败调用红色、
 * 损坏占位灰色；轮次切换处画分界线。点击横条 → 选中该记录（联动账本
 * 滚动定位与详情面板，spec feat03 场景3）。
 *
 * 待做视角（v0.2，spec feat03「待做」）：序列（等宽）、实际时间（真实
 * 时间轴）——数据已具备 started_at_ms / duration_ms，届时只加投影函数。
 */

import { useMemo } from "react";
import { cn } from "@/lib/utils";
import type { TrajectoryRecord } from "@/protocol";
import { formatDurationLabel, laneForKind } from "./derive";

/** 无耗时记录的最小宽度占比（%），保证可见可点。 */
const MIN_WIDTH_PERCENT = 2;
const LANE_LABELS = ["输入", "模型", "工具"] as const;

const KIND_BAR_CLASS: Record<TrajectoryRecord["kind"], string> = {
  system: "bg-emerald-500/70",
  user_input: "bg-sky-500/70",
  llm: "bg-violet-500/70",
  tool: "bg-amber-500/70",
  corrupted: "bg-zinc-600/60",
};

interface TimelineSegment {
  record: TrajectoryRecord;
  widthPercent: number;
  /** 轮次分界：该段是其轮次内的第一段时为真（渲染左侧分界线）。 */
  turnStart: boolean;
}

export interface TrajectoryTimelineProps {
  records: TrajectoryRecord[];
  selectedSeq: number | undefined;
  onSelect: (seq: number) => void;
}

export function TrajectoryTimeline({
  records,
  selectedSeq,
  onSelect,
}: TrajectoryTimelineProps) {
  const lanes = useMemo(() => {
    const maxDuration = Math.max(
      1,
      ...records.map((r) => r.duration_ms ?? 0),
    );
    const byLane: TimelineSegment[][] = [[], [], []];
    let lastTurnKey = "";
    for (const record of records) {
      const turnKey = `${record.run_index}:${record.turn ?? 0}`;
      const turnStart = turnKey !== lastTurnKey;
      lastTurnKey = turnKey;
      const ratio = (record.duration_ms ?? 0) / maxDuration;
      byLane[laneForKind(record.kind)].push({
        record,
        widthPercent: Math.max(MIN_WIDTH_PERCENT, ratio * 100),
        turnStart,
      });
    }
    return byLane;
  }, [records]);

  if (records.length === 0) return null;

  return (
    <div
      data-testid="trajectory-timeline"
      className="space-y-1 rounded-md border border-border/50 bg-muted/20 p-2"
    >
      <div className="flex items-center justify-between text-[10px] text-muted-foreground">
        <span>时间线 · 时长视角</span>
        <span>条长 ∝ 记录耗时</span>
      </div>
      {lanes.map((segments, laneIndex) => (
        <div key={laneIndex} className="flex items-center gap-1.5">
          <span className="w-8 shrink-0 text-[10px] text-muted-foreground">
            {LANE_LABELS[laneIndex]}
          </span>
          <div className="flex h-5 min-w-0 flex-1 items-stretch gap-px overflow-hidden">
            {segments.length === 0 ? (
              <div className="flex-1 border-y border-dashed border-border/40" />
            ) : (
              segments.map(({ record, widthPercent, turnStart }) => (
                <button
                  key={record.seq}
                  type="button"
                  title={`#${record.seq} ${record.tool_name ?? ""} ${formatDurationLabel(
                    record.duration_ms,
                  )}`}
                  onClick={() => onSelect(record.seq)}
                  data-testid={`trajectory-timeline-seg-${record.seq}`}
                  style={{ width: `${widthPercent}%` }}
                  className={cn(
                    "h-full min-w-[4px] rounded-sm transition-opacity hover:opacity-80",
                    KIND_BAR_CLASS[record.kind],
                    record.ok === false && "bg-red-500/80",
                    record.seq === selectedSeq && "ring-2 ring-primary ring-offset-1",
                    turnStart && "border-l-2 border-l-border",
                  )}
                />
              ))
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
