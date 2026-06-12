import type { RuntimeStatusSnapshotDTO } from "@/protocol";

interface Props {
  snapshot: RuntimeStatusSnapshotDTO;
}

function StatCard({
  label,
  value,
  hint,
}: {
  label: string;
  value: string | number;
  hint: string;
}) {
  return (
    <div className="rounded-2xl border border-border/70 bg-card/75 p-4 shadow-sm">
      <div className="text-[11px] uppercase tracking-[0.22em] text-muted-foreground">
        {label}
      </div>
      <div className="mt-2 text-2xl font-semibold tracking-tight text-foreground">
        {value}
      </div>
      <div className="mt-2 text-xs text-muted-foreground">{hint}</div>
    </div>
  );
}

export function ConnectionSummaryCards({ snapshot }: Props) {
  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
      <StatCard
        label="PID"
        value={snapshot.process.pid}
        hint={`web server process · ${snapshot.process.url} · 每 ${snapshot.polling.interval_seconds}s 刷新`}
      />
      <StatCard
        label="Cells"
        value={snapshot.cells_total}
        hint="活跃 generic_chat thread runtime 数"
      />
      <StatCard
        label="Chat WS"
        value={snapshot.chat_ws_connections_total}
        hint="同一 thread 多个 tab 会继续累加"
      />
      <StatCard
        label="Approvals"
        value={snapshot.approval_pending_total}
        hint={`subscriber=${snapshot.global_ws.approval_subscribers}`}
      />
    </div>
  );
}
