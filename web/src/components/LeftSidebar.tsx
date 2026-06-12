import { useEffect } from "react";
import type { JSX } from "react";
import { ChevronRight, PanelLeftClose } from "lucide-react";

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
        "obsidian-panel obsidian-hairline relative z-20 flex h-full shrink-0 flex-col rounded-xl transition-[width,min-width,transform] duration-300 ease-out",
        compactMode ? "absolute inset-y-0 left-0 shadow-xl" : "shadow-none",
        isOpen
          ? mobileMode
            ? "w-[min(20rem,calc(100vw-2rem))] min-w-0 overflow-hidden"
            : compactMode
            ? "w-[min(20rem,calc(100vw-5.5rem))] min-w-0"
            : "w-80 min-w-[18rem]"
          : mobileMode
            ? "w-0 min-w-0 overflow-visible border-r-0"
            : "w-[4.5rem] min-w-[4.5rem] overflow-hidden",
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
          "absolute inset-0 transition-[opacity,transform] duration-300 ease-out",
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
            "inline-flex items-center justify-center border border-border/80 bg-card/90 text-foreground shadow-glass backdrop-blur-xl transition-colors hover:bg-card",
            mobileMode
              ? "absolute left-0 top-4 h-16 w-5 rounded-r-lg border-l-0"
              : "mt-4 ml-2 h-8 w-8 rounded-lg",
          )}
        >
          <ChevronRight className={cn(mobileMode ? "h-4 w-4" : "h-4.5 w-4.5")} />
        </button>
      </div>
    </aside>
    </>
  );
}
