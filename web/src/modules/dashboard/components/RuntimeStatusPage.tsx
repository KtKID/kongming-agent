import { useEffect, useState } from "react";
import { ConnectionSummaryCards } from "@/modules/dashboard/components/ConnectionSummaryCards";
import { ActiveCellsTable } from "@/modules/dashboard/components/ActiveCellsTable";
import { fetchDashboardPollIntervalMs } from "@/modules/dashboard/api";
import { useRuntimeStatusStore } from "@/modules/dashboard/store";

const DEFAULT_POLL_MS = 5000;

export function RuntimeStatusPage() {
  const snapshot = useRuntimeStatusStore((s) => s.snapshot);
  const loading = useRuntimeStatusStore((s) => s.loading);
  const error = useRuntimeStatusStore((s) => s.error);
  const refresh = useRuntimeStatusStore((s) => s.refresh);
  const [pollMs, setPollMs] = useState(DEFAULT_POLL_MS);

  useEffect(() => {
    let cancelled = false;
    fetchDashboardPollIntervalMs()
      .then((value) => {
        if (!cancelled) {
          setPollMs(value);
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => {
      void refresh();
    }, pollMs);
    return () => window.clearInterval(id);
  }, [pollMs, refresh]);

  return (
    <div className="space-y-5">
      {snapshot ? <ConnectionSummaryCards snapshot={snapshot} /> : null}

      <section className="rounded-2xl border border-border/70 bg-card/75 p-4 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold text-foreground">全局连接</h3>
            <p className="mt-1 text-xs text-muted-foreground">
              每个已登录 tab 固定占用 `/ws/thread-status` 和 `/ws/cron` 两条全局 WS。
            </p>
          </div>
          <div className="text-xs text-muted-foreground">
            {snapshot
              ? `updated ${new Date(snapshot.generated_at_ms).toLocaleTimeString()}`
              : loading
                ? "loading"
                : "waiting"}
          </div>
        </div>

        {error ? (
          <div className="mt-4 rounded-xl border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        ) : null}

        {snapshot ? (
          <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <div className="rounded-xl bg-muted/55 px-4 py-3">
              <div className="text-[11px] uppercase tracking-[0.2em] text-muted-foreground">
                thread-status ws
              </div>
              <div className="mt-2 text-xl font-semibold">
                {snapshot.global_ws.thread_status_connections}
              </div>
              <div className="mt-2 text-[11px] text-muted-foreground">
                每个已登录 tab 固定占 1 条
              </div>
            </div>
            <div className="rounded-xl bg-muted/55 px-4 py-3">
              <div className="text-[11px] uppercase tracking-[0.2em] text-muted-foreground">
                cron ws
              </div>
              <div className="mt-2 text-xl font-semibold">
                {snapshot.global_ws.cron_connections}
              </div>
              <div className="mt-2 text-[11px] text-muted-foreground">
                当前实现为全局常驻连接，每个已登录 tab 固定占 1 条
              </div>
            </div>
            <div className="rounded-xl bg-muted/55 px-4 py-3">
              <div className="text-[11px] uppercase tracking-[0.2em] text-muted-foreground">
                claude sessions
              </div>
              <div className="mt-2 text-xl font-semibold">
                {snapshot.provider_sessions.claude_active_sessions}
              </div>
              <div className="mt-2 text-[11px] text-muted-foreground">
                仅统计活跃 claude_code session
              </div>
            </div>
            <div className="rounded-xl bg-muted/55 px-4 py-3">
              <div className="text-[11px] uppercase tracking-[0.2em] text-muted-foreground">
                codex sessions
              </div>
              <div className="mt-2 text-xl font-semibold">
                {snapshot.provider_sessions.codex_active_sessions}
              </div>
              <div className="mt-2 text-[11px] text-muted-foreground">
                仅统计活跃 codex session
              </div>
            </div>
          </div>
        ) : null}

        <div className="mt-4 text-xs leading-6 text-muted-foreground">
          <div>`cell` 表示一个活跃 generic_chat thread runtime。</div>
          <div>`chat ws` 表示聊天 websocket 总数；同一 thread 两个 tab 会显示为 2。</div>
          <div>
            `workspace-shell` 当前显示为{" "}
            {snapshot?.workspace_shell_connections == null ? "未纳入统一计数" : snapshot.workspace_shell_connections}
            。
          </div>
        </div>
      </section>

      <section className="space-y-3">
        <div>
          <h3 className="text-sm font-semibold text-foreground">Active Cells</h3>
          <p className="mt-1 text-xs text-muted-foreground">
            当前表格按 `last_active_at` 倒序，直接展示每个活跃 cell 的连接与审批状态。
          </p>
        </div>
        <ActiveCellsTable cells={snapshot?.cells ?? []} />
      </section>
    </div>
  );
}
