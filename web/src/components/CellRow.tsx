import { useState } from "react";
import { StopCircle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { CellSummaryDTO } from "@/protocol";
import { useCellsStore } from "@/stores/cells";

const STATUS_VARIANT = {
  idle: "secondary",
  running: "default",
  awaiting_approval: "warning",
} as const;

export function CellRow({ cell }: { cell: CellSummaryDTO }) {
  const stop = useCellsStore((s) => s.stop);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const onStop = async () => {
    setBusy(true);
    try {
      await stop(cell.thread_id);
    } finally {
      setBusy(false);
      setConfirmOpen(false);
    }
  };

  return (
    <tr className="border-b border-border last:border-b-0 hover:bg-secondary/40">
      <td className="px-3 py-2 font-mono text-xs">{cell.thread_id}</td>
      <td className="px-3 py-2 text-sm">{cell.thread_name}</td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {cell.preset_id}
      </td>
      <td className="px-3 py-2">
        <Badge variant={STATUS_VARIANT[cell.status]}>{cell.status}</Badge>
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {cell.current_turn ?? "-"}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {cell.pending_approval_count}
      </td>
      <td className="px-3 py-2 text-right">
        <Button
          variant="outline"
          size="sm"
          onClick={() => setConfirmOpen(true)}
          aria-label="停止"
        >
          <StopCircle className="h-3.5 w-3.5" />
          停止
        </Button>
        <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>停止该 cell？</DialogTitle>
              <DialogDescription>
                正在执行的工具调用会被中止，无法恢复。
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => setConfirmOpen(false)}
              >
                取消
              </Button>
              <Button
                variant="destructive"
                onClick={() => void onStop()}
                disabled={busy}
              >
                确认停止
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </td>
    </tr>
  );
}
