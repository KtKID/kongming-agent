import { useEffect } from "react";
import type { JSX } from "react";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";

import { ThreadList } from "@/components/ThreadList";
import { useThreadsStore } from "@/stores/threads";
import { cn } from "@/lib/utils";

/**
 * 左栏总入口：显示通用对话列表和新建对话入口。
 */
interface LeftSidebarProps {
  isOpen?: boolean;
  compactMode?: boolean;
  mobileMode?: boolean;
  onToggleOpen?: () => void;
}

export function LeftSidebar({
  isOpen = true,
  compactMode = false,
  mobileMode = false,
  onToggleOpen,
}: LeftSidebarProps): JSX.Element {
  const fetchThreads = useThreadsStore((s) => s.fetchThreads);

  // mount 时无条件 fetch threads，保证刷新页面后 Chat.tsx 可拿到当前 thread 元数据。
  useEffect(() => {
    void fetchThreads();
  }, [fetchThreads]);

  return (
    <>
      {mobileMode && isOpen ? (
        <button
          type="button"
          aria-label="关闭左侧栏遮罩"
          onClick={onToggleOpen}
          className="absolute inset-0 z-10 bg-background/45 backdrop-blur-[1px]"
        />
      ) : null}
      <aside
        className={cn(
          "relative z-20 flex h-full shrink-0 flex-col transition-[width,min-width,transform] duration-300 ease-out",
          isOpen
            ? "obsidian-panel obsidian-hairline rounded-xl"
            : "absolute inset-y-0 left-0 overflow-visible",
          isOpen && (compactMode ? "absolute inset-y-0 left-0 shadow-xl" : "shadow-none"),
          isOpen
            ? mobileMode
              ? "w-[min(20rem,calc(100vw-2rem))] min-w-0 overflow-hidden"
              : compactMode
                ? "w-[min(20rem,calc(100vw-5.5rem))] min-w-0"
                : "w-80 min-w-[18rem]"
            : "w-0 min-w-0 border-0 bg-transparent shadow-none",
          mobileMode && isOpen ? "translate-x-0" : "",
        )}
      >
      {isOpen && !mobileMode ? (
        <button
          type="button"
          onClick={onToggleOpen}
          aria-label="收起左侧栏"
          className="absolute right-0 top-4 z-20 inline-flex h-8 w-8 translate-x-1/2 items-center justify-center rounded-lg border border-border/80 bg-card/90 text-foreground shadow-glass backdrop-blur-xl transition-colors hover:bg-card"
        >
          <PanelLeftClose className="h-4.5 w-4.5" />
        </button>
      ) : null}
      <div
        className={cn(
          "flex h-full flex-col transition-[opacity,transform] duration-300 ease-out",
          isOpen
            ? "translate-x-0 opacity-100"
            : "pointer-events-none -translate-x-8 opacity-0",
        )}
      >
        <div className="min-h-0 flex-1 overflow-hidden">
          <ThreadList />
        </div>
      </div>
      <div
        className={cn(
          "absolute inset-y-0 left-0 w-0 overflow-visible transition-[opacity,transform] duration-300 ease-out",
          isOpen
            ? "pointer-events-none -translate-x-6 opacity-0"
            : "translate-x-0 opacity-100",
        )}
      >
        <button
          type="button"
          onClick={onToggleOpen}
          aria-label="展开左侧栏"
          data-testid="left-edge-handle"
          className={cn(
            "pointer-events-auto absolute left-3 top-4 inline-flex h-9 w-9 items-center justify-center rounded-xl border border-border/80 bg-card/92 text-foreground shadow-glass backdrop-blur-xl transition-colors hover:bg-card",
            mobileMode ? "left-2" : "",
          )}
        >
          <PanelLeftOpen className="h-4.5 w-4.5" />
        </button>
      </div>
    </aside>
    </>
  );
}
