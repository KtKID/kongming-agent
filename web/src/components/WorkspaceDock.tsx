import { useCallback, useEffect, useMemo, useRef, useState, type JSX, type ReactNode } from "react";
import { FileCode2, GitBranch, MessageSquare, Monitor, PanelsTopLeft } from "lucide-react";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";

// WorkspaceDock 负责右侧工作区页签、当前面板渲染和桌面端宽度拖拽持久化。
// Chat.tsx 集成时只需持有 activeTab 状态并传入各页 ReactNode，中间主对话保持常驻。

export type WorkspaceDockTab = "chat" | "files" | "git" | "shell" | "whiteboard";

export interface WorkspaceDockThreadMeta {
  id?: string;
  title?: string | null;
  status?: string | null;
}

export interface WorkspaceDockProps {
  activeTab: WorkspaceDockTab;
  onTabChange: (tab: WorkspaceDockTab) => void;
  thread?: WorkspaceDockThreadMeta | null;
  workspaceRoot?: string | null;
  chatContent?: ReactNode;
  filesContent?: ReactNode;
  gitContent?: ReactNode;
  shellContent?: ReactNode;
  whiteboardContent?: ReactNode;
  mobile?: boolean;
  compact?: boolean;
  className?: string;
}

export const WORKSPACE_DOCK_WIDTH_KEY = "kongming.workspaceDock.width";
export const WORKSPACE_DOCK_DEFAULT_WIDTH = 360;
export const WORKSPACE_DOCK_MIN_WIDTH = 300;
export const WORKSPACE_DOCK_MAX_WIDTH = 560;

const TABS: Array<{
  value: WorkspaceDockTab;
  label: string;
  icon: typeof MessageSquare;
}> = [
  { value: "chat", label: "Chat", icon: MessageSquare },
  { value: "files", label: "Files", icon: FileCode2 },
  { value: "git", label: "Git", icon: GitBranch },
  { value: "shell", label: "Shell", icon: Monitor },
  { value: "whiteboard", label: "Whiteboard", icon: PanelsTopLeft },
];

// 读取持久化宽度；localStorage 缺失、不可读或值越界时统一回退默认宽度。
function loadWorkspaceDockWidth(): number {
  if (typeof window === "undefined") return WORKSPACE_DOCK_DEFAULT_WIDTH;
  try {
    const raw = window.localStorage.getItem(WORKSPACE_DOCK_WIDTH_KEY);
    if (raw == null) return WORKSPACE_DOCK_DEFAULT_WIDTH;
    const parsed = Number.parseFloat(raw);
    if (
      !Number.isFinite(parsed) ||
      parsed < WORKSPACE_DOCK_MIN_WIDTH ||
      parsed > WORKSPACE_DOCK_MAX_WIDTH
    ) {
      return WORKSPACE_DOCK_DEFAULT_WIDTH;
    }
    return Math.round(parsed);
  } catch {
    return WORKSPACE_DOCK_DEFAULT_WIDTH;
  }
}

// 将拖拽得到的宽度压到 Dock 允许范围内，避免内容区域被拖到不可用尺寸。
function clampWorkspaceDockWidth(width: number): number {
  if (!Number.isFinite(width)) return WORKSPACE_DOCK_DEFAULT_WIDTH;
  return Math.max(
    WORKSPACE_DOCK_MIN_WIDTH,
    Math.min(WORKSPACE_DOCK_MAX_WIDTH, Math.round(width)),
  );
}

// 写入宽度时吞掉浏览器隐私模式、配额满或测试环境 storage stub 的异常。
function persistWorkspaceDockWidth(width: number): void {
  if (typeof window === "undefined" || !Number.isFinite(width)) return;
  try {
    window.localStorage.setItem(WORKSPACE_DOCK_WIDTH_KEY, String(Math.round(width)));
  } catch {
    // localStorage 失败不影响当前 UI 状态。
  }
}

function DefaultChatContext({
  thread,
  workspaceRoot,
}: {
  thread?: WorkspaceDockThreadMeta | null;
  workspaceRoot?: string | null;
}): JSX.Element {
  return (
    <div className="space-y-3 rounded-lg border border-border/70 bg-background/45 p-3 text-sm">
      <div className="font-medium text-foreground">Thread Context</div>
      <div className="space-y-2 text-muted-foreground">
        <div className="truncate">
          <span className="text-foreground/80">Thread:</span>{" "}
          {thread?.title || thread?.id || "Current thread"}
        </div>
        {workspaceRoot ? (
          <div className="truncate" title={workspaceRoot}>
            <span className="text-foreground/80">Workspace:</span> {workspaceRoot}
          </div>
        ) : null}
        {thread?.status ? (
          <div className="truncate">
            <span className="text-foreground/80">Status:</span> {thread.status}
          </div>
        ) : null}
      </div>
    </div>
  );
}

export function WorkspaceDock({
  activeTab,
  onTabChange,
  thread,
  workspaceRoot,
  chatContent,
  filesContent,
  gitContent,
  shellContent,
  whiteboardContent,
  mobile = false,
  compact = false,
  className,
}: WorkspaceDockProps): JSX.Element {
  const [width, setWidth] = useState(loadWorkspaceDockWidth);
  const [isResizing, setIsResizing] = useState(false);
  const dragStateRef = useRef<{
    pointerId: number;
    startClientX: number;
    startWidth: number;
  } | null>(null);
  const resizeEnabled = !mobile && !compact;

  const contentByTab = useMemo<Record<WorkspaceDockTab, ReactNode>>(
    () => ({
      chat: chatContent ?? <DefaultChatContext thread={thread} workspaceRoot={workspaceRoot} />,
      files: filesContent ?? null,
      git: gitContent ?? null,
      shell: shellContent ?? null,
      whiteboard: whiteboardContent ?? null,
    }),
    [
      chatContent,
      filesContent,
      gitContent,
      shellContent,
      thread,
      whiteboardContent,
      workspaceRoot,
    ],
  );

  useEffect(() => {
    if (!isResizing) return;

    const onPointerMove = (event: PointerEvent) => {
      const drag = dragStateRef.current;
      if (!drag || drag.pointerId !== event.pointerId) return;
      const deltaX = drag.startClientX - event.clientX;
      setWidth(clampWorkspaceDockWidth(drag.startWidth + deltaX));
    };

    const stopResize = (event: PointerEvent) => {
      const drag = dragStateRef.current;
      if (!drag || drag.pointerId !== event.pointerId) return;
      dragStateRef.current = null;
      setIsResizing(false);
      setWidth((current) => {
        persistWorkspaceDockWidth(current);
        return current;
      });
    };

    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", stopResize);
    window.addEventListener("pointercancel", stopResize);
    return () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", stopResize);
      window.removeEventListener("pointercancel", stopResize);
    };
  }, [isResizing]);

  // 从左边缘开始拖拽，鼠标向左移动时右侧 Dock 变宽，鼠标向右移动时变窄。
  const startResize = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      if (!resizeEnabled) return;
      event.preventDefault();
      event.stopPropagation();
      dragStateRef.current = {
        pointerId: event.pointerId,
        startClientX: event.clientX,
        startWidth: width,
      };
      setIsResizing(true);
    },
    [resizeEnabled, width],
  );

  return (
    <aside
      data-testid="workspace-dock"
      className={cn(
        "obsidian-panel obsidian-hairline relative flex h-full min-h-0 shrink-0 flex-col overflow-hidden border-l border-border/70 bg-card/70",
        mobile || compact ? "w-full min-w-0" : "min-w-[300px] max-w-[560px]",
        className,
      )}
      style={resizeEnabled ? { width: `${width}px` } : undefined}
    >
      {resizeEnabled ? (
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize workspace panel"
          data-testid="workspace-dock-resize-handle"
          data-resizing={isResizing ? "true" : "false"}
          onPointerDown={startResize}
          className={cn(
            "absolute left-0 top-0 bottom-0 z-20 w-1 cursor-col-resize touch-none transition-colors",
            isResizing ? "bg-primary/60" : "bg-transparent hover:bg-primary/35",
          )}
        />
      ) : null}
      <Tabs
        value={activeTab}
        onValueChange={(value) => onTabChange(value as WorkspaceDockTab)}
        className="flex min-h-0 flex-1 flex-col"
      >
        <div className="border-b border-border/70 px-3 py-2">
          <TabsList className="grid h-9 w-full grid-cols-5">
            {TABS.map((tab) => {
              const Icon = tab.icon;
              return (
                <TabsTrigger key={tab.value} value={tab.value} className="gap-1 px-1.5 text-xs">
                  <Icon className="h-3.5 w-3.5 shrink-0" />
                  <span className="truncate">{tab.label}</span>
                </TabsTrigger>
              );
            })}
          </TabsList>
        </div>
        <div
          data-testid="workspace-dock-content"
          className="min-h-0 flex-1 overflow-auto p-3"
        >
          {contentByTab[activeTab]}
        </div>
      </Tabs>
    </aside>
  );
}
