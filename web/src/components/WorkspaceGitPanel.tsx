import { useEffect, useMemo, useRef, useState } from "react";
import { Clock3, GitBranch, RefreshCw, SplitSquareVertical } from "lucide-react";
import { toast } from "sonner";
import { apiGet, apiPost } from "@/lib/api";
import type {
  WorkspaceGitActionResultDTO,
  WorkspaceContextDTO,
  WorkspaceGitBranchesDTO,
  WorkspaceGitCommitsDTO,
  WorkspaceGitFileDiffDTO,
  WorkspaceGitStatusDTO,
} from "@/protocol";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

type GitView = "changes" | "history" | "branches";

interface WorkspaceGitPanelProps {
  context?: WorkspaceContextDTO;
  loading?: boolean;
  onOpenFile?: (path: string) => void;
}

function statusLabel(code: string): string {
  if (code === "?") return "untracked";
  if (code === "M") return "modified";
  if (code === "A") return "added";
  if (code === "D") return "deleted";
  if (code === "R") return "renamed";
  if (code === "C") return "copied";
  if (code === "U") return "unmerged";
  if (code === "T") return "type";
  return "clean";
}

export function WorkspaceGitPanel({
  context,
  loading = false,
  onOpenFile,
}: WorkspaceGitPanelProps) {
  const [view, setView] = useState<GitView>("changes");
  const [status, setStatus] = useState<WorkspaceGitStatusDTO | null>(null);
  const [branches, setBranches] = useState<WorkspaceGitBranchesDTO | null>(null);
  const [commits, setCommits] = useState<WorkspaceGitCommitsDTO | null>(null);
  const [selectedPath, setSelectedPath] = useState("");
  const [selectedDiff, setSelectedDiff] = useState("");
  const [error, setError] = useState("");
  const [loadingState, setLoadingState] = useState(false);
  const [loadingDiff, setLoadingDiff] = useState(false);
  const [actionLoading, setActionLoading] = useState("");
  const [commitMessage, setCommitMessage] = useState("");
  const [newBranchName, setNewBranchName] = useState("");
  const epochRef = useRef(0);
  const diffRequestVersionRef = useRef(0);

  useEffect(() => {
    epochRef.current += 1;
    diffRequestVersionRef.current = 0;
    setStatus(null);
    setBranches(null);
    setCommits(null);
    setSelectedPath("");
    setSelectedDiff("");
    setError("");
    setActionLoading("");
    setCommitMessage("");
    setNewBranchName("");
  }, [context?.thread_id]);

  useEffect(() => {
    if (!context?.files_available) return;
    void refreshAll();
  }, [context?.thread_id, context?.files_available]);

  const changes = status?.changes ?? [];
  const stageablePaths = useMemo(
    () =>
      changes
        .filter((entry) => entry.staged_status === "?" || entry.unstaged_status !== " ")
        .map((entry) => entry.path),
    [changes],
  );
  const stagedPaths = useMemo(
    () =>
      changes
        .filter((entry) => entry.staged_status !== " " && entry.staged_status !== "?")
        .map((entry) => entry.path),
    [changes],
  );
  const canCommit = stagedPaths.length > 0 && commitMessage.trim().length > 0;
  const selectedEntry = useMemo(
    () => changes.find((entry) => entry.path === selectedPath) ?? null,
    [changes, selectedPath],
  );
  const selectedCanStage = !!selectedEntry
    && (selectedEntry.staged_status === "?" || selectedEntry.unstaged_status !== " ");
  const selectedCanUnstage = !!selectedEntry
    && selectedEntry.staged_status !== " "
    && selectedEntry.staged_status !== "?";

  async function refreshAll(): Promise<void> {
    if (!context) return;
    const epoch = epochRef.current;
    setLoadingState(true);
    try {
      const [nextStatus, nextBranches, nextCommits] = await Promise.all([
        apiGet<WorkspaceGitStatusDTO>(
          `/api/threads/${context.thread_id}/workspace-git/status`,
        ),
        apiGet<WorkspaceGitBranchesDTO>(
          `/api/threads/${context.thread_id}/workspace-git/branches`,
        ),
        apiGet<WorkspaceGitCommitsDTO>(
          `/api/threads/${context.thread_id}/workspace-git/commits`,
        ),
      ]);
      if (epochRef.current !== epoch) return;
      setStatus(nextStatus);
      setBranches(nextBranches);
      setCommits(nextCommits);
      setError("");
      const retainedPath = nextStatus.changes.some((entry) => entry.path === selectedPath)
        ? selectedPath
        : "";
      const firstPath = retainedPath || nextStatus.changes[0]?.path || "";
      if (!firstPath) {
        setSelectedPath("");
        setSelectedDiff("");
        return;
      }
      void loadDiff(firstPath, epoch);
    } catch (err) {
      if (epochRef.current !== epoch) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (epochRef.current === epoch) setLoadingState(false);
    }
  }

  async function loadDiff(path: string, epoch = epochRef.current): Promise<void> {
    if (!context) return;
    const version = diffRequestVersionRef.current + 1;
    diffRequestVersionRef.current = version;
    setSelectedPath(path);
    setLoadingDiff(true);
    try {
      const payload = await apiGet<WorkspaceGitFileDiffDTO>(
        `/api/threads/${context.thread_id}/workspace-git/file-diff?path=${encodeURIComponent(path)}`,
      );
      if (epochRef.current !== epoch || diffRequestVersionRef.current !== version) return;
      setSelectedDiff(payload.diff);
      setError("");
    } catch (err) {
      if (epochRef.current !== epoch || diffRequestVersionRef.current !== version) return;
      setSelectedDiff("");
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (epochRef.current === epoch && diffRequestVersionRef.current === version) {
        setLoadingDiff(false);
      }
    }
  }

  async function runPathAction(
    action: "stage" | "unstage",
    paths: string[],
    successMessage: string,
  ): Promise<void> {
    if (!context || paths.length === 0) return;
    setActionLoading(action);
    try {
      await apiPost<WorkspaceGitActionResultDTO>(
        `/api/threads/${context.thread_id}/workspace-git/${action}`,
        { paths },
      );
      toast.success(successMessage);
      await refreshAll();
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      setError(detail);
      toast.error(detail);
    } finally {
      setActionLoading("");
    }
  }

  async function runCommit(): Promise<void> {
    if (!context || !canCommit) return;
    setActionLoading("commit");
    try {
      const result = await apiPost<WorkspaceGitActionResultDTO>(
        `/api/threads/${context.thread_id}/workspace-git/commit`,
        { message: commitMessage.trim() },
      );
      toast.success(result.short_commit ? `已提交 ${result.short_commit}` : "提交成功");
      setCommitMessage("");
      await refreshAll();
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      setError(detail);
      toast.error(detail);
    } finally {
      setActionLoading("");
    }
  }

  async function runCheckout(branch: string): Promise<void> {
    if (!context) return;
    setActionLoading(`checkout:${branch}`);
    try {
      const result = await apiPost<WorkspaceGitActionResultDTO>(
        `/api/threads/${context.thread_id}/workspace-git/checkout`,
        { branch },
      );
      toast.success(result.current_branch ? `已切到 ${result.current_branch}` : "已切换分支");
      await refreshAll();
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      setError(detail);
      toast.error(detail);
    } finally {
      setActionLoading("");
    }
  }

  async function runCreateBranch(): Promise<void> {
    if (!context || !newBranchName.trim()) return;
    setActionLoading("create-branch");
    try {
      const result = await apiPost<WorkspaceGitActionResultDTO>(
        `/api/threads/${context.thread_id}/workspace-git/create-branch`,
        { branch: newBranchName.trim(), checkout: true },
      );
      toast.success(result.current_branch ? `已创建并切换到 ${result.current_branch}` : "已创建分支");
      setNewBranchName("");
      await refreshAll();
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      setError(detail);
      toast.error(detail);
    } finally {
      setActionLoading("");
    }
  }

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        正在加载 workspace...
      </div>
    );
  }
  if (!context) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        先选择一个 thread
      </div>
    );
  }
  if (!context.files_available) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <div className="max-w-md rounded-3xl border border-border bg-card p-6 text-center">
          <div className="text-lg font-semibold">Git 当前不可用</div>
          <p className="mt-2 text-sm text-muted-foreground">
            {context.unavailable_reason ?? "当前 thread 缺少 workspaceRoot"}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div data-testid="workspace-git-panel" className="flex h-full min-h-0 flex-col">
      <div className="border-b border-border px-4 py-4">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-sm font-medium text-foreground">
              <GitBranch className="h-4 w-4" />
              Git
            </div>
            <div className="mt-2 truncate text-xs text-muted-foreground">
              {status?.repo_root ?? context.workspace_root}
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span>branch: {status?.current_branch || branches?.current_branch || "-"}</span>
              {status?.tracking_branch ? <span>track: {status.tracking_branch}</span> : null}
              {(status?.ahead_count ?? 0) > 0 ? <span>ahead {status?.ahead_count}</span> : null}
              {(status?.behind_count ?? 0) > 0 ? <span>behind {status?.behind_count}</span> : null}
            </div>
          </div>
          <Button type="button" size="sm" variant="outline" onClick={() => void refreshAll()}>
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
            刷新
          </Button>
        </div>
        <div className="mt-4 flex gap-2">
          {(["changes", "history", "branches"] as GitView[]).map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => setView(item)}
              className={`rounded-full px-3 py-1.5 text-sm transition-colors ${
                view === item
                  ? "bg-accent text-accent-foreground"
                  : "text-muted-foreground hover:bg-muted"
              }`}
            >
              {item === "changes" ? "Changes" : item === "history" ? "History" : "Branches"}
            </button>
          ))}
        </div>
      </div>
      {error ? <div className="px-4 py-3 text-sm text-destructive">{error}</div> : null}
      {view === "changes" ? (
        <div className="flex min-h-0 flex-1 overflow-hidden">
          <aside className="flex w-[22rem] min-w-[18rem] shrink-0 flex-col border-r border-border bg-card/40">
            <div className="border-b border-border px-4 py-3 text-xs text-muted-foreground">
              <div className="flex items-center justify-between gap-3">
                <span>{loadingState ? "正在刷新改动..." : `${changes.length} 个改动文件`}</span>
                <div className="flex items-center gap-2">
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={stageablePaths.length === 0 || actionLoading !== ""}
                    onClick={() => void runPathAction("stage", stageablePaths, "已暂存全部改动")}
                  >
                    全部暂存
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={stagedPaths.length === 0 || actionLoading !== ""}
                    onClick={() => void runPathAction("unstage", stagedPaths, "已撤出全部暂存")}
                  >
                    全部撤出
                  </Button>
                </div>
              </div>
            </div>
            <div className="min-h-0 flex-1 overflow-auto px-2 py-3">
              {changes.length === 0 ? (
                <div className="px-3 py-4 text-sm text-muted-foreground">当前 workspace 很干净</div>
              ) : (
                changes.map((entry) => (
                  <button
                    key={entry.path}
                    type="button"
                    onClick={() => void loadDiff(entry.path)}
                    className={`mb-1 flex w-full flex-col rounded-2xl px-3 py-2 text-left transition-colors ${
                      selectedPath === entry.path
                        ? "bg-accent text-accent-foreground"
                        : "hover:bg-muted/80"
                    }`}
                  >
                    <span className="truncate text-sm font-medium">{entry.name}</span>
                    <span className="mt-1 text-xs text-muted-foreground">
                      {statusLabel(entry.staged_status)} / {statusLabel(entry.unstaged_status)}
                    </span>
                    <span className="mt-1 truncate text-[11px] text-muted-foreground">
                      {entry.path}
                    </span>
                  </button>
                ))
              )}
            </div>
            <div className="border-t border-border px-3 py-3">
              <div className="text-xs font-medium text-foreground">Commit staged changes</div>
              <Textarea
                value={commitMessage}
                onChange={(event) => setCommitMessage(event.target.value)}
                placeholder="输入 commit message"
                className="mt-2 min-h-[92px] resize-none"
              />
              <div className="mt-2 flex items-center justify-between gap-3">
                <span className="text-[11px] text-muted-foreground">
                  {stagedPaths.length} 个路径已暂存
                </span>
                <Button
                  type="button"
                  size="sm"
                  disabled={!canCommit || actionLoading !== ""}
                  onClick={() => void runCommit()}
                >
                  提交
                </Button>
              </div>
            </div>
          </aside>
          <section className="flex min-h-0 flex-1 flex-col">
            <div className="flex items-center justify-between border-b border-border px-4 py-3">
              <div className="min-w-0">
                <div className="truncate text-sm font-medium">
                  {selectedEntry?.path || "选择一个改动文件"}
                </div>
                <div className="mt-1 text-xs text-muted-foreground">
                  {loadingDiff ? "正在加载 diff..." : "点击左侧文件查看差异"}
                </div>
              </div>
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={!selectedEntry}
                onClick={() => selectedEntry && onOpenFile?.(selectedEntry.path)}
              >
                <SplitSquareVertical className="mr-1.5 h-3.5 w-3.5" />
                在 Files 中打开
              </Button>
            </div>
            <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3">
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={!selectedEntry || !selectedCanStage || actionLoading !== ""}
                onClick={() =>
                  selectedEntry
                    && runPathAction("stage", [selectedEntry.path], `已暂存 ${selectedEntry.name}`)
                }
              >
                暂存当前文件
              </Button>
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={!selectedEntry || !selectedCanUnstage || actionLoading !== ""}
                onClick={() =>
                  selectedEntry
                    && runPathAction("unstage", [selectedEntry.path], `已撤出 ${selectedEntry.name}`)
                }
              >
                撤出当前文件
              </Button>
            </div>
            <div className="min-h-0 flex-1 overflow-auto p-4">
              <pre className="min-h-full whitespace-pre-wrap rounded-2xl bg-card p-4 font-mono text-[12px] leading-6 text-foreground">
                {selectedDiff || "这里会显示 git diff。"}
              </pre>
            </div>
          </section>
        </div>
      ) : view === "history" ? (
        <div className="min-h-0 flex-1 overflow-auto p-4">
          <div className="mb-4 flex items-center gap-2 text-sm font-medium">
            <Clock3 className="h-4 w-4" />
            Recent commits
          </div>
          <div className="space-y-3">
            {(commits?.commits ?? []).map((commit) => (
              <div key={commit.commit} className="rounded-2xl border border-border bg-card px-4 py-3">
                <div className="flex items-center gap-2 text-sm font-medium">
                  <span>{commit.subject}</span>
                  <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">
                    {commit.short_commit}
                  </span>
                </div>
                <div className="mt-2 text-xs text-muted-foreground">
                  {commit.author} · {commit.authored_at}
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-auto p-4">
          <div className="grid gap-4 md:grid-cols-2">
            <div className="rounded-3xl border border-border bg-card p-4">
              <div className="mb-4 rounded-2xl border border-border bg-background p-3">
                <div className="text-sm font-medium">Create branch</div>
                <div className="mt-3 flex gap-2">
                  <Input
                    value={newBranchName}
                    onChange={(event) => setNewBranchName(event.target.value)}
                    placeholder="feature/..."
                  />
                  <Button
                    type="button"
                    disabled={!newBranchName.trim() || actionLoading !== ""}
                    onClick={() => void runCreateBranch()}
                  >
                    创建
                  </Button>
                </div>
              </div>
              <div className="text-sm font-medium">Local branches</div>
              <div className="mt-3 space-y-2">
                {(branches?.local_branches ?? []).map((branch) => (
                  <div key={branch} className="flex items-center justify-between rounded-xl bg-muted/60 px-3 py-2 text-sm">
                    <span>{branch}</span>
                    {branch === branches?.current_branch ? (
                      <span className="text-xs text-muted-foreground">current</span>
                    ) : (
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={actionLoading !== ""}
                        onClick={() => void runCheckout(branch)}
                      >
                        切换
                      </Button>
                    )}
                  </div>
                ))}
              </div>
            </div>
            <div className="rounded-3xl border border-border bg-card p-4">
              <div className="text-sm font-medium">Remote branches</div>
              <div className="mt-3 space-y-2">
                {(branches?.remote_branches ?? []).map((branch) => (
                  <div key={branch} className="rounded-xl bg-muted/60 px-3 py-2 text-sm">
                    {branch}
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
