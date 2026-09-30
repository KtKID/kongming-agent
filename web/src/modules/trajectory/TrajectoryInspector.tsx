/**
 * track-timeline-v0.1 · 轨迹详情面板（TrajectoryInspector）。
 *
 * 展示选中记录的完整详情：工具（名称 / 完整入参 / 完整输出 / 错误 / 起止
 * 耗时）、模型（完整回复 / 思考 / token 明细含缓存 / 首字耗时 / 归属）、
 * 用户输入全文。超长内容默认截断（2000 字符）+「展开全部」（spec feat04
 * 场景4）；损坏占位记录展示不可展开的说明（feat01 场景3）。
 *
 * 面板固定在轨迹视图底部（自身滚动），不进账本虚拟列表——保证列表行高恒定。
 */

import { useState } from "react";
import { ChevronUp } from "lucide-react";
import { cn } from "@/lib/utils";
import type { TrajectoryRecord } from "@/protocol";
import {
  formatClockLabel,
  formatDurationLabel,
  formatTokensLabel,
} from "./derive";

/** 超长文本默认截断长度（spec 确认默认 2000 字符）。 */
const DETAIL_TRUNCATE_CHARS = 2000;

function DetailBlock({
  label,
  content,
  mono = false,
}: {
  label: string;
  content: string;
  mono?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const overflow = content.length > DETAIL_TRUNCATE_CHARS;
  const visible = overflow && !expanded
    ? content.slice(0, DETAIL_TRUNCATE_CHARS)
    : content;
  return (
    <section className="space-y-1" data-testid={`trajectory-detail-${label}`}>
      <div className="flex items-center justify-between">
        <h4 className="text-[11px] font-medium text-muted-foreground">{label}</h4>
        {overflow ? (
          <button
            type="button"
            className="text-[11px] text-primary hover:underline"
            onClick={() => setExpanded((v) => !v)}
            data-testid={`trajectory-detail-expand-${label}`}
          >
            {expanded ? "收起" : `展开全部（${content.length} 字符）`}
          </button>
        ) : null}
      </div>
      <pre
        className={cn(
          "max-h-64 overflow-auto whitespace-pre-wrap break-all rounded border border-border/40 bg-muted/30 p-2 text-[11px] leading-relaxed",
          mono && "font-mono",
        )}
      >
        {visible}
      </pre>
    </section>
  );
}

function MetaItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-1 text-[11px]">
      <span className="shrink-0 text-muted-foreground">{label}</span>
      <span className="font-mono">{value}</span>
    </div>
  );
}

function LlmDetail({ record }: { record: TrajectoryRecord }) {
  const t = record.tokens;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        <MetaItem label="首字耗时" value={formatDurationLabel(record.ttft_ms)} />
        <MetaItem label="总耗时" value={formatDurationLabel(record.duration_ms)} />
        <MetaItem label="结束原因" value={record.finish_reason ?? "—"} />
        <MetaItem
          label="输入 / 输出"
          value={`${formatTokensLabel(t?.input_total)} / ${formatTokensLabel(t?.output_total)}`}
        />
        <MetaItem label="思考" value={formatTokensLabel(t?.reasoning)} />
        <MetaItem
          label="缓存读 / 写"
          value={`${formatTokensLabel(t?.cache_read)} / ${formatTokensLabel(t?.cache_write)}`}
        />
        <MetaItem label="轮次" value={`${record.run_index}:${record.turn ?? "—"}`} />
      </div>
      {record.output_detail ? (
        <DetailBlock label="回复全文" content={record.output_detail} />
      ) : null}
      {record.reasoning_detail ? (
        <DetailBlock label="思考内容" content={record.reasoning_detail} />
      ) : null}
    </div>
  );
}

function ToolDetail({ record }: { record: TrajectoryRecord }) {
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        <MetaItem label="开始" value={formatClockLabel(record.started_at_ms)} />
        <MetaItem label="耗时" value={formatDurationLabel(record.duration_ms)} />
        <MetaItem label="轮次" value={`${record.run_index}:${record.turn ?? "—"}`} />
        <MetaItem
          label="状态"
          value={record.ok === false ? "失败" : record.ok === true ? "成功" : "未知"}
        />
      </div>
      {record.input_detail ? (
        <DetailBlock label="输入参数" content={record.input_detail} mono />
      ) : null}
      {record.output_detail ? (
        <DetailBlock label="输出" content={record.output_detail} />
      ) : null}
      {record.error_message ? (
        <DetailBlock label="错误信息" content={record.error_message} mono />
      ) : null}
    </div>
  );
}

export interface TrajectoryInspectorProps {
  record: TrajectoryRecord | undefined;
  onClose: () => void;
}

export function TrajectoryInspector({ record, onClose }: TrajectoryInspectorProps) {
  if (!record) return null;
  return (
    <div
      data-testid="trajectory-inspector"
      className="max-h-56 shrink-0 space-y-2 overflow-y-auto border-t border-border/60 bg-card/60 p-2"
    >
      <div className="flex items-center justify-between">
        <h3 className="flex items-center gap-2 text-xs font-medium">
          <span className="font-mono text-muted-foreground">#{record.seq}</span>
          <span>
            {record.kind === "system"
              ? "系统提示词"
              : record.kind === "tool"
                ? `工具 · ${record.tool_name ?? "未知"}`
                : record.kind === "llm"
                  ? "模型回复"
                  : record.kind === "user_input"
                    ? "用户输入"
                    : "记录损坏"}
          </span>
        </h3>
        <button
          type="button"
          onClick={onClose}
          className="flex items-center gap-0.5 text-[11px] text-muted-foreground hover:text-foreground"
          data-testid="trajectory-inspector-close"
        >
          <ChevronUp className="h-3.5 w-3.5" />
          收起
        </button>
      </div>
      {record.kind === "system" && record.input_detail ? (
        <DetailBlock label="系统提示词全文" content={record.input_detail} />
      ) : null}
      {record.kind === "user_input" && record.input_detail ? (
        <DetailBlock label="用户输入" content={record.input_detail} />
      ) : null}
      {record.kind === "llm" ? <LlmDetail record={record} /> : null}
      {record.kind === "tool" ? <ToolDetail record={record} /> : null}
      {record.kind === "corrupted" ? (
        <p className="text-[11px] text-muted-foreground">
          该行 trace 数据损坏（无法解析），已按原位置占位保留；内容不可展开。
        </p>
      ) : null}
    </div>
  );
}
