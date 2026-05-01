import { ArrowUp, ArrowDown, Sigma } from "lucide-react";
import { useChatStore } from "@/stores/chat";

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
}

/**
 * 聊天底部状态栏 — 右侧紧凑布局，新 section 往左扩展。
 *
 * 当前 section：
 * - token 用量（prompt / completion / 累计）
 */
export function StatusLine({ threadId }: StatusLineProps) {
  const usage = useChatStore((s) =>
    threadId ? (s.usageByThread[threadId] ?? null) : null,
  );

  return (
    <div className="mx-auto flex max-w-3xl items-center justify-end gap-3 px-0 py-1 text-[11px] tabular-nums text-muted-foreground">
      <Stat icon={ArrowUp} label="Prompt tokens" value={fmt(usage?.lastPrompt ?? 0)} />
      <Stat icon={ArrowDown} label="Completion tokens" value={fmt(usage?.lastCompletion ?? 0)} />
      <Stat icon={Sigma} label="累计 tokens" value={fmt(usage?.cumulativeTotal ?? 0)} />
    </div>
  );
}
