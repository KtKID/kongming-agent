import { WhiteboardMarkdown } from "@/lib/whiteboard-markdown";
import { cn } from "@/lib/utils";

interface WhiteboardCardPreviewProps {
  content: string;
  className?: string;
  onToggleTask?: (taskIndex: number) => void;
}

export function WhiteboardCardPreview({
  content,
  className,
  onToggleTask,
}: WhiteboardCardPreviewProps) {
  return (
    <div
      className={cn(
        "flex h-full flex-col overflow-hidden rounded-[1rem] border border-border/80 bg-background/92 text-foreground shadow-sm dark:bg-background/45",
        className,
      )}
    >
      <div className="border-b border-border/80 px-3 py-2 text-[11px] font-medium uppercase tracking-[0.2em] text-muted-foreground">
        预览区
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3 scrollbar-overlay">
        {content.trim() ? (
          <WhiteboardMarkdown
            text={content}
            className="text-sm"
            onToggleTask={onToggleTask}
          />
        ) : (
          <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
            这里显示 markdown 预览
          </div>
        )}
      </div>
    </div>
  );
}
