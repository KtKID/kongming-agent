import { useMemo, useState } from "react";
import type { JSX } from "react";
import { Pin, PinOff, Plus, Trash2 } from "lucide-react";
import { useParams } from "react-router-dom";

import { apiGet, apiPatch, apiPost } from "@/lib/api";
import { toast } from "sonner";
import { useThreadsStore } from "@/stores/threads";
import { formatRelative } from "@/lib/relative-time";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AddProjectDialog } from "@/components/AddProjectDialog";
import { ProjectSessionBrowser } from "@/components/ProjectSessionBrowser";
import { SidebarSessionRow, type HoverAction } from "@/components/SidebarSessionRow";
import { ThreadSourceIcon } from "@/components/ThreadSourceIcon";
import type { ImportClaudeSessionResponse } from "@/protocol";

// ---------------------------------------------------------------------------
// Types (保持原导出，LeftSidebar 依赖)
// ---------------------------------------------------------------------------

interface CodexSessionSummary {
  session_id: string;
  title: string;
  cwd: string;
  last_modified: number;
  message_count: number;
  cli_version: string;
  rollout_path: string;
  provider: string;
  is_pinned: boolean;
}

interface CodexProjectSummary {
  cwd: string;
  display_name: string;
  sessions: CodexSessionSummary[];
  last_modified: number;
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface CodexProjectsTreeProps {
  onSessionClick: (
    project: CodexProjectSummary,
    session: CodexSessionSummary,
  ) => void;
  onNewSession: (project: CodexProjectSummary) => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function CodexProjectsTree({
  onSessionClick,
  onNewSession,
}: CodexProjectsTreeProps): JSX.Element {
  const [projects, setProjects] = useState<CodexProjectSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [addOpen, setAddOpen] = useState(false);

  const codexProjectsRefreshKey = useThreadsStore(
    (s) => s.codexProjectsRefreshKey,
  );
  const removeCodexProject = useThreadsStore((s) => s.removeCodexProject);

  const refreshProjects = async (): Promise<void> => {
    setIsRefreshing(true);
    setError(null);
    try {
      const data = await apiGet<CodexProjectSummary[]>("/api/codex/projects");
      setProjects(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsRefreshing(false);
    }
  };

  const handleRemove = async (project: CodexProjectSummary): Promise<void> => {
    const confirmed = window.confirm(
      `移除项目 ${project.cwd}？\n（仅从登记列表移除，不删 ~/.codex/sessions/ 下的 jsonl）`,
    );
    if (!confirmed) return;
    try {
      await removeCodexProject(project.cwd);
      toast.success("已移除");
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`移除失败：${msg}`);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center justify-between border-b px-3 py-2">
        <span className="text-xs text-muted-foreground">Codex 项目</span>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={() => setAddOpen(true)}
          className="h-7 gap-1 px-2 text-xs"
        >
          <Plus className="h-3.5 w-3.5" />
          添加项目
        </Button>
      </div>
      <AddProjectDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        kind="codex"
      />
      <div className="min-h-0 flex-1">
        <ProjectSessionBrowser<CodexProjectSummary, CodexSessionSummary>
          projects={projects}
          projectKey={(p) => p.cwd}
          projectLabel={(p) => p.display_name}
          projectTitle={(p) => p.cwd}
          projectSessionCount={(p) => p.sessions.length}
          sessions={(p) => p.sessions}
          sessionKey={(s) => s.session_id}
          sessionSearchTexts={(s) => [s.title, s.session_id]}
          renderSessionRow={(project, session) => (
            <CodexSessionRow
              key={session.session_id}
              project={project}
              session={session}
              onSessionClick={() => onSessionClick(project, session)}
              onRefresh={refreshProjects}
            />
          )}
          renderProjectActions={(project) => (
            <Button
              type="button"
              size="icon"
              variant="ghost"
              onClick={() => void handleRemove(project)}
              title="移除项目"
              aria-label={`移除项目 ${project.display_name}`}
              className="h-6 w-6 text-muted-foreground hover:text-destructive"
            >
              <Trash2 className="h-3 w-3" />
            </Button>
          )}
          onRefresh={refreshProjects}
          onNewSession={(project) => onNewSession(project)}
          isRefreshing={isRefreshing}
          error={error}
          searchPlaceholder="搜索项目或 session"
          searchAriaLabel="搜索 Codex 历史会话"
          emptyText="还没添加任何项目，点上方按钮添加"
          noMatchText="没有匹配的 Codex 历史会话"
          refreshLabel="重新加载"
          defaultVisible={10}
          externalRefreshKey={codexProjectsRefreshKey}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Session row adapter
// ---------------------------------------------------------------------------

interface CodexSessionRowProps {
  project: CodexProjectSummary;
  session: CodexSessionSummary;
  onSessionClick: () => void;
  onRefresh: () => Promise<void>;
}

function CodexSessionRow({
  project,
  session,
  onSessionClick,
  onRefresh,
}: CodexSessionRowProps): JSX.Element {
  const params = useParams<{ thread_id?: string }>();
  const threads = useThreadsStore((s) => s.threads);

  const selected = useMemo(() => {
    if (!params.thread_id) return false;
    const currentThread = threads.find((t) => t.id === params.thread_id);
    return (
      currentThread?.backend_kind === "codex" &&
      currentThread?.codex_thread_id === session.session_id
    );
  }, [params.thread_id, threads, session.session_id]);

  // pin / unpin
  const onTogglePin = async () => {
    try {
      const importResp = await apiPost<ImportClaudeSessionResponse>(
        "/api/threads/import-codex-session",
        {
          codex_thread_id: session.session_id,
          cwd: project.cwd,
          name: session.title,
        },
      );
      await apiPatch(`/api/threads/${importResp.thread.id}`, {
        is_pinned: !session.is_pinned,
      });
      await onRefresh();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`置顶失败：${msg}`);
    }
  };

  const actions: HoverAction[] = [
    {
      icon: session.is_pinned ? PinOff : Pin,
      label: session.is_pinned ? "取消置顶" : "置顶",
      onClick: () => void onTogglePin(),
    },
  ];

  return (
    <SidebarSessionRow
      selected={selected}
      leading={
        <ThreadSourceIcon backendKind="codex" active={selected} />
      }
      title={session.title}
      meta={
        <>
          <span className="text-muted-foreground shrink-0 text-xs">
            {formatRelative(session.last_modified)}
          </span>
          <Badge variant="secondary" className="shrink-0 px-1.5 py-0 text-xs">
            {session.message_count}
          </Badge>
        </>
      }
      actions={actions}
      onOpen={onSessionClick}
    />
  );
}

export type { CodexProjectSummary, CodexSessionSummary };
