import { ChevronDown, ChevronUp, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";

interface WhiteboardCardHeaderProps {
  title: string;
  category: string;
  collapsed: boolean;
  isEditing?: boolean;
  updatedLabel?: string;
  categoryClassName?: string;
  toolbarClassName?: string;
  onToggleCollapse?: () => void;
  onDelete?: () => void;
  onTitleChange?: (title: string) => void;
  onCategoryChange?: (category: string) => void;
}

export function WhiteboardCardHeader({
  title,
  category,
  collapsed,
  isEditing = false,
  updatedLabel,
  categoryClassName,
  toolbarClassName,
  onToggleCollapse,
  onDelete,
  onTitleChange,
  onCategoryChange,
}: WhiteboardCardHeaderProps) {
  return (
    <div className="px-3.5 py-2.5">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1 space-y-1.5">
          <div className="flex items-center gap-2">
            <input
              value={title}
              onChange={(e) => onTitleChange?.(e.target.value)}
              className="h-6 w-full truncate border-0 bg-transparent px-0 text-[15px] font-semibold tracking-tight text-foreground outline-none placeholder:text-muted-foreground"
              placeholder="便签标题"
              onPointerDown={(e) => e.stopPropagation()}
              aria-label="Card title"
            />
            {isEditing ? (
              <span className="rounded-full bg-foreground px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.18em] text-background">
                Edit
              </span>
            ) : null}
          </div>
          <input
            value={category}
            onChange={(e) => onCategoryChange?.(e.target.value)}
            className={cn(
              "h-5 min-w-0 rounded-full border px-2.5 text-[10px] font-semibold uppercase tracking-[0.18em] outline-none transition-colors focus:border-accent focus:bg-background dark:focus:bg-background",
              categoryClassName ??
                "border-border/80 bg-background/85 text-foreground dark:bg-background/50",
            )}
            placeholder="note"
            onPointerDown={(e) => e.stopPropagation()}
            aria-label="Card category"
          />
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {updatedLabel ? (
            <span className="max-w-[5.5rem] truncate text-[10px] text-muted-foreground">
              {updatedLabel}
            </span>
          ) : null}
          <div className={cn("flex items-center gap-1", toolbarClassName)}>
            <button
              type="button"
              onClick={onDelete}
              className="inline-flex h-7 w-7 items-center justify-center rounded-full border border-destructive/25 bg-destructive/10 text-destructive transition-colors hover:bg-destructive/15"
              aria-label="Delete card"
              onPointerDown={(e) => e.stopPropagation()}
            >
              <Trash2 className="h-4 w-4" />
            </button>
            <button
              type="button"
              onClick={onToggleCollapse}
              className="inline-flex h-7 w-7 items-center justify-center rounded-full border border-border bg-foreground text-background transition-colors hover:opacity-90"
              aria-label={collapsed ? "Expand card" : "Collapse card"}
              onPointerDown={(e) => e.stopPropagation()}
            >
              {collapsed ? (
                <ChevronDown className="h-4 w-4" />
              ) : (
                <ChevronUp className="h-4 w-4" />
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
