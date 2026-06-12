import { useEffect, useRef } from "react";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

interface WhiteboardCardEditorProps {
  value: string;
  className?: string;
  autoFocus?: boolean;
  onChange?: (value: string) => void;
  onBlur?: () => void;
}

export function WhiteboardCardEditor({
  value,
  className,
  autoFocus = false,
  onChange,
  onBlur,
}: WhiteboardCardEditorProps) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    if (!autoFocus || !textareaRef.current) return;
    textareaRef.current.focus();
    const end = textareaRef.current.value.length;
    textareaRef.current.setSelectionRange(end, end);
  }, [autoFocus]);

  return (
    <div
      className={cn(
        "flex h-full flex-col overflow-hidden rounded-[0.95rem] border border-border/70 bg-background/95 text-foreground shadow-sm dark:bg-background/45",
        className,
      )}
    >
      <div className="min-h-0 flex-1">
        <Textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => onChange?.(e.target.value)}
          onBlur={onBlur}
          onKeyDown={(event) => {
            if (event.key === "Escape" || ((event.metaKey || event.ctrlKey) && event.key === "Enter")) {
              event.preventDefault();
              event.currentTarget.blur();
            }
          }}
          className="scrollbar-overlay h-full min-h-full rounded-none border-0 bg-transparent px-3.5 py-3.5 font-mono text-[12px] leading-6 text-foreground shadow-none focus-visible:ring-0 placeholder:text-muted-foreground"
          placeholder="直接写内容；待办用 - [ ]"
          aria-label="Whiteboard card editor"
          onPointerDown={(event) => event.stopPropagation()}
        />
      </div>
    </div>
  );
}
