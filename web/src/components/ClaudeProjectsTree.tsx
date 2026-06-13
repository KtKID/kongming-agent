import { useMemo, useState } from "react";
import type { JSX } from "react";
import { Archive, Pencil, Pin, PinOff, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { useParams } from "react-router-dom";

import { apiPatch, apiPost, apiRefreshClaudeProjects } from "@/lib/api";
import { useThreadsStore } from "@/stores/threads";
import { useThreadStatusStore } from "@/stores/threadStatus";
import { PhaseIndicator } from "@/components/PhaseIndicator";
import { formatRelative } from "@/lib/relative-time";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AddProjectDialog } from "@/components/AddProjectDialog";
import { ProjectSessionBrowser } from "@/components/ProjectSessionBrowser";
import { SidebarSessionRow, type HoverAction } from "@/components/SidebarSessionRow";
import { ThreadSourceIcon } from "@/components/ThreadSourceIcon";
import { useInlineEdit } from "@/hooks/useInlineEdit";
import { loadCachedPayload, saveCachedPayload } from "@/lib/sidebar-cache";
import type {
  ClaudeProjectSummaryDTO,
  ClaudeProjectsRefreshProgressDTO,
  ClaudeSessionSummaryDTO,
  ImportClaudeSessionRequest,
  ImportClaudeSessionResponse,
} from "@/protocol";

const CLAUDE_PROJECTS_CACHE_KEY = "kongming.sidebar.claude-projects";
const CLAUDE_PROJECTS_CACHE_TTL_MS = 15_000;

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface ClaudeProjectsTreeProps {
  onSessionClick: (
    project: ClaudeProjectSummaryDTO,
    session: ClaudeSessionSummaryDTO,
  ) => void;
  onNewSession: (project: ClaudeProjectSummaryDTO) => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ClaudeProjectsTree({
  onSessionClick,
  onNewSession,
}: ClaudeProjectsTreeProps): JSX.Element {
  const cached = loadCachedPayload<ClaudeProjectSummaryDTO[]>(CLAUDE_PROJECTS_CACHE_KEY);
  const [projects, setProjects] = useState<ClaudeProjectSummaryDTO[] | null>(
    cached?.value ?? null,
  );
  const [error, setError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [refreshProgress, setRefreshProgress] =
    useState<ClaudeProjectsRefreshProgressDTO | null>(null);
  const [addOpen, setAddOpen] = useState(false);

  const claudeProjectsRefreshKey = useThreadsStore(
    (s) => s.claudeProjectsRefreshKey,
  );
  const removeClaudeProject = useThreadsStore((s) => s.removeClaudeProject);

  const refreshProjects = async (): Promise<void> => {
    setIsRefreshing(true);
    setRefreshProgress(null);
    setError(null);
    try {
      const result = await apiRefreshClaudeProjects((progress) => {
        setRefreshProgress(progress);
      });
      setProjects(result.projects);
      saveCachedPayload(CLAUDE_PROJECTS_CACHE_KEY, result.projects);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsRefreshing(false);
      setRefreshProgress(null);
    }
  };

  const handleRemove = async (project: ClaudeProjectSummaryDTO): Promise<void> => {
    const confirmed = window.confirm(
      `移除项目 ${project.cwd}？\n（仅从登记列表移除，不删 ~/.claude/projects/ 下的 jsonl）`,
    );
    if (!confirmed) return;
    try {
      await removeClaudeProject(project.cwd);
      toast.success("已移除");
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`移除失败：${msg}`);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center justify-between border-b px-3 py-2">
        <span className="text-xs text-muted-foreground">Claude 项目</span>
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
        kind="claude"
      />
      <div className="min-h-0 flex-1">
        <ProjectSessionBrowser<ClaudeProjectSummaryDTO, ClaudeSessionSummaryDTO>
          projects={projects}
          projectKey={(p) => p.name}
          projectLabel={(p) => p.display_name}
          projectTitle={(p) => p.cwd}
          projectSessionCount={(p) => p.sessions.length}
          sessions={(p) => p.sessions}
          sessionKey={(s) => s.claude_thread_id}
          sessionSearchTexts={(s) => [s.title, s.claude_thread_id]}
          renderSessionRow={(project, session) => (
            <ClaudeSessionRow
              key={session.claude_thread_id}
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
          onNewSession={(p) => onNewSession(p)}
          isRefreshing={isRefreshing}
          refreshProgress={refreshProgress}
          error={error}
          searchPlaceholder="搜索项目或 session"
          searchAriaLabel="搜索 Claude 历史会话"
          emptyText="还没添加任何项目，点上方按钮添加"
          noMatchText="没有匹配的 Claude 历史会话"
          refreshLabel="重新加载"
          externalRefreshKey={
            claudeProjectsRefreshKey > 0 ||
            !cached ||
            Date.now() - cached.savedAt > CLAUDE_PROJECTS_CACHE_TTL_MS
              ? claudeProjectsRefreshKey || 1
              : 0
          }
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Session row adapter（频道业务逻辑：rename / archive / selected）
// ---------------------------------------------------------------------------

interface ClaudeSessionRowProps {
  project: ClaudeProjectSummaryDTO;
  session: ClaudeSessionSummaryDTO;
  onSessionClick: () => void;
  onRefresh: () => Promise<void>;
}

function ClaudeSessionRow({
  project,
  session,
  onSessionClick,
  onRefresh,
}: ClaudeSessionRowProps): JSX.Element {
  const params = useParams<{ thread_id?: string }>();
  const threads = useThreadsStore((s) => s.threads);
  const statuses = useThreadStatusStore((s) => s.statuses);

  // 选中态：当前路由 thread 对应的 claude_thread_id 匹配
  const selected = useMemo(() => {
    if (!params.thread_id) return false;
    const currentThread = threads.find((t) => t.id === params.thread_id);
    return (
      currentThread?.backend_kind === "claude_code" &&
      currentThread?.claude_thread_id === session.claude_thread_id
    );
  }, [params.thread_id, threads, session.claude_thread_id]);

  // phase 状态
  const phase = useMemo(() => {
    const t = threads.find((th) => th.claude_thread_id === session.claude_thread_id);
    return t ? statuses[t.id] : undefined;
  }, [threads, statuses, session.claude_thread_id]);

  // rename
  const { editing, startEdit, inputProps } = useInlineEdit({
    onConfirm: async (newTitle) => {
      try {
        // 先确保 session 已绑定 thread（import 是幂等的），再 PATCH name
        const importBody: ImportClaudeSessionRequest = {
          claude_thread_id: session.claude_thread_id,
          cwd: project.cwd,
          name: session.title,
        };
        const importResp = await apiPost<ImportClaudeSessionResponse>(
          "/api/threads/import-claude-session",
          importBody,
        );
        await apiPatch(`/api/threads/${importResp.thread.id}`, {
          name: newTitle,
        });
        session.title = newTitle;
        // 首次 rename（imported=true）会新建 thread metadata，必须 refresh 让
        // 左侧 threads 列表立即同步——避免 phase / status / pin 查询读 stale 值。
        await onRefresh();
        toast.success("已重命名");
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        toast.error(`重命名失败：${msg}`);
      }
    },
  });

  // pin / unpin
  const onTogglePin = async () => {
    try {
      // 先确保 session 已绑定 thread（import 是幂等的）
      const importBody: ImportClaudeSessionRequest = {
        claude_thread_id: session.claude_thread_id,
        cwd: project.cwd,
        name: session.title,
      };
      const importResp = await apiPost<ImportClaudeSessionResponse>(
        "/api/threads/import-claude-session",
        importBody,
      );
      // 再 toggle pin 状态
      await apiPatch(`/api/threads/${importResp.thread.id}`, {
        is_pinned: !session.is_pinned,
      });
      await onRefresh();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`置顶失败：${msg}`);
    }
  };

  // archive
  const onArchive = async () => {
    if (!window.confirm("确定归档这个会话？归档后将不再显示在列表中。")) return;
    try {
      // 先确保 session 已绑定 thread（import 是幂等的），再 PATCH is_archived=true
      const importBody: ImportClaudeSessionRequest = {
        claude_thread_id: session.claude_thread_id,
        cwd: project.cwd,
        name: session.title,
      };
      const importResp = await apiPost<ImportClaudeSessionResponse>(
        "/api/threads/import-claude-session",
        importBody,
      );
      await apiPatch(`/api/threads/${importResp.thread.id}`, {
        is_archived: true,
      });
      toast.success("已归档");
      await onRefresh();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`归档失败：${msg}`);
    }
  };

  const actions: HoverAction[] = [
    {
      icon: session.is_pinned ? PinOff : Pin,
      label: session.is_pinned ? "取消置顶" : "置顶",
      onClick: () => void onTogglePin(),
    },
    { icon: Pencil, label: "重命名", onClick: () => startEdit(session.title) },
    { icon: Archive, label: "归档", variant: "destructive", onClick: () => void onArchive() },
  ];

  return (
    <SidebarSessionRow
      selected={selected}
      leading={
        <>
          <ThreadSourceIcon backendKind="claude_code" active={selected} />
          <PhaseIndicator phase={phase?.phase} toolName={phase?.toolName} />
        </>
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
      editing={editing}
      editSlot={
        <input
          {...inputProps}
          className="h-6 flex-1 rounded border border-border bg-background px-1.5 text-sm outline-none focus:ring-1 focus:ring-ring"
        />
      }
      onOpen={onSessionClick}
    />
  );
}
