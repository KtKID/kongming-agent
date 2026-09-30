/**
 * track-timeline-v0.1 · 轨迹面板（TrajectoryPanel，模块根组件）。
 *
 * 组合：总览统计（状态 / 总时长 / 轮次 / 调用数 / token / 损坏提示）+
 * 运行分段条（feat06 场景2）+ 时间线总览（feat03）+ 搜索（feat05）+
 * 账本列表（feat02）+ 底部详情面板（feat04）。
 *
 * 状态：加载中 / 空态（feat01 场景2）/ 会话不存在错误态 + 返回列表入口
 * （feat01 场景5）/ 进行中标注 + 手动刷新（feat01 场景4，v0.1 不自动追加）。
 */

import { useEffect, useMemo } from "react";
import { Link } from "react-router-dom";
import { RefreshCw, Search, UnfoldVertical } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  buildTurnGroups,
  filterRecords,
  formatDurationLabel,
  formatTokensLabel,
  runStatusLabel,
} from "./derive";
import { trajectoryManager } from "./TrajectoryManager";
import { TrajectoryInspector } from "./TrajectoryInspector";
import { TrajectoryLedger } from "./TrajectoryLedger";
import { useTrajectoryStore } from "./TrajectoryStore";
import { TrajectoryTimeline } from "./TrajectoryTimeline";

function StatusBadge({ status }: { status: "running" | "completed" | "failed" | "cancelled" }) {
  const { label, tone } = runStatusLabel(status);
  return (
    <span
      data-testid="trajectory-status-badge"
      className={cn(
        "rounded px-1.5 py-0.5 text-[10px] font-medium",
        tone === "running" && "animate-pulse bg-blue-500/20 text-blue-300",
        tone === "ok" && "bg-emerald-500/20 text-emerald-300",
        tone === "error" && "bg-red-500/20 text-red-300",
        tone === "muted" && "bg-zinc-500/20 text-zinc-300",
      )}
    >
      {label}
    </span>
  );
}

/** 稳定空数组：zustand v5 selector 不允许每次返回新引用（会无限重渲染）。 */
const EMPTY_COLLAPSED_KEYS: readonly string[] = [];

export interface TrajectoryPanelProps {
  threadId: string;
}

export function TrajectoryPanel({ threadId }: TrajectoryPanelProps) {
  const data = useTrajectoryStore((s) => s.dataByThread[threadId]);
  const loading = useTrajectoryStore((s) => Boolean(s.loadingByThread[threadId]));
  const error = useTrajectoryStore((s) => s.errorByThread[threadId]);
  const selectedSeq = useTrajectoryStore((s) => s.selectedSeqByThread[threadId]);
  const search = useTrajectoryStore((s) => s.searchByThread[threadId] ?? "");
  const collapsedTurns =
    useTrajectoryStore((s) => s.collapsedTurnsByThread[threadId]) ??
    EMPTY_COLLAPSED_KEYS;
  const toolsCollapsed = useTrajectoryStore(
    (s) => Boolean(s.toolsCollapsedByThread[threadId]),
  );
  const select = useTrajectoryStore((s) => s.select);
  const setSearch = useTrajectoryStore((s) => s.setSearch);
  const toggleTurn = useTrajectoryStore((s) => s.toggleTurnCollapsed);
  const setAllTurns = useTrajectoryStore((s) => s.setAllTurnsCollapsed);
  const setToolsCollapsed = useTrajectoryStore((s) => s.setToolsCollapsed);

  // 挂载 / 切 thread 时加载一次；刷新按钮手动触发（v0.1 无实时推送）
  useEffect(() => {
    void trajectoryManager.load(threadId);
  }, [threadId]);

  const filteredRecords = useMemo(
    () => (data ? filterRecords(data.records, search) : []),
    [data, search],
  );
  const groups = useMemo(() => buildTurnGroups(filteredRecords), [filteredRecords]);
  const collapsedKeys = useMemo(() => new Set(collapsedTurns), [collapsedTurns]);
  const selectedRecord = useMemo(
    () => data?.records.find((r) => r.seq === selectedSeq),
    [data, selectedSeq],
  );

  if (loading && !data) {
    return (
      <div className="p-4 text-xs text-muted-foreground" data-testid="trajectory-loading">
        正在加载轨迹…
      </div>
    );
  }

  if (error === "not_found") {
    return (
      <div className="space-y-2 p-4 text-xs" data-testid="trajectory-not-found">
        <p className="text-red-400">会话不存在或已删除。</p>
        <Link to="/" className="text-primary hover:underline">
          返回会话列表
        </Link>
      </div>
    );
  }

  if (error === "network") {
    return (
      <div className="space-y-2 p-4 text-xs" data-testid="trajectory-network-error">
        <p className="text-red-400">轨迹加载失败，请稍后重试。</p>
        <button
          type="button"
          className="text-primary hover:underline"
          onClick={() => void trajectoryManager.load(threadId)}
        >
          重新加载
        </button>
      </div>
    );
  }

  if (!data || (data.records.length === 0 && data.runs.length === 0)) {
    return (
      <div className="space-y-2 p-4 text-xs text-muted-foreground" data-testid="trajectory-empty">
        <p>暂无轨迹数据</p>
        <p>回到对话页发送第一条消息，跑完后即可回放完整轨迹。</p>
      </div>
    );
  }

  const summary = data.summary;
  const hasFilter = search.trim().length > 0;
  const allCollapsed = collapsedTurns.length > 0;

  return (
    <div className="flex h-full min-h-0 flex-col gap-2 p-2">
      {/* 总览统计（feat06 场景1）+ 损坏提示（feat01 场景3）+ 刷新（场景4） */}
      <div className="space-y-1.5" data-testid="trajectory-summary">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
          <StatusBadge status={summary.status} />
          <span className="font-mono">{formatDurationLabel(summary.total_duration_ms)}</span>
          <span>{summary.turn_count} 轮</span>
          <span>{summary.tool_call_count} 次调用</span>
          <span className="font-mono text-muted-foreground">
            {formatTokensLabel(summary.tokens.input_total)}→
            {formatTokensLabel(summary.tokens.output_total)} tok
          </span>
          <button
            type="button"
            title="刷新"
            onClick={() => void trajectoryManager.load(threadId)}
            className="ml-auto text-muted-foreground hover:text-foreground"
            data-testid="trajectory-refresh"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
          </button>
        </div>
        {summary.corrupted_count > 0 ? (
          <p className="rounded bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-300">
            轨迹数据不完整：{summary.corrupted_count} 条记录损坏，已占位保留。
          </p>
        ) : null}
        {/* 运行分段（feat06 场景2） */}
        {data.runs.length > 0 ? (
          <div className="flex flex-wrap gap-1" data-testid="trajectory-runs">
            {data.runs.map((run) => {
              const { label, tone } = runStatusLabel(run.status);
              return (
                <span
                  key={run.run_index}
                  title={`运行 ${run.run_index} · ${run.run_id}`}
                  className={cn(
                    "rounded border border-border/50 px-1.5 py-0.5 text-[10px]",
                    tone === "running" && "text-blue-300",
                    tone === "ok" && "text-emerald-300",
                    tone === "error" && "text-red-300",
                    tone === "muted" && "text-zinc-300",
                  )}
                >
                  #{run.run_index} {label} · {formatDurationLabel(run.duration_ms)} ·{" "}
                  {run.turn_count} 轮
                </span>
              );
            })}
          </div>
        ) : null}
      </div>

      {/* 时间线总览（feat03：时长视角 + 三泳道 + 点选联动） */}
      <TrajectoryTimeline
        records={filteredRecords}
        selectedSeq={selectedSeq}
        onSelect={(seq) => select(threadId, seq)}
      />

      {/* 搜索 + 轮次/调用折叠控制（feat02 场景2/3、feat05） */}
      <div className="flex items-center gap-1.5">
        <div className="relative min-w-0 flex-1">
          <Search className="absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-muted-foreground" />
          <input
            value={search}
            onChange={(e) => setSearch(threadId, e.target.value)}
            placeholder="搜索轨迹（摘要 / 工具名 / 入参 / 输出 / 错误）"
            data-testid="trajectory-search-input"
            className="h-7 w-full rounded border border-border/60 bg-background/60 pl-6 pr-2 text-xs outline-none focus:border-primary/60"
          />
        </div>
        <button
          type="button"
          onClick={() => setAllTurns(threadId, !allCollapsed)}
          className="h-7 shrink-0 rounded border border-border/60 px-1.5 text-[10px] text-muted-foreground hover:text-foreground"
          data-testid="trajectory-toggle-turns"
        >
          轮次
        </button>
        <button
          type="button"
          onClick={() => setToolsCollapsed(threadId, !toolsCollapsed)}
          title={toolsCollapsed ? "展开所有工具调用" : "收起所有工具调用"}
          className={cn(
            "flex h-7 shrink-0 items-center gap-0.5 rounded border border-border/60 px-1.5 text-[10px]",
            toolsCollapsed ? "text-muted-foreground" : "text-foreground",
          )}
          data-testid="trajectory-toggle-tools"
        >
          <UnfoldVertical className="h-3 w-3" />
          调用
        </button>
      </div>

      {hasFilter && filteredRecords.length === 0 ? (
        <p
          className="px-1 text-[11px] text-muted-foreground"
          data-testid="trajectory-search-empty"
        >
          没有匹配的记录
        </p>
      ) : null}

      {/* 账本 + 详情 */}
      <TrajectoryLedger
        groups={groups}
        collapsedKeys={collapsedKeys}
        toolsCollapsed={toolsCollapsed}
        selectedSeq={selectedSeq}
        query={search}
        onSelect={(seq) => select(threadId, seq)}
        onToggleTurn={(key) => toggleTurn(threadId, key)}
      />
      <TrajectoryInspector
        record={selectedRecord}
        onClose={() => select(threadId, undefined)}
      />
    </div>
  );
}
