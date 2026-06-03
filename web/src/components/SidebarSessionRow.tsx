import type { CSSProperties, JSX, ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface HoverAction {
  icon: LucideIcon;
  label: string;
  variant?: "default" | "destructive";
  onClick: () => void;
}

export interface SidebarSessionRowProps {
  /** 当前行是否选中（高亮 bg-accent） */
  selected?: boolean;
  /** 左侧 leading 区（icon / badge） */
  leading?: ReactNode;
  /** 行标题文字 */
  title: string;
  /** 右侧附加信息（时间戳 / message count badge） */
  meta?: ReactNode;
  /** hover 时浮出的操作按钮 */
  actions?: HoverAction[];
  /** 编辑态：为 true 时整行替换为 editSlot */
  editing?: boolean;
  /** 编辑态替换内容（通常是 input） */
  editSlot?: ReactNode;
  /** 点击行回调（navigate / import / ...） */
  onOpen: () => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * 侧边栏 session 行原语 — 三频道（通用/Claude/Codex）共用。
 *
 * 职责：
 * - button 容器 + selected/hover 样式
 * - hover 操作浮层（absolute + gradient 遮罩 + N 个 action button）
 * - editing 切换（整行替换为 editSlot）
 *
 * 不管的：
 * - 数据源、API 调用、toast — 全部由调用方通过 actions/onOpen 回调处理
 */
export function SidebarSessionRow({
  selected = false,
  leading,
  title,
  meta,
  actions,
  editing = false,
  editSlot,
  onOpen,
}: SidebarSessionRowProps): JSX.Element {
  const hasActions = Boolean(actions && actions.length > 0);
  const railWidthRem = hasActions
    ? Math.max(5.6, (actions?.length ?? 0) * 1.95 + 0.6)
    : 0;

  if (editing && editSlot) {
    return (
      <div className="flex items-center gap-2 rounded-[1.4rem] border border-border/70 bg-card/76 px-3 py-2 shadow-sm">
        {leading}
        <div className="min-w-0 flex-1">{editSlot}</div>
      </div>
    );
  }

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen();
        }
      }}
      title={title}
      style={
        hasActions
          ? ({ "--action-rail-width": `${railWidthRem}rem` } as CSSProperties)
          : undefined
      }
      className={cn(
        "group relative flex h-[46px] w-full items-center gap-2 overflow-hidden rounded-[1.4rem] border px-3 text-left text-sm transition-[border-color,background-color,box-shadow,transform] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/22 focus-visible:ring-offset-2 focus-visible:ring-offset-background",
        selected
          ? "border-primary/20 bg-primary/10 text-foreground shadow-sm"
          : "border-border/70 bg-card/76 text-foreground/92 shadow-sm hover:border-primary/18 hover:bg-card/92 hover:-translate-y-[1px]",
      )}
    >
      <div
        className={cn(
          "grid min-w-0 flex-1 items-center gap-2",
          meta ? "grid-cols-[minmax(0,1fr)_30%]" : "grid-cols-[minmax(0,1fr)]",
        )}
      >
        <div className="flex min-w-0 items-center gap-2">
          {leading}
          <div className="min-w-0 flex-1">
            <span className="block truncate text-[15px] leading-none transition-opacity group-hover:opacity-95">
              {title}
            </span>
          </div>
        </div>
        {meta ? (
          <div className="relative z-10 flex min-w-0 items-center justify-end gap-2 rounded-l-full bg-card px-2 pr-6 text-muted-foreground shadow-[inset_10px_0_12px_-12px_hsl(var(--background)/0.65)] transition-opacity duration-200 group-hover:opacity-70">
            {meta}
          </div>
        ) : null}
      </div>
      {hasActions && (
        <div
          className={cn(
            "absolute right-2 top-1/2 z-20 flex h-8 -translate-y-1/2 items-center overflow-hidden rounded-full border border-transparent bg-transparent shadow-none backdrop-blur-sm",
            "w-[18px] justify-end transition-[width,opacity] duration-200 ease-standard",
            "group-hover:w-[var(--action-rail-width)] group-hover:border-border/80 group-hover:bg-secondary/95 group-hover:shadow-sm",
          )}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="pointer-events-none absolute inset-y-1.5 right-[0.62rem] w-px rounded-full bg-border/60 opacity-90 transition-opacity duration-150 group-hover:opacity-0" />
          <div className="pointer-events-none absolute right-[0.92rem] top-1/2 h-1.5 w-1.5 -translate-y-1/2 rounded-full bg-border/75 opacity-90 transition-opacity duration-150 group-hover:opacity-0" />
          <div className="flex w-full items-center justify-end gap-1 px-1.5 translate-x-2 opacity-0 transition-[opacity,transform] duration-150 ease-standard group-hover:translate-x-0 group-hover:opacity-100">
            {actions!.map((action) => (
              <button
                key={action.label}
                type="button"
                aria-label={action.label}
                title={action.label}
                className={cn(
                  "inline-flex h-6 w-6 items-center justify-center rounded-full border border-transparent bg-background/80 text-muted-foreground transition-colors",
                  action.variant === "destructive"
                    ? "hover:border-destructive/20 hover:bg-destructive/12 hover:text-destructive"
                    : "hover:border-primary/16 hover:bg-background hover:text-foreground",
                )}
                onClick={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  action.onClick();
                }}
              >
                <action.icon className="h-3.5 w-3.5" />
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
