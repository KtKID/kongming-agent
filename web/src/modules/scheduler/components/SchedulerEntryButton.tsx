/**
 * 顶部入口按钮"定时任务"
 */

import { Clock } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useSchedulerStore } from "../store";

export function SchedulerEntryButton() {
  const openDrawer = useSchedulerStore((s) => s.openDrawer);

  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={openDrawer}
      className="text-muted-foreground hover:bg-secondary"
      aria-label="定时任务"
    >
      <Clock className="h-3.5 w-3.5" />
      <span className="text-xs">定时任务</span>
    </Button>
  );
}
