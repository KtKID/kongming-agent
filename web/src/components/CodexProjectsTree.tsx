import { useEffect, useMemo, useState } from "react";
import type { JSX } from "react";
import {
  ChevronDown,
  ChevronRight,
  Folder,
  FolderOpen,
  MessageSquare,
  RefreshCw,
  Search,
  X,
} from "lucide-react";

import { apiGet } from "@/lib/api";
import { formatRelative } from "@/lib/relative-time";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

interface CodexSessionSummary {
  session_id: string;
  title: string;
  cwd: string;
  last_modified: number;
  message_count: number;
  cli_version: string;
  rollout_path: string;
  provider: string;
}

interface CodexProjectSummary {
  cwd: string;
  display_name: string;
  sessions: CodexSessionSummary[];
  last_modified: number;
}

interface CodexProjectsTreeProps {
  onSessionClick: (
    project: CodexProjectSummary,
    session: CodexSessionSummary,
  ) => void;
}

const DEFAULT_VISIBLE = 10;
const ONE_WEEK_SEC = 7 * 86400;

function buildRecentProjectsSet(projects: CodexProjectSummary[]): Set<string> {
  const recent = new Set<string>();
  const oneWeekAgo = Date.now() / 1000 - ONE_WEEK_SEC;
  for (const project of projects) {
    const latest = project.sessions[0]?.last_modified;
    if (latest !== undefined && latest > oneWeekAgo) {
      recent.add(project.cwd);
    }
  }
  return recent;
}

export function CodexProjectsTree({
  onSessionClick,
}: CodexProjectsTreeProps): JSX.Element {
  const [projects, setProjects] = useState<CodexProjectSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expandedProjects, setExpandedProjects] = useState<Set<string>>(
    new Set(),
  );
  const [showAllSessions, setShowAllSessions] = useState<Set<string>>(
    new Set(),
  );
  const [searchQuery, setSearchQuery] = useState("");
  const [isRefreshing, setIsRefreshing] = useState(false);

  const applyProjects = (nextProjects: CodexProjectSummary[]): void => {
    setProjects(nextProjects);
    const recent = buildRecentProjectsSet(nextProjects);
    setExpandedProjects((current) => {
      const next = new Set<string>();
      for (const project of nextProjects) {
        if (current.has(project.cwd) || recent.has(project.cwd)) {
          next.add(project.cwd);
        }
      }
      return next;
    });
  };

  const toggleExpand = (cwd: string): void => {
    setExpandedProjects((s) => {
      const next = new Set(s);
      if (next.has(cwd)) next.delete(cwd);
      else next.add(cwd);
      return next;
    });
  };

  const showAll = (cwd: string): void => {
    setShowAllSessions((s) => new Set(s).add(cwd));
  };

  const expandAll = (): void => {
    if (!projects) return;
    const all = new Set(projects.map((p) => p.cwd));
    setExpandedProjects(all);
    setShowAllSessions(all);
  };

  const collapseAll = (): void => {
    setExpandedProjects(new Set());
    setShowAllSessions(new Set());
  };

  const refreshProjects = async (): Promise<void> => {
    setIsRefreshing(true);
    setError(null);
    try {
      const data = await apiGet<CodexProjectSummary[]>("/api/codex/projects");
      applyProjects(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsRefreshing(false);
    }
  };

  useEffect(() => {
    void refreshProjects();
  }, []);

  const normalizedQuery = searchQuery.trim().toLowerCase();
  const filteredProjects = useMemo(() => {
    if (!projects) return null;
    if (!normalizedQuery) return projects;

    return projects.flatMap((project) => {
      const projectMatched = [project.display_name, project.cwd].some((v) =>
        v.toLowerCase().includes(normalizedQuery),
      );
      const matchedSessions = project.sessions.filter((s) =>
        [s.title, s.session_id].some((v) =>
          v.toLowerCase().includes(normalizedQuery),
        ),
      );
      if (!projectMatched && matchedSessions.length === 0) return [];
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
    () => new Set(filteredProjects?.map((p) => p.cwd) ?? []),
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
            onClick={() => void refreshProjects()}
            disabled={isRefreshing}
            className="h-8 rounded-lg px-3"
          >
            <RefreshCw
              className={isRefreshing ? "h-3.5 w-3.5 animate-spin" : "h-3.5 w-3.5"}
            />
            刷新
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
            placeholder="搜索项目或 session"
            aria-label="搜索 Codex 历史会话"
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
          <div className="text-muted-foreground px-2 py-1">
            <RefreshCw className="mr-1.5 inline h-3.5 w-3.5 animate-spin" />
            正在扫描 Codex 历史会话...
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
            尚无 Codex 历史会话
          </div>
        )}
        {filteredProjects?.length === 0 && normalizedQuery && (
          <div className="text-muted-foreground px-2 py-1">
            没有匹配的 Codex 历史会话
          </div>
        )}
        {filteredProjects?.map((project) => (
          <ProjectNode
            key={project.cwd}
            project={project}
            expanded={
              normalizedQuery
                ? searchExpandedProjects.has(project.cwd)
                : expandedProjects.has(project.cwd)
            }
            showAll={showAllSessions.has(project.cwd)}
            searching={normalizedQuery.length > 0}
            onToggleExpand={() => toggleExpand(project.cwd)}
            onShowAll={() => showAll(project.cwd)}
            onSessionClick={(session) => onSessionClick(project, session)}
          />
        ))}
      </div>
    </div>
  );
}

interface ProjectNodeProps {
  project: CodexProjectSummary;
  expanded: boolean;
  showAll: boolean;
  searching: boolean;
  onToggleExpand: () => void;
  onShowAll: () => void;
  onSessionClick: (session: CodexSessionSummary) => void;
}

function ProjectNode({
  project,
  expanded,
  showAll,
  searching,
  onToggleExpand,
  onShowAll,
  onSessionClick,
}: ProjectNodeProps): JSX.Element {
  const visibleSessions = expanded
    ? searching || showAll
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
        <span className="text-muted-foreground ml-auto shrink-0 text-xs">
          {project.sessions.length}
        </span>
      </button>

      {expanded && (
        <div className="ml-4 flex flex-col gap-0.5 border-l pl-2">
          {visibleSessions.map((s) => (
            <button
              key={s.session_id}
              type="button"
              onClick={() => onSessionClick(s)}
              title={s.title}
              className="hover:bg-accent flex items-center gap-1.5 rounded px-2 py-1 text-left transition-colors"
            >
              <MessageSquare className="text-muted-foreground h-3.5 w-3.5 shrink-0" />
              <span className="flex-1 truncate">{s.title}</span>
              <span className="text-muted-foreground shrink-0 text-xs">
                {formatRelative(s.last_modified)}
              </span>
              <Badge variant="secondary" className="shrink-0 px-1.5 py-0 text-xs">
                {s.message_count}
              </Badge>
            </button>
          ))}
          {expanded && !showAll && hasMore && (
            <button
              type="button"
              onClick={onShowAll}
              className="text-muted-foreground hover:text-foreground hover:bg-accent rounded px-2 py-1 text-left text-xs transition-colors"
            >
              ▽ 显示更多（共 {project.sessions.length} 条）
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export type { CodexProjectSummary, CodexSessionSummary };
