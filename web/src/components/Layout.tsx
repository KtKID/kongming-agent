import { useMemo } from "react";
import { Link, NavLink, Outlet, useLocation, useParams } from "react-router-dom";
import {
  ArrowLeft,
  Clock,
  LogOut,
  Monitor,
  Moon,
  PawPrint,
  Plug,
  ScrollText,
  Settings2,
  Sun,
  Telescope,
  Workflow,
} from "lucide-react";
import { ConnectionIndicator } from "@/components/ConnectionIndicator";
import { MobileToolsMenu } from "@/components/MobileToolsMenu";
import { ThemeToggle } from "@/components/ThemeToggle";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  WebShellRail,
  WebShellRailManager,
  WebShellRailProvider,
  useWebShellRailRegisteredItems,
  type WebShellRailContext,
  type WebShellRailItem,
} from "@/components/web-shell-rail";
import { ApprovalToastQueue } from "@/features/approval-inbox";
import { useChatLayout } from "@/hooks/useChatLayout";
import { useClientConfig } from "@/hooks/useClientConfig";
import { useThreadStatusWS } from "@/hooks/useThreadStatusWS";
import { cn } from "@/lib/utils";
import {
  SchedulerDrawerHost,
  SchedulerEntryButton,
  useSchedulerStore,
} from "@/modules/scheduler";
import {
  LogViewerEntryButton,
  LogViewerOverlay,
  useLogViewerStore,
} from "@/modules/logs";
import {
  SitianReportDialog,
  SitianReportEntryButton,
  useSitian,
} from "@/modules/sitian";
import { useAuthStore } from "@/stores/auth";
import { useConnectionStatusStore } from "@/stores/connectionStatus";
import { useThemeStore } from "@/stores/theme";
import { useThreadsStore } from "@/stores/threads";

export function Layout() {
  return (
    <WebShellRailProvider>
      <LayoutContent />
    </WebShellRailProvider>
  );
}

function LayoutContent() {
  const clientConfig = useClientConfig();
  useThreadStatusWS(clientConfig?.heartbeat);
  const { isCompactLayout, isMobileLayout } = useChatLayout();
  const useCompactHeader = isCompactLayout;

  const params = useParams<{ thread_id?: string }>();
  const location = useLocation();
  const threads = useThreadsStore((s) => s.threads);
  const logout = useAuthStore((s) => s.logout);
  const authenticated = useAuthStore((s) => s.authenticated);
  const openScheduler = useSchedulerStore((s) => s.openDrawer);
  const openLogs = useLogViewerStore((s) => s.open);
  const { open: openSitian, loading: sitianLoading } = useSitian();
  const theme = useThemeStore((s) => s.theme);
  const setTheme = useThemeStore((s) => s.setTheme);
  const registeredRailItems = useWebShellRailRegisteredItems();

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
  const railDensity = isMobileLayout
    ? "mobile"
    : isCompactLayout
      ? "compact"
      : "desktop";
  const railContext = useMemo<WebShellRailContext>(
    () => ({
      density: railDensity,
      activeThreadId,
      activeThreadTitle,
      hasActiveThread: Boolean(activeThreadId),
      isAuthenticated: authenticated,
      hostEnvironment: clientConfig?.hostEnvironment ?? "browser",
      capabilities: clientConfig?.capabilities ?? {
        xspaceHost: false,
        nativeFileDialog: false,
      },
    }),
    [
      activeThreadId,
      activeThreadTitle,
      authenticated,
      clientConfig?.capabilities,
      clientConfig?.hostEnvironment,
      railDensity,
    ],
  );
  const ThemeRailIcon =
    theme === "dark" ? Moon : theme === "light" ? Sun : Monitor;
  const globalRailItems = useMemo<WebShellRailItem[]>(
    () => [
      {
        id: onManagePage ? "chat" : "manage",
        scope: "global",
        priority: "p0",
        label: onManagePage ? "聊天" : "管理",
        icon: onManagePage ? ArrowLeft : Settings2,
        available: true,
        to: onManagePage ? "/chat" : "/manage",
      },
      {
        id: "thread-task-detail-route",
        scope: "thread",
        priority: "p0",
        label: "任务详情",
        icon: Workflow,
        available: Boolean(activeThreadId),
        to: activeThreadId ? `/chat/${activeThreadId}/task-detail` : undefined,
      },
      {
        id: "pet",
        scope: "global",
        priority: "p0",
        label: "宠物",
        icon: PawPrint,
        available: clientConfig?.hostEnvironment === "xspace",
      },
      {
        id: "scheduler",
        scope: "global",
        priority: "p1",
        label: "定时任务",
        icon: Clock,
        available: true,
        onSelect: openScheduler,
      },
      {
        id: "sitian",
        scope: "global",
        priority: "p1",
        label: "司天报告",
        icon: Telescope,
        available: true,
        disabledReason: sitianLoading ? "司天报告加载中" : undefined,
        onSelect: () => {
          if (!sitianLoading) void openSitian();
        },
      },
      {
        id: "logs",
        scope: "global",
        priority: "p1",
        label: "日志",
        icon: ScrollText,
        available: true,
        onSelect: openLogs,
      },
      {
        id: "theme",
        scope: "global",
        priority: "p2",
        label: "切换主题",
        icon: ThemeRailIcon,
        available: true,
        onSelect: () => {
          setTheme(theme === "system" ? "dark" : theme === "dark" ? "light" : "system");
        },
      },
      {
        id: "logout",
        scope: "global",
        priority: "p2",
        label: "退出登录",
        icon: LogOut,
        available: authenticated,
        onSelect: () => {
          void logout();
        },
      },
    ],
    [
      ThemeRailIcon,
      activeThreadId,
      authenticated,
      clientConfig?.hostEnvironment,
      logout,
      onManagePage,
      openLogs,
      openScheduler,
      openSitian,
      setTheme,
      sitianLoading,
      theme,
    ],
  );
  const railManager = useMemo(
    () =>
      new WebShellRailManager({
        context: railContext,
        items: [...globalRailItems, ...registeredRailItems],
      }),
    [globalRailItems, railContext, registeredRailItems],
  );

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
  const pluginButton = (
    <NavLink
      to="/manage/plugins"
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
        <Plug className="h-3.5 w-3.5" />
        插件
      </span>
    </NavLink>
  );

  return (
    <div className="flex h-full flex-col bg-transparent">
      <WebShellRail manager={railManager} />
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
          {pluginButton}
          <MobileToolsMenu threadId={activeThreadId} threadTitle={activeThreadTitle} />
        </div>
        ) : (
          <>
            {manageButton}
            {pluginButton}
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
      <LogViewerOverlay activeThreadId={activeThreadId ?? null} />
    </div>
  );
}
