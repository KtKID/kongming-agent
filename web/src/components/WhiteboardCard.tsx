import { useEffect, useRef, useState } from "react";
import { WhiteboardCardEditor } from "@/components/WhiteboardCardEditor";
import { WhiteboardCardHeader } from "@/components/WhiteboardCardHeader";
import { WhiteboardCardPreview } from "@/components/WhiteboardCardPreview";
import { toggleTaskAtIndex } from "@/lib/whiteboard-markdown";
import {
  shouldStartWhiteboardCardInEditor,
} from "@/lib/whiteboard-card-templates";
import { cn } from "@/lib/utils";
import type { CardScope } from "@/protocol";

export interface WhiteboardCardItem {
  id: string;
  scope: CardScope;
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
  onResizePointerDown?: React.PointerEventHandler<HTMLDivElement>;
  onToggleCollapse?: (cardId: string) => void;
  onDeleteCard?: (cardId: string) => void;
  onUpdateCard?: (cardId: string, patch: Partial<WhiteboardCardItem>) => void;
}

const MIN_CARD_HEIGHT = 160;
const DEFAULT_CARD_HEIGHT = 200;

export function WhiteboardCard({
  card,
  onDragPointerDown,
  onResizePointerDown,
  onToggleCollapse,
  onDeleteCard,
  onUpdateCard,
}: WhiteboardCardProps) {
  const [isEditing, setIsEditing] = useState<boolean>(() =>
    shouldStartWhiteboardCardInEditor(card.content),
  );
  const lastCardIdRef = useRef(card.id);
  const cardHeight = Math.max(card.height ?? DEFAULT_CARD_HEIGHT, MIN_CARD_HEIGHT);
  const summary =
    card.content.split("\n").find((line) => line.trim().length > 0)?.trim() ??
    "空白卡片";
  const tone = getCardTone(card.category);

  useEffect(() => {
    if (lastCardIdRef.current === card.id) return;
    lastCardIdRef.current = card.id;
    setIsEditing(shouldStartWhiteboardCardInEditor(card.content));
  }, [card.id, card.content]);

  const shellStyle: React.CSSProperties = {};
  if (!card.collapsed) {
    shellStyle.height = cardHeight;
  }
  if (card.scope === "global") {
    shellStyle.borderColor = "hsl(var(--whiteboard-scope-global-border))";
  }

  return (
    <article
      data-testid="whiteboard-card-shell"
      className={cn(
        "group overflow-hidden rounded-[1.15rem] border shadow-sm backdrop-blur-sm transition-shadow",
        "hover:shadow-md dark:hover:shadow-black/20",
        tone.shellClassName,
      )}
      style={Object.keys(shellStyle).length > 0 ? shellStyle : undefined}
      onPointerDown={onDragPointerDown}
    >
      <div className="flex h-full flex-col">
        <WhiteboardCardHeader
          title={card.title}
          category={card.category}
          collapsed={card.collapsed}
          isEditing={isEditing}
          updatedLabel={card.updatedLabel}
          categoryClassName={tone.categoryClassName}
          toolbarClassName={tone.toolbarClassName}
          onToggleCollapse={() => onToggleCollapse?.(card.id)}
          onDelete={() => onDeleteCard?.(card.id)}
          onTitleChange={(title) => onUpdateCard?.(card.id, { title })}
          onCategoryChange={(category) => onUpdateCard?.(card.id, { category })}
        />
        {card.collapsed ? (
          <div
            className={cn(
              "border-t px-3.5 py-2.5 text-xs text-muted-foreground",
              tone.summaryClassName,
            )}
          >
            <div className="truncate">{summary}</div>
          </div>
        ) : (
          <div
            className={cn(
              "relative flex min-h-0 flex-1 flex-col border-t px-2 pb-2 pt-1.5",
              tone.bodyClassName,
            )}
          >
            <div className="min-h-0 flex-1">
              {isEditing ? (
                <WhiteboardCardEditor
                  value={card.content}
                  autoFocus={true}
                  className={cn("h-full", tone.panelClassName)}
                  onChange={(content) => onUpdateCard?.(card.id, { content })}
                  onBlur={() => setIsEditing(false)}
                />
              ) : (
                <WhiteboardCardPreview
                  content={card.content}
                  className={cn("h-full", tone.panelClassName)}
                  onOpenEditor={() => setIsEditing(true)}
                  onToggleTask={(taskIndex) =>
                    onUpdateCard?.(card.id, {
                      content: toggleTaskAtIndex(card.content, taskIndex),
                    })
                  }
                />
              )}
            </div>
            <div
              data-testid="whiteboard-card-resize-edge"
              className="absolute inset-x-3 bottom-0 h-3 cursor-row-resize"
              aria-label="Resize card height"
              onPointerDown={(event) => {
                event.stopPropagation();
                onResizePointerDown?.(event);
              }}
            />
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
        "border-amber-200/80 bg-card text-card-foreground dark:border-amber-900/70 dark:bg-card",
      bodyClassName:
        "border-amber-200/70 bg-transparent dark:border-amber-900/60",
      summaryClassName:
        "border-amber-200/70 bg-amber-50/35 dark:border-amber-900/60 dark:bg-amber-950/20",
      panelClassName:
        "border-amber-200/80 bg-background/88 dark:border-amber-900/60 dark:bg-background/40",
      categoryClassName:
        "border-amber-300/80 bg-amber-100/90 text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-200",
      toolbarClassName: "text-muted-foreground",
    };
  }
  if (value.includes("detail")) {
    return {
      shellClassName:
        "border-emerald-200/80 bg-card text-card-foreground dark:border-emerald-900/70 dark:bg-card",
      bodyClassName:
        "border-emerald-200/70 bg-transparent dark:border-emerald-900/60",
      summaryClassName:
        "border-emerald-200/70 bg-emerald-50/35 dark:border-emerald-900/60 dark:bg-emerald-950/18",
      panelClassName:
        "border-emerald-200/80 bg-background/88 dark:border-emerald-900/60 dark:bg-background/40",
      categoryClassName:
        "border-emerald-300/80 bg-emerald-100/90 text-emerald-900 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-200",
      toolbarClassName: "text-muted-foreground",
    };
  }
  if (value.includes("quick")) {
    return {
      shellClassName:
        "border-sky-200/80 bg-card text-card-foreground dark:border-sky-900/70 dark:bg-card",
      bodyClassName:
        "border-sky-200/70 bg-transparent dark:border-sky-900/60",
      summaryClassName:
        "border-sky-200/70 bg-sky-50/35 dark:border-sky-900/60 dark:bg-sky-950/18",
      panelClassName:
        "border-sky-200/80 bg-background/88 dark:border-sky-900/60 dark:bg-background/40",
      categoryClassName:
        "border-sky-300/80 bg-sky-100/90 text-sky-900 dark:border-sky-800 dark:bg-sky-950/40 dark:text-sky-200",
      toolbarClassName: "text-muted-foreground",
    };
  }
  return {
    shellClassName: "border-border/80 bg-card text-card-foreground dark:bg-card",
    bodyClassName: "border-border/80 bg-transparent",
    summaryClassName: "border-border/80 bg-muted/25 dark:bg-muted/20",
    panelClassName: "border-border/80 bg-background/88 dark:bg-background/40",
    categoryClassName:
      "border-border/80 bg-muted/85 text-foreground dark:bg-muted/55 dark:text-foreground",
    toolbarClassName: "text-muted-foreground",
  };
}
