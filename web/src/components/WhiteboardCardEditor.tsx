import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

interface WhiteboardCardEditorProps {
  value: string;
  className?: string;
  onChange?: (value: string) => void;
}

export function WhiteboardCardEditor({
  value,
  className,
  onChange,
}: WhiteboardCardEditorProps) {
  return (
    <div
      className={cn(
        "flex h-full flex-col overflow-hidden rounded-[1rem] border border-border/80 bg-background/92 text-foreground shadow-sm dark:bg-background/45",
        className,
      )}
    >
      <div className="border-b border-border/80 px-3 py-2 text-[11px] font-medium uppercase tracking-[0.2em] text-muted-foreground">
        编辑区
      </div>
      <div className="min-h-0 flex-1">
        <Textarea
          value={value}
          onChange={(e) => onChange?.(e.target.value)}
          className="scrollbar-overlay h-full min-h-full rounded-none border-0 bg-transparent px-3 py-3 font-mono text-xs leading-6 text-foreground shadow-none focus-visible:ring-0 placeholder:text-muted-foreground"
          placeholder="在这里输入 markdown 内容"
          aria-label="白板卡片内容编辑器"
        />
      </div>
    </div>
  );
}
