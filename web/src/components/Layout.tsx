import { Link, NavLink, Outlet, useLocation, useParams } from "react-router-dom";
import { ArrowLeft, LogOut, Settings2 } from "lucide-react";
import { ConnectionIndicator } from "@/components/ConnectionIndicator";
import { MobileToolsMenu } from "@/components/MobileToolsMenu";
import { ThemeToggle } from "@/components/ThemeToggle";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { ApprovalToastQueue } from "@/features/approval-inbox";
import { useChatLayout } from "@/hooks/useChatLayout";
import { useHeartbeatConfig } from "@/hooks/useHeartbeatConfig";
import { useThreadStatusWS } from "@/hooks/useThreadStatusWS";
import { cn } from "@/lib/utils";
import {
  SchedulerDrawerHost,
  SchedulerEntryButton,
} from "@/modules/scheduler";
import {
  SitianReportDialog,
  SitianReportEntryButton,
} from "@/modules/sitian";
import { useAuthStore } from "@/stores/auth";
import { useConnectionStatusStore } from "@/stores/connectionStatus";
import { useThreadsStore } from "@/stores/threads";

export function Layout() {
  const heartbeatConfig = useHeartbeatConfig();
  useThreadStatusWS(heartbeatConfig);
  const { isCompactLayout } = useChatLayout();
  const useCompactHeader = isCompactLayout;

  const params = useParams<{ thread_id?: string }>();
  const location = useLocation();
  const threads = useThreadsStore((s) => s.threads);
  const logout = useAuthStore((s) => s.logout);

  const threadWsActive = useConnectionStatusStore((s) => s.threadWsActive);
  const threadWsState = useConnectionStatusStore((s) => s.threadWsState);
  const threadWsLatencyMs = useConnectionStatusStore((s) => s.threadWsLatencyMs);
  const claudeWsActive = useConnectionStatusStore((s) => s.claudeWsActive);
  const claudeWsState = useConnectionStatusStore((s) => s.claudeWsState);
  const claudeWsLatencyMs = useConnectionStatusStore((s) => s.claudeWsLatencyMs);
  const statusWsState = useConnectionStatusStore((s) => s.statusWsState);
  const statusWsLatencyMs = useConnectionStatusStore((s) => s.statusWsLatencyMs);

  const current = params.thread_id
    ? threads.find((t) => t.id === params.thread_id)
    : undefined;
  const onManagePage = location.pathname.startsWith("/manage");
  const title = onManagePage ? "运行管理" : current?.name ?? "kongming-agent";

  const manageButton = onManagePage ? (
    <Link
      to="/chat"
      className="inline-flex items-center gap-1 rounded-xl border border-border/70 bg-card/72 px-3 py-2 text-xs font-medium text-muted-foreground shadow-sm transition-colors hover:bg-secondary hover:text-foreground"
    >
      <ArrowLeft className="h-3.5 w-3.5" />
      聊天
    </Link>
  ) : (
    <NavLink
      to="/manage"
      className={({ isActive }) =>
        cn(
          "rounded-xl border px-3 py-2 text-xs font-medium shadow-sm transition-colors",
          isActive
            ? "border-primary/20 bg-primary/12 text-foreground"
            : "border-border/70 bg-card/70 text-muted-foreground hover:bg-secondary hover:text-foreground",
        )
      }
    >
      <span className="inline-flex items-center gap-1.5">
        <Settings2 className="h-3.5 w-3.5" />
        管理
      </span>
    </NavLink>
  );

  return (
    <div className="flex h-full flex-col bg-transparent">
      <header
        className={cn(
          "obsidian-panel obsidian-hairline relative z-30 mx-3 mt-3 flex shrink-0 items-center rounded-[1.6rem]",
          useCompactHeader ? "min-h-15 gap-2 px-3 py-3" : "h-15 gap-3 px-5",
        )}
        data-testid="app-header"
      >
        <Link
          to="/chat"
          className={cn(
            "flex min-w-0 items-center text-sm font-semibold tracking-tight text-foreground",
            useCompactHeader ? "gap-2.5" : "gap-3",
          )}
        >
          <span className="flex h-9 w-9 items-center justify-center rounded-2xl border border-primary/20 bg-primary/10 shadow-sm">
            <img
              src="/favicon.png"
              alt="logo"
              className="h-6 w-6 rounded-md object-contain"
            />
          </span>
          <span className="flex min-w-0 flex-col leading-none">
            <span className="truncate text-base font-semibold tracking-[0.01em] text-glow">
              kongming
            </span>
            <span className="text-[10px] font-medium uppercase tracking-[0.24em] text-muted-foreground">
              workspace runtime
            </span>
          </span>
        </Link>
        {useCompactHeader ? null : (
          <Separator orientation="vertical" className="h-7 bg-border/70" />
        )}
        <div className="flex min-w-0 flex-1 flex-col justify-center">
          {useCompactHeader ? null : (
            <div className="text-[10px] font-medium uppercase tracking-[0.24em] text-muted-foreground">
              active thread
            </div>
          )}
          <div className="truncate text-sm text-foreground/88">{title}</div>
        </div>
        {useCompactHeader ? (
          <div className="flex shrink-0 items-center gap-2">
            {manageButton}
            <MobileToolsMenu threadId={current?.id} threadTitle={current?.name} />
          </div>
        ) : (
          <>
            {manageButton}
            <SchedulerEntryButton />
            <SitianReportEntryButton />
            <span className="inline-flex items-center gap-2.5">
              {threadWsActive ? (
                <ConnectionIndicator
                  label="Thread connection"
                  state={threadWsState}
                  latencyMs={threadWsLatencyMs}
                />
              ) : null}
              {current?.backend_kind === "claude_code" && claudeWsActive ? (
                <ConnectionIndicator
                  label="Claude connection"
                  state={claudeWsState}
                  latencyMs={claudeWsLatencyMs}
                />
              ) : null}
              <ConnectionIndicator
                label="Status connection"
                state={statusWsState}
                latencyMs={statusWsLatencyMs}
              />
            </span>
            <ThemeToggle />
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                void logout();
              }}
              aria-label="Logout"
            >
              <LogOut className="h-3.5 w-3.5" />
              退出
            </Button>
          </>
        )}
      </header>
      <main className="relative z-0 flex-1 overflow-hidden pb-3">
        <Outlet />
      </main>
      <SchedulerDrawerHost />
      <SitianReportDialog />
      <ApprovalToastQueue />
    </div>
  );
}
