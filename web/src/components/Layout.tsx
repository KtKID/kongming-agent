import { Link, NavLink, Outlet, useLocation, useParams } from "react-router-dom";
import { ArrowLeft, LogOut, Settings2 } from "lucide-react";
import { ThemeToggle } from "@/components/ThemeToggle";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { useAuthStore } from "@/stores/auth";
import { useThreadsStore } from "@/stores/threads";
import { cn } from "@/lib/utils";

/**
 * 全局壳：顶部 nav 显示 logo + 当前 thread 标题 + 入口按钮。
 * <Outlet /> 渲染子路由。
 */
export function Layout() {
  const params = useParams<{ thread_id?: string }>();
  const location = useLocation();
  const threads = useThreadsStore((s) => s.threads);
  const logout = useAuthStore((s) => s.logout);

  const current = params.thread_id
    ? threads.find((t) => t.id === params.thread_id)
    : undefined;

  const onManagePage = location.pathname.startsWith("/manage");
  const title = onManagePage ? "运行管理" : current?.name ?? "kongming-agent";

  return (
    <div className="flex h-full flex-col bg-background">
      <header className="flex h-13 shrink-0 items-center gap-3 border-b border-border px-4">
        <Link
          to="/chat"
          className="flex items-center gap-2 text-sm font-semibold tracking-tight"
        >
          <img src="/favicon.png" alt="logo" className="h-6 w-6 rounded-md object-contain" />
          <span>kongming</span>
        </Link>
        <Separator orientation="vertical" className="h-6" />
        <div className="flex-1 truncate text-sm text-muted-foreground">
          {title}
        </div>
        {onManagePage ? (
          <Link
            to="/chat"
            className="inline-flex items-center gap-1 rounded-md px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-secondary"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            聊天
          </Link>
        ) : null}
        <NavLink
          to="/manage"
          className={({ isActive }) =>
            cn(
              "rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
              isActive
                ? "bg-accent text-accent-foreground"
                : "text-muted-foreground hover:bg-secondary",
            )
          }
        >
          <span className="inline-flex items-center gap-1.5">
            <Settings2 className="h-3.5 w-3.5" />
            管理
          </span>
        </NavLink>
        <ThemeToggle />
        <Button
          variant="ghost"
          size="sm"
          onClick={() => {
            void logout();
          }}
          aria-label="退出登录"
        >
          <LogOut className="h-3.5 w-3.5" />
          退出
        </Button>
      </header>
      <main className="flex-1 overflow-hidden">
        <Outlet />
      </main>
    </div>
  );
}
