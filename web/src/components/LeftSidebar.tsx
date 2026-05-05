import { useEffect, useState } from "react";
import type { JSX } from "react";
import { ChevronRight, PanelLeftClose } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";

import { ThreadList } from "@/components/ThreadList";
import {
  LeftSidebarTabs,
  loadPersistedTab,
  persistTab,
  type LeftSidebarTab,
} from "@/components/LeftSidebarTabs";
import { ClaudeProjectsTree } from "@/components/ClaudeProjectsTree";
import { apiPost } from "@/lib/api";
import { useThreadsStore } from "@/stores/threads";
import type {
  ClaudeProjectSummaryDTO,
  ClaudeSessionSummaryDTO,
  ImportClaudeSessionRequest,
  ImportClaudeSessionResponse,
} from "@/protocol";
import { cn } from "@/lib/utils";

/**
 * 左栏总入口：通用（ThreadList）/ Claude（ClaudeProjectsTree）双 Tab。
 *
 * - tab 选中态走 localStorage 持久化（loadPersistedTab / persistTab）
 * - Claude tab 内点击 session 卡片 → POST /api/threads/import-claude-session
 *   → 拿到 thread → 刷新 threads store → navigate /chat/{thread.id}
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
  const [tab, setTab] = useState<LeftSidebarTab>(() => loadPersistedTab());
  const navigate = useNavigate();
  const fetchThreads = useThreadsStore((s) => s.fetchThreads);

  // mount 时无条件 fetch threads —— Claude tab 下 ThreadList 不渲染时也要拿
  // 数据，否则 Chat.tsx 在刷新页面后找不到当前 thread 的 backend_kind，
  // 会回退 generic_chat 走错 ws endpoint（被后端 403）。
  useEffect(() => {
    void fetchThreads();
  }, [fetchThreads]);

  const handleTabChange = (next: LeftSidebarTab): void => {
    setTab(next);
    persistTab(next);
  };

  const handleSessionClick = async (
    project: ClaudeProjectSummaryDTO,
    session: ClaudeSessionSummaryDTO,
  ): Promise<void> => {
    try {
      const body: ImportClaudeSessionRequest = {
        sdk_session_id: session.sdk_session_id,
        cwd: project.cwd,
        name: session.title,
      };
      const resp = await apiPost<ImportClaudeSessionResponse>(
        "/api/threads/import-claude-session",
        body,
      );
      // 刷新 thread 列表（让 generic tab 看到新 thread；同时 useThreadsStore 缓存有新条目）
      await fetchThreads();
      navigate(`/chat/${resp.thread.id}`);
      if (!resp.imported) {
        toast.info("已打开已绑定的会话");
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`导入会话失败：${msg}`);
    }
  };

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
        "relative z-20 flex h-full shrink-0 flex-col border-r border-border bg-card transition-[width,min-width,transform] duration-300 ease-out",
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
          className="absolute right-0 top-5 z-20 inline-flex h-11 w-11 translate-x-1/2 items-center justify-center rounded-[1.3rem] border border-border/80 bg-background/92 text-foreground shadow-sm backdrop-blur-sm transition-colors hover:bg-card"
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
        <div className="border-b border-border p-2 pr-8">
          <LeftSidebarTabs active={tab} onChange={handleTabChange} />
        </div>
        <div className="min-h-0 flex-1 overflow-hidden">
          {tab === "generic" ? (
            <ThreadList />
          ) : (
            <ClaudeProjectsTree onSessionClick={handleSessionClick} />
          )}
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
            "inline-flex items-center justify-center border border-border/80 bg-card/88 text-foreground shadow-sm backdrop-blur-sm transition-colors hover:bg-card",
            mobileMode
              ? "absolute left-0 top-5 h-20 w-6 rounded-r-2xl border-l-0"
              : "mt-5 ml-2 h-12 w-12 rounded-[1.4rem]",
          )}
        >
          <ChevronRight className={cn(mobileMode ? "h-4 w-4" : "h-4.5 w-4.5")} />
        </button>
      </div>
    </aside>
    </>
  );
}
