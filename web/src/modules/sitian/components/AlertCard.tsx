/**
 * AlertCard —— TopAlert 卡片（纯渲染）
 *
 * 视觉规约：
 *   - priority badge：P0 destructive / P1 warning / P2 secondary
 *   - severity 小标签：high/medium/low
 *   - title 粗体、displayMessage 正文、recommendation 缩进块
 *   - sessionRef 一行小字（projectName + sessionId 前 8 位）
 *   - evidence 默认折叠，<details> 原生展开
 */

import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import type { AlertPriority, AlertSeverity, TopAlert } from "../types";

interface AlertCardProps {
  alert: TopAlert;
}

const PRIORITY_VARIANT: Record<
  AlertPriority,
  "destructive" | "warning" | "secondary"
> = {
  P0: "destructive",
  P1: "warning",
  P2: "secondary",
};

const SEVERITY_LABEL: Record<AlertSeverity, string> = {
  high: "高",
  medium: "中",
  low: "低",
};

const SEVERITY_CLASS: Record<AlertSeverity, string> = {
  high: "bg-destructive/10 text-destructive border-destructive/20",
  medium: "bg-warning/10 text-warning-foreground border-warning/30",
  low: "bg-muted text-muted-foreground border-border",
};

function shortSessionId(sessionId: string): string {
  return sessionId.length > 8 ? sessionId.slice(0, 8) : sessionId;
}

export function AlertCard({ alert }: AlertCardProps) {
  const priorityVariant = PRIORITY_VARIANT[alert.priority] ?? "secondary";

  return (
    <div className="rounded-md border border-border bg-card p-3 text-sm shadow-sm">
      {/* 行 1：badge + title */}
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Badge variant={priorityVariant} className="px-1.5 py-0 text-[10px]">
          {alert.priority}
        </Badge>
        <span
          className={cn(
            "inline-flex items-center rounded border px-1.5 py-0 text-[10px] font-medium",
            SEVERITY_CLASS[alert.severity] ?? SEVERITY_CLASS.low,
          )}
        >
          {SEVERITY_LABEL[alert.severity] ?? alert.severity}
        </span>
        <span className="font-semibold text-foreground">{alert.title}</span>
      </div>

      {/* 行 2：displayMessage */}
      {alert.displayMessage && (
        <p className="mb-2 whitespace-pre-wrap text-sm text-foreground/90">
          {alert.displayMessage}
        </p>
      )}

      {/* 行 3：recommendation 缩进 */}
      {alert.recommendation && (
        <div className="mb-2 rounded border-l-2 border-primary/40 bg-muted/40 px-3 py-1.5">
          <div className="mb-0.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            建议
          </div>
          <p className="whitespace-pre-wrap text-sm text-foreground/90">
            {alert.recommendation}
          </p>
        </div>
      )}

      {/* 行 4：severityReasons（可选） */}
      {alert.severityReasons && alert.severityReasons.length > 0 && (
        <ul className="mb-2 list-disc pl-5 text-xs text-muted-foreground">
          {alert.severityReasons.map((reason, idx) => (
            <li key={idx}>{reason}</li>
          ))}
        </ul>
      )}

      {/* 行 5：sessionRef */}
      <div className="mb-1 text-xs text-muted-foreground">
        <span>{alert.sessionRef.projectName}</span>
        {alert.sessionRef.sessionId && (
          <>
            <span className="mx-1">·</span>
            <span className="font-mono">
              {shortSessionId(alert.sessionRef.sessionId)}
            </span>
          </>
        )}
      </div>

      {/* 行 6：evidence 折叠 */}
      {alert.evidence && alert.evidence.length > 0 && (
        <details className="group mt-2 text-xs">
          <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
            证据 ({alert.evidence.length})
          </summary>
          <ul className="mt-1 space-y-1 pl-3">
            {alert.evidence.map((ev, idx) => (
              <li
                key={idx}
                className="border-l border-border pl-2 text-muted-foreground"
              >
                <span className="whitespace-pre-wrap">"{ev.quote}"</span>
                {ev.threadId && (
                  <span className="ml-1 font-mono text-[10px] opacity-70">
                    @{ev.threadId.slice(0, 8)}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
