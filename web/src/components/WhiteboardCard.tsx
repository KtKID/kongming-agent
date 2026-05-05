import { useEffect, useState } from "react";
import { GripHorizontal } from "lucide-react";
import { WhiteboardCardEditor } from "@/components/WhiteboardCardEditor";
import { WhiteboardCardHeader } from "@/components/WhiteboardCardHeader";
import { WhiteboardCardPreview } from "@/components/WhiteboardCardPreview";
import { toggleTaskAtIndex } from "@/lib/whiteboard-markdown";
import { cn } from "@/lib/utils";

export interface WhiteboardCardItem {
  id: string;
  title: string;
  category: string;
  content: string;
  collapsed: boolean;
  mode?: "editor" | "preview";
  x?: number;
  y?: number;
  zIndex?: number;
  height?: number;
  updatedLabel?: string;
}

interface WhiteboardCardProps {
  card: WhiteboardCardItem;
  onDragPointerDown?: React.PointerEventHandler<HTMLDivElement>;
  onResizePointerDown?: React.PointerEventHandler<HTMLButtonElement>;
  onToggleCollapse?: (cardId: string) => void;
  onDeleteCard?: (cardId: string) => void;
  onUpdateCard?: (cardId: string, patch: Partial<WhiteboardCardItem>) => void;
}

export function WhiteboardCard({
  card,
  onDragPointerDown,
  onResizePointerDown,
  onToggleCollapse,
  onDeleteCard,
  onUpdateCard,
}: WhiteboardCardProps) {
  const [mode, setMode] = useState<"editor" | "preview">(card.mode ?? "preview");
  const cardHeight = Math.max(card.height ?? 280, 220);
  const summary = card.content.split("\n").find((line) => line.trim().length > 0) ?? "空白卡片";
  const tone = getCardTone(card.category);

  useEffect(() => {
    setMode(card.mode ?? "preview");
  }, [card.id, card.mode]);

  return (
    <article
      className={cn(
        "group overflow-hidden rounded-[1.35rem] border shadow-sm backdrop-blur-sm transition-shadow",
        "hover:shadow-md dark:hover:shadow-black/20",
        tone.shellClassName,
      )}
      style={card.collapsed ? undefined : { height: cardHeight }}
    >
      <div className="flex h-full flex-col">
        <WhiteboardCardHeader
          title={card.title}
          category={card.category}
          mode={mode}
          collapsed={card.collapsed}
          updatedLabel={card.updatedLabel}
          categoryClassName={tone.categoryClassName}
          toolbarClassName={tone.toolbarClassName}
          onDragPointerDown={onDragPointerDown}
          onToggleCollapse={() => onToggleCollapse?.(card.id)}
          onDelete={() => onDeleteCard?.(card.id)}
          onTitleChange={(title) => onUpdateCard?.(card.id, { title })}
          onCategoryChange={(category) => onUpdateCard?.(card.id, { category })}
          onModeChange={(nextMode) => {
            setMode(nextMode);
            onUpdateCard?.(card.id, { mode: nextMode });
          }}
        />
        {card.collapsed ? (
          <div className={cn("border-t px-4 py-3 text-xs text-muted-foreground", tone.summaryClassName)}>
            <div className="truncate">{summary}</div>
          </div>
        ) : (
          <div className={cn("flex min-h-0 flex-1 flex-col border-t", tone.bodyClassName)}>
            <div className="min-h-0 flex-1 px-4 py-3">
              {mode === "editor" ? (
                <WhiteboardCardEditor
                  value={card.content}
                  className={tone.panelClassName}
                  onChange={(content) => onUpdateCard?.(card.id, { content })}
                />
              ) : (
                <WhiteboardCardPreview
                  content={card.content}
                  className={tone.panelClassName}
                  onToggleTask={(taskIndex) =>
                    onUpdateCard?.(card.id, {
                      content: toggleTaskAtIndex(card.content, taskIndex),
                    })
                  }
                />
              )}
            </div>
            <div className={cn("flex items-center justify-between border-t px-4 py-2 text-[11px] text-muted-foreground", tone.footerClassName)}>
              <span className="inline-flex items-center gap-1">
                <span className="h-1.5 w-1.5 rounded-full bg-accent" />
                内部滚动区
              </span>
              <button
                type="button"
                className="inline-flex cursor-row-resize items-center gap-1 rounded-full border border-border bg-foreground px-2.5 py-1 text-background transition-colors hover:opacity-90"
                aria-label="调整卡片高度"
                onPointerDown={onResizePointerDown}
              >
                <GripHorizontal className="h-3.5 w-3.5" />
                resize
              </button>
            </div>
          </div>
        )}
      </div>
    </article>
  );
}

function getCardTone(category: string) {
  const value = category.trim().toLowerCase();
  if (value.includes("todo") || value.includes("task")) {
    return {
      shellClassName:
        "border-amber-200/80 bg-[linear-gradient(180deg,hsl(var(--card)/0.98),rgba(251,191,36,0.12))] text-card-foreground dark:border-amber-900/70 dark:bg-[linear-gradient(180deg,hsl(var(--card)/0.98),rgba(251,191,36,0.1))]",
      bodyClassName: "border-amber-200/70 bg-transparent dark:border-amber-900/60",
      summaryClassName: "border-amber-200/70 bg-amber-50/35 dark:border-amber-900/60 dark:bg-amber-950/20",
      footerClassName: "border-amber-200/70 bg-amber-50/20 dark:border-amber-900/60 dark:bg-amber-950/10",
      panelClassName: "border-amber-200/80 bg-background/88 dark:border-amber-900/60 dark:bg-background/40",
      categoryClassName:
        "border-amber-300/80 bg-amber-100/90 text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-200",
      toolbarClassName: "text-muted-foreground",
    };
  }
  if (value.includes("detail")) {
    return {
      shellClassName:
        "border-emerald-200/80 bg-[linear-gradient(180deg,hsl(var(--card)/0.98),rgba(16,185,129,0.1))] text-card-foreground dark:border-emerald-900/70 dark:bg-[linear-gradient(180deg,hsl(var(--card)/0.98),rgba(16,185,129,0.08))]",
      bodyClassName: "border-emerald-200/70 bg-transparent dark:border-emerald-900/60",
      summaryClassName: "border-emerald-200/70 bg-emerald-50/35 dark:border-emerald-900/60 dark:bg-emerald-950/18",
      footerClassName: "border-emerald-200/70 bg-emerald-50/20 dark:border-emerald-900/60 dark:bg-emerald-950/10",
      panelClassName: "border-emerald-200/80 bg-background/88 dark:border-emerald-900/60 dark:bg-background/40",
      categoryClassName:
        "border-emerald-300/80 bg-emerald-100/90 text-emerald-900 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-200",
      toolbarClassName: "text-muted-foreground",
    };
  }
  if (value.includes("quick")) {
    return {
      shellClassName:
        "border-sky-200/80 bg-[linear-gradient(180deg,hsl(var(--card)/0.98),rgba(56,189,248,0.11))] text-card-foreground dark:border-sky-900/70 dark:bg-[linear-gradient(180deg,hsl(var(--card)/0.98),rgba(56,189,248,0.08))]",
      bodyClassName: "border-sky-200/70 bg-transparent dark:border-sky-900/60",
      summaryClassName: "border-sky-200/70 bg-sky-50/35 dark:border-sky-900/60 dark:bg-sky-950/18",
      footerClassName: "border-sky-200/70 bg-sky-50/20 dark:border-sky-900/60 dark:bg-sky-950/10",
      panelClassName: "border-sky-200/80 bg-background/88 dark:border-sky-900/60 dark:bg-background/40",
      categoryClassName:
        "border-sky-300/80 bg-sky-100/90 text-sky-900 dark:border-sky-800 dark:bg-sky-950/40 dark:text-sky-200",
      toolbarClassName: "text-muted-foreground",
    };
  }
  return {
    shellClassName:
      "border-border/80 bg-[linear-gradient(180deg,hsl(var(--card)/0.98),hsl(var(--muted)/0.38))] text-card-foreground dark:bg-[linear-gradient(180deg,hsl(var(--card)/0.98),hsl(var(--muted)/0.18))]",
    bodyClassName: "border-border/80 bg-transparent",
    summaryClassName: "border-border/80 bg-muted/25 dark:bg-muted/20",
    footerClassName: "border-border/80 bg-muted/15 dark:bg-muted/10",
    panelClassName: "border-border/80 bg-background/88 dark:bg-background/40",
    categoryClassName:
      "border-border/80 bg-muted/85 text-foreground dark:bg-muted/55 dark:text-foreground",
    toolbarClassName: "text-muted-foreground",
  };
}
