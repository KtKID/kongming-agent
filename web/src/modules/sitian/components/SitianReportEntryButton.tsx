/**
 * 顶部入口按钮"司天报告"
 *
 * 紧挨 SchedulerEntryButton 摆放（由 Layout.tsx 决定）。
 * 点击 → useSitian().open() → manager 内部触发 fetch。
 */

import { Telescope } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useSitian } from "../hooks/useSitian";

export function SitianReportEntryButton() {
  const { open, loading } = useSitian();

  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={() => void open()}
      disabled={loading}
      className="text-muted-foreground hover:bg-secondary"
      aria-label="司天报告"
    >
      <Telescope className="h-3.5 w-3.5" />
      <span className="text-xs">司天报告</span>
    </Button>
  );
}
