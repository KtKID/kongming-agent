import { NavLink, Outlet } from "react-router-dom";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";

// 竖 tab 设计来自 manage-config-tab task：左侧「配置」/「模型服务商」/「网络」+ 右侧 Outlet。
// 「网络」对应原 /manage/runtime-status 页面（破坏性更名为 /manage/network，无兼容 alias）；
// 「配置」走 /manage/config → /manage/config/model 兜底。路由表见 lib/router.tsx。
const TABS: ReadonlyArray<{ to: string; label: string }> = [
  { to: "/manage/config", label: "配置" },
  { to: "/manage/model-providers", label: "模型服务商" },
  { to: "/manage/network", label: "网络" },
];

export function ManagePage() {
  return (
    <ScrollArea className="h-full">
      <div className="mx-auto max-w-5xl p-6">
        <div className="mb-5">
          <h2 className="text-lg font-semibold tracking-tight text-foreground">
            运行管理
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            统一观察进程、cell、连接和活跃 session。
          </p>
        </div>
        <div className="flex gap-5">
          <nav
            role="tablist"
            aria-label="运行管理分组"
            className="flex w-44 shrink-0 flex-col gap-1 rounded-2xl border border-border/70 bg-card/70 p-2 shadow-sm"
          >
            {TABS.map((tab) => (
              <NavLink
                key={tab.to}
                to={tab.to}
                role="tab"
                className={({ isActive }) =>
                  cn(
                    "rounded-xl px-3 py-2 text-xs font-medium transition-colors",
                    isActive
                      ? "bg-primary/12 text-foreground"
                      : "text-muted-foreground hover:bg-secondary hover:text-foreground",
                  )
                }
              >
                {tab.label}
              </NavLink>
            ))}
          </nav>
          <div className="min-w-0 flex-1">
            <Outlet />
          </div>
        </div>
      </div>
    </ScrollArea>
  );
}
