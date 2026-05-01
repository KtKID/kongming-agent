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
        "hover:shadow-md",
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
          <div className={cn("border-t px-4 py-3 text-xs text-stone-500", tone.summaryClassName)}>
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
            <div className={cn("flex items-center justify-between border-t px-4 py-2 text-[11px] text-stone-500", tone.footerClassName)}>
              <span className="inline-flex items-center gap-1">
                <span className="h-1.5 w-1.5 rounded-full bg-blue-500" />
                内部滚动区
              </span>
              <button
                type="button"
                className="inline-flex cursor-row-resize items-center gap-1 rounded-full border border-stone-700 bg-stone-900 px-2.5 py-1 text-white transition-colors hover:bg-stone-800"
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
        "border-amber-300/80 bg-[linear-gradient(180deg,rgba(255,248,220,0.98),rgba(255,252,239,0.98))] text-stone-900",
      bodyClassName: "border-amber-200/80 bg-transparent",
      summaryClassName: "border-amber-200/80 bg-white/45",
      footerClassName: "border-amber-200/80 bg-white/30",
      panelClassName: "border-amber-200/90 bg-white/92",
      categoryClassName:
        "border-amber-400/70 bg-amber-100/95 text-amber-900",
      toolbarClassName: "text-stone-700",
    };
  }
  if (value.includes("detail")) {
    return {
      shellClassName:
        "border-emerald-300/80 bg-[linear-gradient(180deg,rgba(245,252,247,0.98),rgba(253,255,254,0.98))] text-stone-900",
      bodyClassName: "border-emerald-200/80 bg-transparent",
      summaryClassName: "border-emerald-200/80 bg-white/45",
      footerClassName: "border-emerald-200/80 bg-white/30",
      panelClassName: "border-emerald-200/90 bg-white/92",
      categoryClassName:
        "border-emerald-400/70 bg-emerald-100/95 text-emerald-900",
      toolbarClassName: "text-stone-700",
    };
  }
  if (value.includes("quick")) {
    return {
      shellClassName:
        "border-rose-300/80 bg-[linear-gradient(180deg,rgba(255,244,238,0.98),rgba(255,251,249,0.98))] text-stone-900",
      bodyClassName: "border-rose-200/80 bg-transparent",
      summaryClassName: "border-rose-200/80 bg-white/45",
      footerClassName: "border-rose-200/80 bg-white/30",
      panelClassName: "border-rose-200/90 bg-white/92",
      categoryClassName:
        "border-rose-400/70 bg-rose-100/95 text-rose-900",
      toolbarClassName: "text-stone-700",
    };
  }
  return {
    shellClassName:
      "border-violet-300/80 bg-[linear-gradient(180deg,rgba(249,246,255,0.98),rgba(255,253,255,0.98))] text-stone-900",
    bodyClassName: "border-violet-200/80 bg-transparent",
    summaryClassName: "border-violet-200/80 bg-white/45",
    footerClassName: "border-violet-200/80 bg-white/30",
    panelClassName: "border-violet-200/90 bg-white/92",
    categoryClassName:
      "border-violet-400/70 bg-violet-100/95 text-violet-900",
    toolbarClassName: "text-stone-700",
  };
}
