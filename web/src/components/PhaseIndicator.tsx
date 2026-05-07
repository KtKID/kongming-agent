import { cn } from "@/lib/utils";
import type { ThreadStatusPhase } from "@/protocol";

export function PhaseIndicator({ phase, toolName }: { phase?: ThreadStatusPhase; toolName?: string }) {
  if (!phase || phase === "idle") return null;

  if (phase === "complete") {
    return (
      <span className="shrink-0 text-[10px] leading-none text-green-500" aria-label="完成">
        ✓
      </span>
    );
  }

  if (phase === "error") {
    return (
      <span className="shrink-0 text-[10px] leading-none text-red-500" aria-label="错误">
        ✗
      </span>
    );
  }

  if (phase === "tool_calling") {
    return (
      <svg
        className="h-3 w-3 shrink-0 animate-pulse text-orange-500"
        viewBox="0 0 16 16"
        fill="currentColor"
        aria-label="调用工具"
      >
        <title>{toolName ? `调用工具：${toolName}` : "调用工具"}</title>
        <path d="M5.433 2.304A4.494 4.494 0 0 0 3.5 6c0 1.598.832 3.002 2.09 3.802l-.126.144a.5.5 0 0 0-.1.387l.8 4a.5.5 0 0 0 .49.4h2.691a.5.5 0 0 0 .49-.4l.8-4a.5.5 0 0 0-.1-.387l-.126-.144A4.497 4.497 0 0 0 12.5 6a4.494 4.494 0 0 0-1.933-3.696A4.5 4.5 0 0 0 5.433 2.304z" />
      </svg>
    );
  }

  const colorMap: Record<string, string> = {
    responding: "bg-green-500",
    thinking: "bg-purple-500",
    waiting_approval: "bg-yellow-500",
  };

  return (
    <span
      className={cn(
        "inline-block h-2 w-2 shrink-0 animate-pulse rounded-full",
        colorMap[phase],
      )}
      title={phase}
      aria-label={phase}
    />
  );
}
