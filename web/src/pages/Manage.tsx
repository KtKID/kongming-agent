import { useEffect } from "react";
import { useCellsStore } from "@/stores/cells";
import { CellRow } from "@/components/CellRow";
import { ScrollArea } from "@/components/ui/scroll-area";

const POLL_MS = 5000;

export function ManagePage() {
  const cells = useCellsStore((s) => s.cells);
  const refresh = useCellsStore((s) => s.refresh);

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => {
      void refresh();
    }, POLL_MS);
    return () => window.clearInterval(id);
  }, [refresh]);

  return (
    <ScrollArea className="h-full">
      <div className="mx-auto max-w-5xl p-6">
        <h2 className="mb-4 text-lg font-semibold">运行管理</h2>
        {cells.length === 0 ? (
          <div className="rounded-md border border-dashed border-border px-4 py-12 text-center text-sm text-muted-foreground">
            暂无活动 cell
          </div>
        ) : (
          <div className="overflow-hidden rounded-md border border-border">
            <table className="w-full text-sm">
              <thead className="bg-muted text-xs uppercase text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 text-left">thread_id</th>
                  <th className="px-3 py-2 text-left">名称</th>
                  <th className="px-3 py-2 text-left">preset</th>
                  <th className="px-3 py-2 text-left">状态</th>
                  <th className="px-3 py-2 text-left">turn</th>
                  <th className="px-3 py-2 text-left">pending</th>
                  <th className="px-3 py-2 text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {cells.map((c) => (
                  <CellRow key={c.thread_id} cell={c} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </ScrollArea>
  );
}
