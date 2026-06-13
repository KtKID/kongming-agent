/**
 * 顶部入口按钮“日志”。
 *
 * 点击后打开 LogViewerOverlay，由 overlay 负责加载日志数据。
 */

import { ScrollText } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useLogViewerStore } from "../store";

export function LogViewerEntryButton() {
  const open = useLogViewerStore((s) => s.open);

  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={open}
      className="text-muted-foreground hover:bg-secondary"
      aria-label="日志"
    >
      <ScrollText className="h-3.5 w-3.5" />
      <span className="text-xs">日志</span>
    </Button>
  );
}
