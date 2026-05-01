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
        "flex h-full flex-col overflow-hidden rounded-[1rem] border border-stone-300/80 bg-white/92 text-stone-900 shadow-sm",
        className,
      )}
    >
      <div className="border-b border-stone-200 px-3 py-2 text-[11px] font-medium uppercase tracking-[0.2em] text-stone-500">
        预览区
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3 scrollbar-thin">
        {content.trim() ? (
          <WhiteboardMarkdown
            text={content}
            className="text-sm"
            onToggleTask={onToggleTask}
          />
        ) : (
          <div className="flex h-full items-center justify-center text-xs text-stone-400">
            这里显示 markdown 预览
          </div>
        )}
      </div>
    </div>
  );
}
