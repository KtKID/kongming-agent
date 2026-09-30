/**
 * track-timeline-v0.1 · 轨迹账本列表（TrajectoryLedger）。
 *
 * 按轮次分组的记录列表：轮次头（轮次号 + 起止时间 + 折叠）+ 记录行
 * （#seq、角色图标配色、单行摘要、耗时、token、错误标红）。损坏占位
 * 记录保留序号与时间、不可展开。长列表用固定行高滚动窗口渲染
 * （仅渲染可视区 + 上下缓冲），数百条记录滚动不卡顿（spec feat02 场景5）。
 *
 * 选中行由 store 驱动（selectedSeq）；详情展示在底部固定 Inspector，
 * 不在列表内展开——保证虚拟滚动行高恒定。
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Bot,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  FileCog,
  User,
  Wrench,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { TrajectoryRecord } from "@/protocol";
import {
  buildLedgerRows,
  formatClockLabel,
  formatDurationLabel,
  formatTokensLabel,
  type TurnGroup,
} from "./derive";

/** 固定行高（px）：轮次头与记录行一致，虚拟滚动按此切片。 */
export const LEDGER_ROW_HEIGHT = 40;
/** 可视区上下各多渲染的缓冲行数。 */
const LEDGER_BUFFER_ROWS = 8;

const KIND_STYLE: Record<
  TrajectoryRecord["kind"],
  { icon: typeof User; className: string; label: string }
> = {
  system: { icon: FileCog, className: "text-emerald-400", label: "系统" },
  user_input: { icon: User, className: "text-sky-400", label: "用户" },
  llm: { icon: Bot, className: "text-violet-400", label: "模型" },
  tool: { icon: Wrench, className: "text-amber-400", label: "工具" },
  corrupted: { icon: CircleAlert, className: "text-zinc-500", label: "损坏" },
};

/** 记录行序号标签：trace 行号（#N）或非 trace 来源标记（sys / usr）。 */
function seqLabel(record: TrajectoryRecord): string {
  if (record.seq >= 0) return `#${record.seq}`;
  return record.kind === "system" ? "sys" : "usr";
}

function TurnHeader({
  group,
  collapsed,
  toolsHidden,
  onToggle,
}: {
  group: TurnGroup;
  collapsed: boolean;
  toolsHidden: boolean;
  onToggle: () => void;
}) {
  const toolCount = group.records.filter((r) => r.kind === "tool").length;
  const Icon = collapsed ? ChevronRight : ChevronDown;
  return (
    <div
      className="flex h-full items-center gap-1.5 border-y border-border/40 bg-muted/40 px-2 text-xs text-muted-foreground"
      data-testid={`trajectory-turn-header-${group.key}`}
    >
      <button
        type="button"
        onClick={onToggle}
        className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
        aria-expanded={!collapsed}
      >
        <Icon className="h-3.5 w-3.5 shrink-0" />
        <span className="shrink-0 font-medium text-foreground/80">
          {group.runIndex === 0
            ? "会话信息"
            : `运行 ${group.runIndex} · 轮次 ${group.turn ?? "—"}`}
        </span>
        <span className="shrink-0">
          {formatClockLabel(group.startedAtMs)}–{formatClockLabel(group.endedAtMs)}
        </span>
        {collapsed ? (
          <span className="shrink-0 text-muted-foreground/80">
            （{group.records.length} 条记录 · {toolCount} 次工具调用）
          </span>
        ) : null}
      </button>
      {!collapsed && toolsHidden && toolCount > 0 ? (
        <span className="shrink-0 text-muted-foreground/80">
          工具调用已收起（{toolCount}）
        </span>
      ) : null}
    </div>
  );
}

function RecordRow({
  record,
  selected,
  query,
  onSelect,
  hideToolDetail,
}: {
  record: TrajectoryRecord;
  selected: boolean;
  query: string;
  onSelect: () => void;
  hideToolDetail: boolean;
}) {
  const style = KIND_STYLE[record.kind];
  const Icon = record.ok === false ? AlertTriangle : style.icon;
  const summary = record.ok === false && record.error_message
    ? `${record.summary} · ${record.error_message}`
    : record.summary;
  return (
    <button
      type="button"
      onClick={onSelect}
      data-testid={`trajectory-record-${record.seq}`}
      className={cn(
        "flex h-full w-full items-center gap-2 px-2 text-left text-xs hover:bg-muted/50",
        selected && "bg-primary/10 ring-1 ring-inset ring-primary/40",
        record.ok === false && "text-red-400",
        record.ok === false && "bg-red-500/5",
      )}
    >
      <span className="w-10 shrink-0 truncate font-mono text-[10px] text-muted-foreground">
        {seqLabel(record)}
      </span>
      <Icon className={cn("h-3.5 w-3.5 shrink-0", style.className)} />
      <span className="w-8 shrink-0 text-[10px] text-muted-foreground">{style.label}</span>
      <span className="min-w-0 flex-1 truncate">
        <HighlightedText text={summary} query={query} />
      </span>
      {record.kind === "llm" && record.tokens?.output_total != null ? (
        <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
          {formatTokensLabel(record.tokens.input_total)}→{formatTokensLabel(record.tokens.output_total)}
        </span>
      ) : null}
      <span className="w-14 shrink-0 text-right font-mono text-[10px] text-muted-foreground">
        {formatDurationLabel(record.duration_ms)}
      </span>
      {hideToolDetail && record.kind === "tool" ? null : null}
    </button>
  );
}

/** 命中关键词高亮（大小写不敏感；空关键词原样返回）。 */
export function HighlightedText({
  text,
  query,
}: {
  text: string;
  query: string;
}) {
  const q = query.trim();
  if (!q) return <>{text}</>;
  const lower = text.toLowerCase();
  const target = q.toLowerCase();
  const parts: Array<{ text: string; hit: boolean }> = [];
  let cursor = 0;
  let idx = lower.indexOf(target, cursor);
  while (idx >= 0) {
    if (idx > cursor) parts.push({ text: text.slice(cursor, idx), hit: false });
    parts.push({ text: text.slice(idx, idx + q.length), hit: true });
    cursor = idx + q.length;
    idx = lower.indexOf(target, cursor);
  }
  if (cursor < text.length) parts.push({ text: text.slice(cursor), hit: false });
  return (
    <>
      {parts.map((part, i) =>
        part.hit ? (
          <mark key={i} className="rounded bg-yellow-500/30 px-0.5 text-inherit">
            {part.text}
          </mark>
        ) : (
          <span key={i}>{part.text}</span>
        ),
      )}
    </>
  );
}

export interface TrajectoryLedgerProps {
  groups: TurnGroup[];
  collapsedKeys: ReadonlySet<string>;
  toolsCollapsed: boolean;
  selectedSeq: number | undefined;
  query: string;
  onSelect: (seq: number) => void;
  onToggleTurn: (key: string) => void;
}

export function TrajectoryLedger({
  groups,
  collapsedKeys,
  toolsCollapsed,
  selectedSeq,
  query,
  onSelect,
  onToggleTurn,
}: TrajectoryLedgerProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportHeight, setViewportHeight] = useState(480);

  // 「调用」收起时隐藏工具记录行（轮次头保留计数摘要）
  const visibleGroups = useMemo(() => {
    if (!toolsCollapsed) return groups;
    return groups.map((group) => ({
      ...group,
      records: group.records.filter((r) => r.kind !== "tool"),
    }));
  }, [groups, toolsCollapsed]);

  const rows = useMemo(
    () => buildLedgerRows(visibleGroups, collapsedKeys),
    [visibleGroups, collapsedKeys],
  );

  // 选中变化时把目标行滚进可视区（时间线 / 搜索联动定位）
  useEffect(() => {
    if (selectedSeq === undefined || !scrollRef.current) return;
    const rowIndex = rows.findIndex(
      (row) => row.type === "record" && row.record?.seq === selectedSeq,
    );
    if (rowIndex < 0) return;
    const targetTop = rowIndex * LEDGER_ROW_HEIGHT;
    const current = scrollRef.current;
    if (
      targetTop < current.scrollTop ||
      targetTop + LEDGER_ROW_HEIGHT > current.scrollTop + current.clientHeight
    ) {
      current.scrollTo({
        top: Math.max(0, targetTop - current.clientHeight / 2),
        behavior: "smooth",
      });
    }
  }, [selectedSeq, rows]);

  const totalHeight = rows.length * LEDGER_ROW_HEIGHT;
  const firstVisible = Math.max(
    0,
    Math.floor(scrollTop / LEDGER_ROW_HEIGHT) - LEDGER_BUFFER_ROWS,
  );
  const visibleCount =
    Math.ceil(viewportHeight / LEDGER_ROW_HEIGHT) + LEDGER_BUFFER_ROWS * 2;
  const slice = rows.slice(firstVisible, firstVisible + visibleCount);

  return (
    <div
      ref={scrollRef}
      data-testid="trajectory-ledger"
      className="min-h-0 flex-1 overflow-y-auto"
      onScroll={(event) => {
        const el = event.currentTarget;
        setScrollTop(el.scrollTop);
        setViewportHeight(el.clientHeight || 480);
      }}
    >
      <div style={{ height: totalHeight, position: "relative" }}>
        <div
          style={{
            position: "absolute",
            top: firstVisible * LEDGER_ROW_HEIGHT,
            left: 0,
            right: 0,
          }}
        >
          {slice.map((row) =>
            row.type === "turn-header" && row.group ? (
              <div
                key={row.key}
                style={{ height: LEDGER_ROW_HEIGHT }}
                className="border-b border-border/30"
              >
                <TurnHeader
                  group={row.group}
                  collapsed={collapsedKeys.has(row.group.key)}
                  toolsHidden={toolsCollapsed}
                  onToggle={() => onToggleTurn(row.group!.key)}
                />
              </div>
            ) : row.record ? (
              <div
                key={row.key}
                style={{ height: LEDGER_ROW_HEIGHT }}
                className="border-b border-border/20"
              >
                <RecordRow
                  record={row.record}
                  selected={row.record.seq === selectedSeq}
                  query={query}
                  onSelect={() => onSelect(row.record!.seq)}
                  hideToolDetail={toolsCollapsed}
                />
              </div>
            ) : null,
          )}
        </div>
      </div>
    </div>
  );
}
