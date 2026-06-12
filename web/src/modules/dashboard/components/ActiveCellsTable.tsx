import type { ActiveCellStatusDTO } from "@/protocol";

interface Props {
  cells: ActiveCellStatusDTO[];
}

function formatTime(unixSeconds: number) {
  return new Date(unixSeconds * 1000).toLocaleString();
}

function StatusBadge({ status }: { status: ActiveCellStatusDTO["status"] }) {
  const tone =
    status === "running"
      ? "border-emerald-500/20 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
      : status === "awaiting_approval"
        ? "border-amber-500/20 bg-amber-500/10 text-amber-700 dark:text-amber-300"
        : "border-border/80 bg-muted text-muted-foreground";
  return (
    <span
      className={`inline-flex rounded-full border px-2 py-0.5 text-[11px] font-medium ${tone}`}
    >
      {status}
    </span>
  );
}

export function ActiveCellsTable({ cells }: Props) {
  if (cells.length === 0) {
    return (
      <div className="rounded-2xl border border-dashed border-border/80 px-4 py-12 text-center text-sm text-muted-foreground">
        暂无活动 cell
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-2xl border border-border/70 bg-card/75 shadow-sm">
      <table className="w-full text-sm">
        <thead className="bg-muted/70 text-xs uppercase tracking-[0.18em] text-muted-foreground">
          <tr>
            <th className="px-3 py-3 text-left">thread</th>
            <th className="px-3 py-3 text-left">backend</th>
            <th className="px-3 py-3 text-left">状态</th>
            <th className="px-3 py-3 text-left">chat ws</th>
            <th className="px-3 py-3 text-left">pending</th>
            <th className="px-3 py-3 text-left">last active</th>
          </tr>
        </thead>
        <tbody>
          {cells.map((cell) => (
            <tr key={cell.thread_id} className="border-t border-border/60 align-top">
              <td className="px-3 py-3">
                <div className="font-medium text-foreground">{cell.thread_name}</div>
                <div className="mt-1 font-mono text-[11px] text-muted-foreground">
                  {cell.thread_id}
                </div>
                <div className="mt-1 text-[11px] text-muted-foreground">
                  preset={cell.preset_id || "-"}
                </div>
              </td>
              <td className="px-3 py-3">
                <div className="text-foreground">{cell.backend_kind}</div>
                <div className="mt-1 max-w-[220px] truncate text-[11px] text-muted-foreground">
                  {cell.cwd || "无 cwd"}
                </div>
              </td>
              <td className="px-3 py-3">
                <StatusBadge status={cell.status} />
              </td>
              <td className="px-3 py-3 font-medium text-foreground">
                {cell.chat_ws_connections}
              </td>
              <td className="px-3 py-3 text-foreground">
                {cell.pending_approval_count}
              </td>
              <td className="px-3 py-3 text-[12px] text-muted-foreground">
                {formatTime(cell.last_active_at)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
