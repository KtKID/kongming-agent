import { useEffect, useMemo, useRef, useState } from "react";
import type { JSX, ReactNode } from "react";
import {
  ChevronDown,
  ChevronRight,
  Folder,
  FolderOpen,
  Plus,
  RefreshCw,
  Search,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ProjectSessionBrowserProps<TProject, TSession> {
  /** 项目列表（null = 未加载） */
  projects: TProject[] | null;
  /** 项目唯一 key */
  projectKey: (p: TProject) => string;
  /** 项目显示名 */
  projectLabel: (p: TProject) => string;
  /** 项目 tooltip（如 cwd 路径） */
  projectTitle?: (p: TProject) => string;
  /** 项目下 session 数量 */
  projectSessionCount: (p: TProject) => number;
  /** 获取项目下的 session 列表 */
  sessions: (p: TProject) => TSession[];
  /** session 唯一 key */
  sessionKey: (s: TSession) => string;
  /** session 搜索匹配文本列表 */
  sessionSearchTexts?: (s: TSession) => string[];
  /** 渲染单个 session 行（由频道 adapter 负责，调 SidebarSessionRow） */
  renderSessionRow: (project: TProject, session: TSession) => ReactNode;
  /** 刷新回调 */
  onRefresh: () => Promise<void>;
  /** 新建会话回调（如果提供，在 project 展开后显示"新建会话"按钮） */
  onNewSession?: (project: TProject) => void;
  /** 是否正在刷新 */
  isRefreshing?: boolean;
  /** 刷新进度（可选） */
  refreshProgress?: { current: number; total: number; current_project: string } | null;
  /** 错误信息 */
  error?: string | null;
  /** 搜索框 placeholder */
  searchPlaceholder?: string;
  /** 搜索框 aria-label */
  searchAriaLabel?: string;
  /** 空状态提示文案 */
  emptyText?: string;
  /** 搜索无结果文案 */
  noMatchText?: string;
  /** 默认显示 session 数 */
  defaultVisible?: number;
  /** 刷新按钮文案 */
  refreshLabel?: string;
  /** 外部触发刷新 key（变化时自动刷新） */
  externalRefreshKey?: number;
  /** 项目 header 右侧自定义 actions（如"移除"按钮）。点击不会触发展开。 */
  renderProjectActions?: (project: TProject) => ReactNode;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const ONE_WEEK_SEC = 7 * 86400;

function buildRecentSet<T>(items: T[], key: (t: T) => string, lastModified: (t: T) => number | undefined): Set<string> {
  const recent = new Set<string>();
  const oneWeekAgo = Date.now() / 1000 - ONE_WEEK_SEC;
  for (const item of items) {
    const lm = lastModified(item);
    if (lm !== undefined && lm > oneWeekAgo) {
      recent.add(key(item));
    }
  }
  return recent;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ProjectSessionBrowser<TProject, TSession>({
  projects,
  projectKey,
  projectLabel,
  projectTitle,
  projectSessionCount,
  sessions: getSessions,
  sessionKey: _sessionKey,
  sessionSearchTexts,
  renderSessionRow,
  onRefresh,
  onNewSession,
  isRefreshing = false,
  refreshProgress,
  error,
  searchPlaceholder = "搜索项目或 session",
  searchAriaLabel = "搜索历史会话",
  emptyText = "尚无历史会话",
  noMatchText = "没有匹配的历史会话",
  defaultVisible = 6,
  refreshLabel = "重新加载",
  externalRefreshKey,
  renderProjectActions,
}: ProjectSessionBrowserProps<TProject, TSession>): JSX.Element {
  const [expandedProjects, setExpandedProjects] = useState<Set<string>>(new Set());
  const [showAllSessions, setShowAllSessions] = useState<Set<string>>(new Set());
  const [searchQuery, setSearchQuery] = useState("");
  const didAutoRefreshRef = useRef(false);

  // 项目列表变化时，保留已展开 + 自动展开最近活跃
  useEffect(() => {
    if (!projects) return;
    const recent = buildRecentSet(
      projects,
      projectKey,
      (p) => {
        const ss = getSessions(p);
        return ss.length > 0 ? (ss[0] as Record<string, unknown>)?.last_modified as number | undefined : undefined;
      },
    );
    setExpandedProjects((current) => {
      const next = new Set<string>();
      for (const p of projects) {
        const k = projectKey(p);
        if (current.has(k) || recent.has(k)) next.add(k);
      }
      return next;
    });
    setShowAllSessions((current) => {
      const next = new Set<string>();
      for (const p of projects) {
        const k = projectKey(p);
        if (current.has(k)) next.add(k);
      }
      return next;
    });
  }, [projects, projectKey, getSessions]);

  // 外部触发刷新
  useEffect(() => {
    if (externalRefreshKey && externalRefreshKey > 0) {
      void onRefresh();
    }
  }, [externalRefreshKey]);

  // mount 时初始加载
  useEffect(() => {
    if (projects !== null || didAutoRefreshRef.current) return;
    didAutoRefreshRef.current = true;
    void onRefresh();
  }, [onRefresh, projects]);

  const toggleExpand = (key: string) => {
    setExpandedProjects((s) => {
      const next = new Set(s);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const showAll = (key: string) => {
    setShowAllSessions((s) => new Set(s).add(key));
  };

  const expandAll = () => {
    if (!projects) return;
    const all = new Set(projects.map(projectKey));
    setExpandedProjects(all);
    setShowAllSessions(all);
  };

  const collapseAll = () => {
    setExpandedProjects(new Set());
    setShowAllSessions(new Set());
  };

  // 搜索过滤
  const normalizedQuery = searchQuery.trim().toLowerCase();
  const filteredProjects = useMemo(() => {
    if (!projects) return null;
    if (!normalizedQuery) return projects;
    return projects.flatMap((project) => {
      const pKey = projectKey(project);
      const pLabel = projectLabel(project);
      const pTitle = projectTitle?.(project) ?? "";
      const projectMatched = [pLabel, pKey, pTitle].some(
        (v) => v.toLowerCase().includes(normalizedQuery),
      );
      const ss = getSessions(project);
      const matchedSessions = sessionSearchTexts
        ? ss.filter((s) =>
            sessionSearchTexts(s).some((v) => v.toLowerCase().includes(normalizedQuery)),
          )
        : ss;
      if (!projectMatched && matchedSessions.length === 0) return [];
      return [{
        ...project,
        _filteredSessions: matchedSessions.length > 0 || !projectMatched ? matchedSessions : ss,
      }] as Array<TProject & { _filteredSessions?: TSession[] }>;
    });
  }, [normalizedQuery, projects, projectKey, projectLabel, projectTitle, getSessions, sessionSearchTexts]);

  const searchExpandedProjects = useMemo(
    () => new Set(filteredProjects?.map((p) => projectKey(p)) ?? []),
    [filteredProjects, projectKey],
  );

  return (
    <div className="flex h-full min-h-0 flex-col text-sm">
      {/* 工具栏 */}
      <div className="border-b px-3 py-3">
        <div className="mb-2 flex items-center gap-2">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => void onRefresh()}
            disabled={isRefreshing}
            className="h-8 rounded-lg px-3"
          >
            <RefreshCw className={isRefreshing ? "h-3.5 w-3.5 animate-spin" : "h-3.5 w-3.5"} />
            {refreshLabel}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={expandAll}
            disabled={!projects || projects.length === 0}
            className="h-8 rounded-lg px-3"
          >
            全部展开
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={collapseAll}
            disabled={!projects || projects.length === 0}
            className="h-8 rounded-lg px-3"
          >
            全部收起
          </Button>
        </div>
        <div className="relative">
          <Search className="text-muted-foreground pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2" />
          <Input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder={searchPlaceholder}
            aria-label={searchAriaLabel}
            className="h-9 rounded-xl pl-9 pr-9"
          />
          {searchQuery && (
            <button
              type="button"
              onClick={() => setSearchQuery("")}
              aria-label="清空搜索"
              className="text-muted-foreground hover:bg-accent hover:text-foreground absolute right-2 top-1/2 -translate-y-1/2 rounded-md p-1 transition-colors"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>

      {/* 刷新进度 */}
      {isRefreshing && refreshProgress && (
        <div className="border-b px-3 py-2">
          <div className="mb-1 flex items-center justify-between text-xs">
            <span className="text-muted-foreground truncate">
              扫描：{refreshProgress.current_project}
            </span>
            <span className="text-muted-foreground shrink-0">
              {refreshProgress.current}/{refreshProgress.total}
            </span>
          </div>
          <div className="h-1 overflow-hidden rounded-full bg-muted">
            <div
              className="h-full rounded-full bg-primary transition-[width] duration-200"
              style={{ width: `${(refreshProgress.current / Math.max(refreshProgress.total, 1)) * 100}%` }}
            />
          </div>
        </div>
      )}

      {/* 项目树 */}
      <div className="flex min-h-0 flex-col gap-1 overflow-y-auto p-2">
        {isRefreshing && !refreshProgress && (
          <div className="text-muted-foreground px-2 py-1">
            <RefreshCw className="mr-1.5 inline h-3.5 w-3.5 animate-spin" />
            正在扫描...
          </div>
        )}
        {projects === null && !error && !isRefreshing && (
          <div className="text-muted-foreground px-2 py-1">加载中...</div>
        )}
        {error && (
          <div className="text-destructive px-2 py-1">加载失败：{error}</div>
        )}
        {projects?.length === 0 && (
          <div className="text-muted-foreground px-2 py-1">{emptyText}</div>
        )}
        {filteredProjects?.length === 0 && normalizedQuery && (
          <div className="text-muted-foreground px-2 py-1">{noMatchText}</div>
        )}
        {filteredProjects?.map((project) => {
          const key = projectKey(project);
          const expanded = normalizedQuery
            ? searchExpandedProjects.has(key)
            : expandedProjects.has(key);
          const ss = (project as TProject & { _filteredSessions?: TSession[] })._filteredSessions
            ?? getSessions(project);
          const visibleSessions = expanded
            ? normalizedQuery || showAllSessions.has(key)
              ? ss
              : ss.slice(0, defaultVisible)
            : [];
          const hasMore = !normalizedQuery && ss.length > defaultVisible;

          return (
            <div key={key} className="flex flex-col">
              <button
                type="button"
                onClick={() => toggleExpand(key)}
                title={projectTitle?.(project) ?? projectLabel(project)}
                className="hover:bg-accent flex items-center gap-1.5 rounded px-2 py-1 text-left transition-colors"
              >
                {expanded ? (
                  <ChevronDown className="text-muted-foreground h-3.5 w-3.5 shrink-0" />
                ) : (
                  <ChevronRight className="text-muted-foreground h-3.5 w-3.5 shrink-0" />
                )}
                {expanded ? (
                  <FolderOpen className="text-muted-foreground h-4 w-4 shrink-0" />
                ) : (
                  <Folder className="text-muted-foreground h-4 w-4 shrink-0" />
                )}
                <span className="truncate">{projectLabel(project)}</span>
                <span className="text-muted-foreground ml-auto shrink-0 text-xs">
                  {projectSessionCount(project)}
                </span>
                {renderProjectActions && (
                  <span
                    className="ml-1 flex shrink-0 items-center"
                    onClick={(e) => e.stopPropagation()}
                  >
                    {renderProjectActions(project)}
                  </span>
                )}
              </button>
              {expanded && (
                <div className="ml-4 flex flex-col gap-0.5 border-l pl-2">
                  {onNewSession && (
                    <button
                      type="button"
                      onClick={() => onNewSession(project)}
                      className="hover:bg-accent flex items-center gap-1.5 rounded px-2 py-1 text-left text-xs font-medium text-primary transition-colors"
                    >
                      <Plus className="h-3.5 w-3.5 shrink-0" />
                      新建会话
                    </button>
                  )}
                  {visibleSessions.map((s) => renderSessionRow(project, s))}
                  {expanded && !showAllSessions.has(key) && hasMore && (
                    <button
                      type="button"
                      onClick={() => showAll(key)}
                      className="text-muted-foreground hover:text-foreground hover:bg-accent rounded px-2 py-1 text-left text-xs transition-colors"
                    >
                      ▽ 显示更多（共 {ss.length} 条）
                    </button>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
