import { Link, NavLink, Outlet, useLocation, useParams } from "react-router-dom";
import { ArrowLeft, LogOut, Settings2, Workflow } from "lucide-react";
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
  LogViewerEntryButton,
  LogViewerOverlay,
} from "@/modules/logs";
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
  const activeThreadId = current?.id ?? params.thread_id;
  const activeThreadTitle = current?.name ?? (activeThreadId ? "Current thread" : undefined);
  const onManagePage = location.pathname.startsWith("/manage");
  const title = onManagePage ? "运行管理" : activeThreadTitle ?? "kongming-agent";

  const manageButton = onManagePage ? (
    <Link
      to="/chat"
      className="inline-flex h-7 items-center gap-1 rounded-md border border-border/70 bg-card/72 px-2.5 py-1.5 text-xs font-medium text-muted-foreground shadow-sm transition-colors hover:bg-secondary hover:text-foreground"
    >
      <ArrowLeft className="h-3.5 w-3.5" />
      聊天
    </Link>
  ) : (
    <NavLink
      to="/manage"
      className={({ isActive }) =>
        cn(
          "inline-flex h-7 items-center rounded-md border px-2.5 py-1.5 text-xs font-medium shadow-sm transition-colors",
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

  const workflowButton = activeThreadId ? (
    <NavLink
      to={`/chat/${activeThreadId}/agent-workflows`}
      className={({ isActive }) =>
        cn(
          "inline-flex h-7 items-center rounded-md border px-2.5 py-1.5 text-xs font-medium shadow-sm transition-colors",
          isActive
            ? "border-accent/25 bg-accent/12 text-foreground"
            : "border-border/70 bg-card/70 text-muted-foreground hover:bg-secondary hover:text-foreground",
        )
      }
    >
      <span className="inline-flex items-center gap-1.5">
        <Workflow className="h-3.5 w-3.5" />
        Workflow
      </span>
    </NavLink>
  ) : null;

  return (
    <div className="flex h-full flex-col bg-transparent">
      <header
        className={cn(
          "obsidian-panel obsidian-hairline relative z-30 mx-2 mt-2 flex shrink-0 items-center rounded-xl",
          useCompactHeader ? "min-h-11 gap-1.5 px-2 py-2" : "h-11 gap-2 px-3",
        )}
        data-testid="app-header"
      >
        <Link
          to="/chat"
          className={cn(
            "flex min-w-0 items-center text-sm font-semibold tracking-tight text-foreground",
            useCompactHeader ? "gap-2" : "gap-2.5",
          )}
        >
          <span className="flex h-8 w-8 items-center justify-center rounded-lg border border-primary/20 bg-primary/10 shadow-sm">
            <img
              src="/favicon.png"
              alt="logo"
              className="h-5 w-5 rounded-sm object-contain"
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
          <MobileToolsMenu threadId={activeThreadId} threadTitle={activeThreadTitle} />
        </div>
        ) : (
          <>
            {manageButton}
            {workflowButton}
            <SchedulerEntryButton />
            <SitianReportEntryButton />
            <LogViewerEntryButton />
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
      <main className="relative z-0 flex-1 overflow-hidden pb-2">
        <Outlet />
      </main>
      <SchedulerDrawerHost />
      <SitianReportDialog />
      <ApprovalToastQueue />
      <LogViewerOverlay />
    </div>
  );
}
