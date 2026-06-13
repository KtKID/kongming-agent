/**
 * LogContentPane renders formatted log rows.
 *
 * JSON rows can expand to show pretty JSON. Traceback rows keep preformatted
 * text and use an error-colored frame.
 */

import { useState, useCallback } from "react";
import { ChevronRight, ChevronDown } from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { LogLineViewModel, LogLevel } from "../formatter";
import type { LogFormat } from "../types";

interface LogContentPaneProps {
  lines: LogLineViewModel[];
  format: LogFormat;
}

const LEVEL_STYLES: Record<LogLevel, string> = {
  error: "bg-destructive/15 text-destructive border-destructive/30",
  warn:
    "bg-amber-500/15 text-amber-600 border-amber-500/30 dark:text-amber-400",
  info: "bg-muted text-muted-foreground border-border",
  debug: "bg-muted/50 text-muted-foreground/70 border-border/50",
  unknown: "bg-muted/30 text-muted-foreground/50 border-transparent",
};

function LevelBadge({ level }: { level: LogLevel }) {
  const cls = LEVEL_STYLES[level] ?? LEVEL_STYLES.unknown;
  return (
    <span
      className={`inline-flex items-center rounded px-1 py-px text-[10px] font-medium leading-none border ${cls}`}
    >
      {level.toUpperCase()}
    </span>
  );
}

interface LineRowProps {
  model: LogLineViewModel;
}

function LineRow({ model }: LineRowProps) {
  const [expanded, setExpanded] = useState(false);
  const toggle = useCallback(() => setExpanded((v) => !v), []);

  const isExpandable = model.kind === "json" && !!model.prettyJson;

  if (model.kind === "traceback") {
    return (
      <div className="rounded border border-destructive/30 bg-destructive/5 px-2 py-1.5 text-xs">
        <pre className="whitespace-pre-wrap font-mono text-destructive/90 leading-relaxed">
          {model.raw}
        </pre>
      </div>
    );
  }

  return (
    <div>
      <button
        type="button"
        onClick={isExpandable ? toggle : undefined}
        className={`flex w-full items-start gap-1.5 px-1 py-0.5 text-left ${
          isExpandable
            ? "cursor-pointer hover:bg-muted/40 rounded-sm"
            : "cursor-default"
        }`}
      >
        {isExpandable && (
          <span className="mt-0.5 flex-shrink-0 text-muted-foreground/60">
            {expanded ? (
              <ChevronDown className="h-3 w-3" />
            ) : (
              <ChevronRight className="h-3 w-3" />
            )}
          </span>
        )}

        {model.time && (
          <span className="flex-shrink-0 font-mono text-[10px] leading-4 text-muted-foreground/70 tabular-nums">
            {model.time}
          </span>
        )}

        {model.level && <LevelBadge level={model.level} />}

        {model.badges.length > 0 && (
          <span className="flex flex-shrink-0 gap-1">
            {model.badges.map((b, i) => (
              <span
                key={i}
                className="inline-flex rounded bg-secondary px-1 py-px text-[9px] leading-none text-secondary-foreground"
              >
                {b}
              </span>
            ))}
          </span>
        )}

        <span className="min-w-0 flex-1 truncate text-xs leading-4 text-foreground/90">
          {model.summary}
        </span>
      </button>

      {expanded && model.prettyJson && (
        <pre className="mx-1 mt-0.5 mb-1 max-h-64 overflow-auto rounded bg-muted/60 px-2 py-1.5 font-mono text-[11px] leading-relaxed text-foreground/80">
          {model.prettyJson}
        </pre>
      )}
    </div>
  );
}

export function LogContentPane({ lines, format: _format }: LogContentPaneProps) {
  if (lines.length === 0) {
    return (
      <div className="flex items-center justify-center py-16 text-sm text-muted-foreground">
        日志文件为空
      </div>
    );
  }

  return (
    <ScrollArea className="h-full">
      <div className="flex flex-col gap-0.5 px-1 py-1">
        {lines.map((model) => (
          <LineRow key={model.key} model={model} />
        ))}
      </div>
    </ScrollArea>
  );
}
