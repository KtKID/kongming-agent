import { WhiteboardMarkdown } from "@/lib/whiteboard-markdown";
import { cn } from "@/lib/utils";

interface WhiteboardCardPreviewProps {
  content: string;
  className?: string;
  onToggleTask?: (taskIndex: number) => void;
  onOpenEditor?: () => void;
}

export function WhiteboardCardPreview({
  content,
  className,
  onToggleTask,
  onOpenEditor,
}: WhiteboardCardPreviewProps) {
  const hasContent = content.trim().length > 0;

  return (
    <div
      className={cn(
        "flex h-full flex-col overflow-hidden rounded-[0.95rem] border border-border/70 bg-background/95 text-foreground shadow-sm dark:bg-background/45",
        className,
      )}
    >
      <div
        role="button"
        tabIndex={0}
        className="min-h-0 flex-1 overflow-y-auto px-3.5 py-3 text-left scrollbar-overlay"
        onPointerDown={(event) => event.stopPropagation()}
        onClick={onOpenEditor}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            onOpenEditor?.();
          }
        }}
      >
        {hasContent ? (
          <WhiteboardMarkdown
            text={content}
            className="text-sm"
            onToggleTask={onToggleTask}
          />
        ) : (
          <div className="flex h-full min-h-[6rem] items-center justify-center rounded-2xl border border-dashed border-border/70 bg-muted/20 text-xs text-muted-foreground">
            点击开始写内容
          </div>
        )}
      </div>
    </div>
  );
}
