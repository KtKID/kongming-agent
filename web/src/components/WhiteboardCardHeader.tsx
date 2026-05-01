import { ChevronDown, ChevronUp, Eye, GripHorizontal, SquarePen, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";

interface WhiteboardCardHeaderProps {
  title: string;
  category: string;
  mode: "editor" | "preview";
  collapsed: boolean;
  updatedLabel?: string;
  categoryClassName?: string;
  toolbarClassName?: string;
  onDragPointerDown?: React.PointerEventHandler<HTMLDivElement>;
  onToggleCollapse?: () => void;
  onDelete?: () => void;
  onTitleChange?: (title: string) => void;
  onCategoryChange?: (category: string) => void;
  onModeChange?: (mode: "editor" | "preview") => void;
}

export function WhiteboardCardHeader({
  title,
  category,
  mode,
  collapsed,
  updatedLabel,
  categoryClassName,
  toolbarClassName,
  onDragPointerDown,
  onToggleCollapse,
  onDelete,
  onTitleChange,
  onCategoryChange,
  onModeChange,
}: WhiteboardCardHeaderProps) {
  return (
    <div className="px-4 py-3">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div
          className="inline-flex select-none touch-none items-center gap-2 rounded-full border border-stone-300/80 bg-white/80 px-2.5 py-1 text-[11px] font-medium text-stone-600 shadow-sm cursor-grab active:cursor-grabbing"
          onPointerDown={onDragPointerDown}
        >
          <GripHorizontal className="h-3.5 w-3.5" />
          拖动卡片
        </div>
        {updatedLabel ? (
          <span className="truncate text-[11px] text-stone-500">
            {updatedLabel}
          </span>
        ) : null}
      </div>
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1 space-y-2">
          <input
            value={title}
            onChange={(e) => onTitleChange?.(e.target.value)}
            className="h-7 w-full truncate border-0 bg-transparent px-0 text-sm font-semibold tracking-tight text-stone-900 outline-none placeholder:text-stone-400"
            placeholder="卡片标题"
            onPointerDown={(e) => e.stopPropagation()}
            aria-label="卡片标题"
          />
          <div className="flex items-center gap-2">
            <input
              value={category}
              onChange={(e) => onCategoryChange?.(e.target.value)}
              className={cn(
                "h-6 min-w-0 rounded-full border px-2.5 text-[11px] font-semibold uppercase tracking-[0.18em] outline-none transition-colors focus:border-accent focus:bg-background",
                categoryClassName ??
                  "border-stone-300/80 bg-white/85 text-stone-700",
              )}
              placeholder="note"
              onPointerDown={(e) => e.stopPropagation()}
              aria-label="卡片类别"
            />
          </div>
        </div>
        <div className={cn("flex shrink-0 items-center gap-1", toolbarClassName)}>
          <button
            type="button"
            onClick={onDelete}
            className="inline-flex h-8 w-8 items-center justify-center rounded-full border border-rose-200 bg-rose-50 text-rose-700 transition-colors hover:bg-rose-100"
            aria-label="删除卡片"
          >
            <Trash2 className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={() => onModeChange?.(mode === "editor" ? "preview" : "editor")}
            className={cn(
              "inline-flex h-8 items-center gap-1 rounded-full border border-border px-2.5 text-[11px] font-medium transition-colors",
              "bg-stone-900 text-white hover:bg-stone-800",
            )}
            aria-label={mode === "editor" ? "切换到预览" : "切换到编辑"}
          >
            {mode === "editor" ? (
              <>
                <Eye className="h-3.5 w-3.5" />
                预览
              </>
            ) : (
              <>
                <SquarePen className="h-3.5 w-3.5" />
                编辑
              </>
            )}
          </button>
          <button
            type="button"
            onClick={onToggleCollapse}
            className="inline-flex h-8 w-8 items-center justify-center rounded-full border border-stone-700 bg-stone-900 text-white transition-colors hover:bg-stone-800"
            aria-label={collapsed ? "展开卡片" : "折叠卡片"}
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
  );
}
