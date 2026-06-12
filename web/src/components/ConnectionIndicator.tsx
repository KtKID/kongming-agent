import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { SocketState } from "@/network/manager";

export type ConnectionState = SocketState;

interface ConnectionIndicatorProps {
  /** tooltip 文案（如 "对话连接" / "状态连接"） */
  label: string;
  /** WS 连接状态 */
  state: ConnectionState;
  /** ping/pong RTT（毫秒），null 时不显示数字 */
  latencyMs: number | null;
}

/**
 * 连接状态指示球：绿（正常 + RTT）/ 黄（重连中）/ 红（已断开）
 */
export function ConnectionIndicator({
  label,
  state,
  latencyMs,
}: ConnectionIndicatorProps) {
  const { dotClass, text } = stateDisplay(state, latencyMs);

  return (
    <TooltipProvider delayDuration={300}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            className="inline-flex items-center gap-1 text-[11px] tabular-nums text-muted-foreground select-none"
            data-testid="connection-indicator"
          >
            <span
              className={cn(
                "inline-block h-2 w-2 rounded-full shrink-0",
                dotClass,
              )}
            />
            <span>{text}</span>
          </span>
        </TooltipTrigger>
        <TooltipContent side="bottom">
          <span>{label}</span>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

function stateDisplay(
  state: ConnectionState,
  latencyMs: number | null,
): { dotClass: string; text: string } {
  switch (state) {
    case "open":
      return {
        dotClass: "bg-green-500",
        text: latencyMs != null ? `${latencyMs}ms` : "—",
      };
    case "connecting":
    case "reconnecting":
      return {
        dotClass: "bg-yellow-500 animate-pulse",
        text: "重连中…",
      };
    case "failed":
    case "closed":
      return {
        dotClass: "bg-red-500",
        text: "已断开",
      };
    default:
      return { dotClass: "bg-gray-400", text: "—" };
  }
}
