import { useEffect, useMemo, useState } from "react";
import type { JSX } from "react";
import {
  ChevronDown,
  ChevronRight,
  Folder,
  FolderOpen,
  MessageSquare,
  Plus,
  RefreshCw,
  Search,
  X,
} from "lucide-react";

import { apiRefreshClaudeProjects } from "@/lib/api";
import { useThreadsStore } from "@/stores/threads";
import { formatRelative } from "@/lib/relative-time";
import type {
  ClaudeProjectSummaryDTO,
  ClaudeProjectsRefreshProgressDTO,
  ClaudeSessionSummaryDTO,
} from "@/protocol";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/**
 * Claude 历史项目树（左栏 Claude tab 的主体）。
 *
 * - mount 时拉 `GET /api/claude/projects`
 * - 自管展开 / 折叠 / "显示更多"三个 set 状态
 * - 默认展开"最近一周内"有活动的项目
 * - 点击 session 卡片仅向上 callback（具体跳转逻辑由 #12 阶段在 Chat.tsx 里实现）
 */
interface ClaudeProjectsTreeProps {
  onSessionClick: (
    project: ClaudeProjectSummaryDTO,
    session: ClaudeSessionSummaryDTO,
  ) => void;
  onNewSession: (project: ClaudeProjectSummaryDTO) => void;
}

const DEFAULT_VISIBLE = 6;
const ONE_WEEK_SEC = 7 * 86400;
const MIN_REFRESH_PROGRESS_MS = 350;

function buildRecentProjectsSet(
  projects: ClaudeProjectSummaryDTO[],
): Set<string> {
  const recent = new Set<string>();
  const oneWeekAgo = Date.now() / 1000 - ONE_WEEK_SEC;
  for (const project of projects) {
    const latest = project.sessions[0]?.last_modified;
    if (latest !== undefined && latest > oneWeekAgo) {
      recent.add(project.name);
    }
  }
  return recent;
}

function formatCurrentProject(encodedName: string): string {
  const segments = encodedName.split("-").filter(Boolean);
  return segments.length >= 2
    ? `${segments[segments.length - 2]}/${segments[segments.length - 1]}`
    : encodedName;
}

export function ClaudeProjectsTree({
  onSessionClick,
  onNewSession,
}: ClaudeProjectsTreeProps): JSX.Element {
  const [projects, setProjects] = useState<ClaudeProjectSummaryDTO[] | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const [expandedProjects, setExpandedProjects] = useState<Set<string>>(
    new Set(),
  );
  const [showAllSessions, setShowAllSessions] = useState<Set<string>>(
    new Set(),
  );
  const [searchQuery, setSearchQuery] = useState("");
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [refreshProgress, setRefreshProgress] =
    useState<ClaudeProjectsRefreshProgressDTO | null>(null);

  const applyProjects = (nextProjects: ClaudeProjectSummaryDTO[]): void => {
    setProjects(nextProjects);
    const recent = buildRecentProjectsSet(nextProjects);
    setExpandedProjects((current) => {
      const next = new Set<string>();
      for (const project of nextProjects) {
        if (current.has(project.name) || recent.has(project.name)) {
          next.add(project.name);
        }
      }
      return next;
    });
    setShowAllSessions((current) => {
      const next = new Set<string>();
      for (const project of nextProjects) {
        if (current.has(project.name)) {
          next.add(project.name);
        }
      }
      return next;
    });
  };

  const toggleExpand = (name: string): void => {
    setExpandedProjects((s) => {
      const next = new Set(s);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const showAll = (name: string): void => {
    setShowAllSessions((s) => new Set(s).add(name));
  };

  const expandAll = (): void => {
    if (!projects) return;
    const all = new Set(projects.map((project) => project.name));
    setExpandedProjects(all);
    setShowAllSessions(all);
  };

  const collapseAll = (): void => {
    setExpandedProjects(new Set());
    setShowAllSessions(new Set());
  };

  const refreshProjects = async (): Promise<void> => {
    const startedAt = Date.now();
    setIsRefreshing(true);
    setRefreshProgress(null);
    setError(null);
    try {
      const result = await apiRefreshClaudeProjects((progress) => {
        setRefreshProgress(progress);
      });
      const elapsed = Date.now() - startedAt;
      if (elapsed < MIN_REFRESH_PROGRESS_MS) {
        await new Promise((resolve) =>
          setTimeout(resolve, MIN_REFRESH_PROGRESS_MS - elapsed),
        );
      }
      applyProjects(result.projects);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsRefreshing(false);
      setRefreshProgress(null);
    }
  };

  useEffect(() => {
    void refreshProjects();
  }, []);

  const claudeProjectsRefreshKey = useThreadsStore(
    (s) => s.claudeProjectsRefreshKey,
  );
  useEffect(() => {
    if (claudeProjectsRefreshKey > 0) {
      void refreshProjects();
    }
  }, [claudeProjectsRefreshKey]);

  const normalizedQuery = searchQuery.trim().toLowerCase();
  const filteredProjects = useMemo(() => {
    if (!projects) return null;
    if (!normalizedQuery) return projects;

    return projects.flatMap((project) => {
      const projectMatched = [
        project.display_name,
        project.name,
        project.cwd,
      ].some((value) => value.toLowerCase().includes(normalizedQuery));

      const matchedSessions = project.sessions.filter((session) =>
        [session.title, session.claude_thread_id].some((value) =>
          value.toLowerCase().includes(normalizedQuery),
        ),
      );

      if (!projectMatched && matchedSessions.length === 0) {
        return [];
      }

      return [
        {
          ...project,
          sessions:
            matchedSessions.length > 0 || !projectMatched
              ? matchedSessions
              : project.sessions,
        },
      ];
    });
  }, [normalizedQuery, projects]);

  const searchExpandedProjects = useMemo(
    () => new Set(filteredProjects?.map((project) => project.name) ?? []),
    [filteredProjects],
  );

  return (
    <div className="flex h-full min-h-0 flex-col text-sm">
      <div className="border-b px-3 py-3">
        <div className="mb-2 flex items-center gap-2">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              void refreshProjects();
            }}
            disabled={isRefreshing}
            className="h-8 rounded-lg px-3"
          >
            <RefreshCw
              className={isRefreshing ? "h-3.5 w-3.5 animate-spin" : "h-3.5 w-3.5"}
            />
            重新加载
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
            onChange={(event) => setSearchQuery(event.target.value)}
            placeholder="搜索项目或 session"
            aria-label="搜索 Claude 历史会话"
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

      <div className="flex min-h-0 flex-col gap-1 overflow-y-auto p-2">
        {isRefreshing && (
          <div className="mb-2 rounded-2xl border bg-card/95 px-4 py-4 shadow-sm">
            <div className="mb-3 flex items-center gap-3">
              <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-muted">
                <RefreshCw className="text-muted-foreground h-5 w-5 animate-spin" />
              </div>
              <div className="min-w-0">
                <div className="text-sm font-semibold">加载项目中...</div>
                <div className="text-muted-foreground text-xs">
                  正在重新扫描 Claude 历史项目
                </div>
              </div>
            </div>
            {refreshProgress && refreshProgress.total > 0 ? (
              <div className="space-y-2">
                <div className="h-2 overflow-hidden rounded-full bg-muted">
                  <div
                    className="h-full bg-primary transition-[width] duration-200 ease-out"
                    style={{
                      width: `${Math.round(
                        (refreshProgress.current / refreshProgress.total) * 100,
                      )}%`,
                    }}
                  />
                </div>
                <div className="text-sm font-medium">
                  {refreshProgress.current}/{refreshProgress.total} 项目
                </div>
                <div
                  className="text-muted-foreground truncate text-xs"
                  title={refreshProgress.current_project}
                >
                  {formatCurrentProject(refreshProgress.current_project)}
                </div>
              </div>
            ) : (
              <div className="text-muted-foreground text-sm">正在准备扫描列表...</div>
            )}
          </div>
        )}
        {projects === null && !error && !isRefreshing && (
          <div className="text-muted-foreground px-2 py-1">加载中...</div>
        )}
        {error && (
          <div className="text-destructive px-2 py-1">加载失败：{error}</div>
        )}
        {projects?.length === 0 && (
          <div className="text-muted-foreground px-2 py-1">
            尚无 Claude 历史会话
          </div>
        )}
        {filteredProjects?.length === 0 && normalizedQuery && (
          <div className="text-muted-foreground px-2 py-1">
            没有匹配的 Claude 历史会话
          </div>
        )}
        {filteredProjects?.map((project) => (
          <ProjectNode
            key={project.name}
            project={project}
            expanded={
              normalizedQuery
                ? searchExpandedProjects.has(project.name)
                : expandedProjects.has(project.name)
            }
            showAll={showAllSessions.has(project.name)}
            searching={normalizedQuery.length > 0}
            onToggleExpand={() => toggleExpand(project.name)}
            onShowAll={() => showAll(project.name)}
            onSessionClick={(session) => onSessionClick(project, session)}
            onNewSession={() => onNewSession(project)}
          />
        ))}
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------------

interface ProjectNodeProps {
  project: ClaudeProjectSummaryDTO;
  expanded: boolean;
  showAll: boolean;
  searching: boolean;
  onToggleExpand: () => void;
  onShowAll: () => void;
  onSessionClick: (session: ClaudeSessionSummaryDTO) => void;
  onNewSession: () => void;
}

function ProjectNode({
  project,
  expanded,
  showAll,
  searching,
  onToggleExpand,
  onShowAll,
  onSessionClick,
  onNewSession,
}: ProjectNodeProps): JSX.Element {
  const visibleSessions = expanded
    ? searching
      ? project.sessions
      : showAll
      ? project.sessions
      : project.sessions.slice(0, DEFAULT_VISIBLE)
    : [];
  const hasMore = !searching && project.sessions.length > DEFAULT_VISIBLE;

  return (
    <div className="flex flex-col">
      <button
        type="button"
        onClick={onToggleExpand}
        title={project.cwd}
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
        <span className="truncate">{project.display_name}</span>
        <span className="text-muted-foreground ml-auto text-xs shrink-0">
          {project.sessions.length}
        </span>
      </button>

      {expanded && (
        <div className="ml-4 flex flex-col gap-0.5 border-l pl-2">
          <button
            type="button"
            onClick={onNewSession}
            className="hover:bg-accent flex items-center gap-1.5 rounded px-2 py-1 text-left text-xs font-medium text-primary transition-colors"
          >
            <Plus className="h-3.5 w-3.5 shrink-0" />
            新建会话
          </button>
          {visibleSessions.map((s) => (
            <SessionCard
              key={s.claude_thread_id}
              session={s}
              onClick={() => onSessionClick(s)}
            />
          ))}
          {expanded && !showAll && hasMore && (
            <button
              type="button"
              onClick={onShowAll}
              className="text-muted-foreground hover:text-foreground hover:bg-accent rounded px-2 py-1 text-left text-xs transition-colors"
            >
              ▽ 显示更多会话（共 {project.sessions.length} 条）
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ----------------------------------------------------------------------------

interface SessionCardProps {
  session: ClaudeSessionSummaryDTO;
  onClick: () => void;
}

function SessionCard({ session, onClick }: SessionCardProps): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      title={session.title}
      className="hover:bg-accent flex items-center gap-1.5 rounded px-2 py-1 text-left transition-colors"
    >
      <MessageSquare className="text-muted-foreground h-3.5 w-3.5 shrink-0" />
      <span className="flex-1 truncate">{session.title}</span>
      <span className="text-muted-foreground shrink-0 text-xs">
        {formatRelative(session.last_modified)}
      </span>
      <Badge variant="secondary" className="shrink-0 px-1.5 py-0 text-xs">
        {session.message_count}
      </Badge>
    </button>
  );
}
