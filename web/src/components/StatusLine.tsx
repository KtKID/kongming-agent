import { ArrowUp, ArrowDown, Sigma, Brain } from "lucide-react";
import { useChatStore } from "@/stores/chat";
import type { ReasoningEffort } from "@/components/Composer";

function fmt(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function Stat({
  icon: Icon,
  label,
  value,
}: {
  icon: React.ElementType;
  label: string;
  value: string;
}) {
  return (
    <span className="inline-flex items-center gap-1" title={label}>
      <Icon className="h-3 w-3" />
      <span>{value}</span>
    </span>
  );
}

interface StatusLineProps {
  threadId: string | undefined;
  reasoningEffort?: ReasoningEffort | null;
}

/**
 * 聊天底部状态栏 — 右侧紧凑布局，新 section 往左扩展。
 *
 * 当前 section：
 * - token 用量（prompt / completion / 累计）
 */
export function StatusLine({ threadId, reasoningEffort }: StatusLineProps) {
  const usage = useChatStore((s) =>
    threadId ? (s.usageByThread[threadId] ?? null) : null,
  );

  return (
    <div className="mx-auto flex max-w-3xl items-center justify-between px-0 py-1 text-[11px] tabular-nums text-muted-foreground">
      {reasoningEffort && (
        <span className="inline-flex items-center gap-1 text-primary">
          <Brain className="h-3 w-3" />
          深度思考 {reasoningEffort === "low" ? "低" : reasoningEffort === "medium" ? "中" : "高"}
        </span>
      )}
      {!reasoningEffort && <span />}
      <span className="inline-flex items-center gap-3">
      <Stat icon={ArrowUp} label="累计输入 tokens" value={fmt(usage?.cumulativePrompt ?? 0)} />
      <Stat icon={ArrowDown} label="累计输出 tokens" value={fmt(usage?.cumulativeCompletion ?? 0)} />
      <Stat icon={Sigma} label="累计 tokens" value={fmt(usage?.cumulativeTotal ?? 0)} />
      </span>
    </div>
  );
}
