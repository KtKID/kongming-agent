import {
  startTransition,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { FileText, Folder, FolderOpen, FolderTree, Save } from "lucide-react";
import { apiGet, apiPut } from "@/lib/api";
import type {
  UpdateWorkspaceFileRequest,
  WorkspaceContextDTO,
  WorkspaceFileDTO,
  WorkspaceTreeDTO,
  WorkspaceTreeNodeDTO,
} from "@/protocol";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

interface WorkspaceFilesPanelProps {
  context?: WorkspaceContextDTO;
  loading?: boolean;
  openRequest?: { path: string; nonce: number };
}

function sortEntries(entries: WorkspaceTreeNodeDTO[]): WorkspaceTreeNodeDTO[] {
  return [...entries].sort((a, b) => {
    if (a.kind !== b.kind) return a.kind === "dir" ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
}

export function WorkspaceFilesPanel({
  context,
  loading = false,
  openRequest,
}: WorkspaceFilesPanelProps) {
  const [treeByPath, setTreeByPath] = useState<Record<string, WorkspaceTreeNodeDTO[]>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [selectedPath, setSelectedPath] = useState("");
  const [currentFile, setCurrentFile] = useState<WorkspaceFileDTO | null>(null);
  const [draft, setDraft] = useState("");
  const [search, setSearch] = useState("");
  const [treeError, setTreeError] = useState("");
  const [fileError, setFileError] = useState("");
  const [saving, setSaving] = useState(false);
  const [loadingFile, setLoadingFile] = useState(false);
  const deferredSearch = useDeferredValue(search.trim().toLowerCase());
  const threadEpochRef = useRef(0);
  const treeRequestVersionRef = useRef<Record<string, number>>({});
  const fileRequestVersionRef = useRef(0);
  const saveRequestVersionRef = useRef(0);

  useEffect(() => {
    threadEpochRef.current += 1;
    treeRequestVersionRef.current = {};
    fileRequestVersionRef.current = 0;
    saveRequestVersionRef.current = 0;
    setTreeByPath({});
    setExpanded({});
    setSelectedPath("");
    setCurrentFile(null);
    setDraft("");
    setSearch("");
    setTreeError("");
    setFileError("");
  }, [context?.thread_id]);

  useEffect(() => {
    if (!context?.files_available) return;
    void loadDir("");
  }, [context?.files_available, context?.thread_id]);

  useEffect(() => {
    if (!context?.files_available || !openRequest?.path) return;
    void openFileWithAncestors(openRequest.path);
  }, [context?.files_available, context?.thread_id, openRequest?.nonce]);

  const dirty = Boolean(currentFile && draft !== currentFile.content);

  async function loadDir(path: string): Promise<void> {
    if (!context) return;
    const threadEpoch = threadEpochRef.current;
    const requestKey = path;
    const nextVersion = (treeRequestVersionRef.current[requestKey] ?? 0) + 1;
    treeRequestVersionRef.current[requestKey] = nextVersion;
    try {
      const data = await apiGet<WorkspaceTreeDTO>(
        `/api/threads/${context.thread_id}/workspace-tree?path=${encodeURIComponent(path)}`,
      );
      if (
        threadEpochRef.current !== threadEpoch ||
        treeRequestVersionRef.current[requestKey] !== nextVersion
      ) {
        return;
      }
      setTreeError("");
      setTreeByPath((prev) => ({ ...prev, [path]: sortEntries(data.entries) }));
    } catch (err) {
      if (
        threadEpochRef.current !== threadEpoch ||
        treeRequestVersionRef.current[requestKey] !== nextVersion
      ) {
        return;
      }
      setTreeError(err instanceof Error ? err.message : String(err));
    }
  }

  async function openFile(path: string): Promise<void> {
    if (!context) return;
    const threadEpoch = threadEpochRef.current;
    const requestVersion = fileRequestVersionRef.current + 1;
    fileRequestVersionRef.current = requestVersion;
    setSelectedPath(path);
    setLoadingFile(true);
    try {
      const data = await apiGet<WorkspaceFileDTO>(
        `/api/threads/${context.thread_id}/workspace-file?path=${encodeURIComponent(path)}`,
      );
      if (
        threadEpochRef.current !== threadEpoch ||
        fileRequestVersionRef.current !== requestVersion
      ) {
        return;
      }
      setCurrentFile(data);
      setDraft(data.content);
      setFileError("");
    } catch (err) {
      if (
        threadEpochRef.current !== threadEpoch ||
        fileRequestVersionRef.current !== requestVersion
      ) {
        return;
      }
      setCurrentFile(null);
      setDraft("");
      setFileError(err instanceof Error ? err.message : String(err));
    } finally {
      if (
        threadEpochRef.current === threadEpoch &&
        fileRequestVersionRef.current === requestVersion
      ) {
        setLoadingFile(false);
      }
    }
  }

  async function saveFile(): Promise<void> {
    if (!context || !currentFile) return;
    const threadEpoch = threadEpochRef.current;
    const requestVersion = saveRequestVersionRef.current + 1;
    const savedPath = currentFile.path;
    const savedDraft = draft;
    saveRequestVersionRef.current = requestVersion;
    setSaving(true);
    try {
      const data = await apiPut<WorkspaceFileDTO>(
        `/api/threads/${context.thread_id}/workspace-file`,
        {
          path: currentFile.path,
          content: draft,
        } satisfies UpdateWorkspaceFileRequest,
      );
      if (
        threadEpochRef.current !== threadEpoch ||
        saveRequestVersionRef.current !== requestVersion
      ) {
        return;
      }
      setCurrentFile(data);
      setDraft((prev) => (prev === savedDraft ? data.content : prev));
      setFileError("");
    } catch (err) {
      if (
        threadEpochRef.current !== threadEpoch ||
        saveRequestVersionRef.current !== requestVersion ||
        currentFile.path !== savedPath
      ) {
        return;
      }
      setFileError(err instanceof Error ? err.message : String(err));
    } finally {
      if (
        threadEpochRef.current === threadEpoch &&
        saveRequestVersionRef.current === requestVersion
      ) {
        setSaving(false);
      }
    }
  }

  async function openFileWithAncestors(path: string): Promise<void> {
    const normalized = path
      .trim()
      .replaceAll("\\", "/")
      .replace(/^\/+/, "")
      .replace(/\/+/g, "/");
    if (!normalized) return;
    const segments = normalized.split("/").filter(Boolean);
    let current = "";
    for (const segment of segments.slice(0, -1)) {
      const next = current ? `${current}/${segment}` : segment;
      setExpanded((prev) => ({ ...prev, [next]: true }));
      if (!treeByPath[next]) {
        await loadDir(next);
      }
      current = next;
    }
    await openFile(normalized);
  }

  function toggleDir(node: WorkspaceTreeNodeDTO): void {
    const nextOpen = !expanded[node.path];
    setExpanded((prev) => ({ ...prev, [node.path]: nextOpen }));
    if (nextOpen && !treeByPath[node.path]) {
      void loadDir(node.path);
    }
  }

  const visibleTree = useMemo(() => {
    if (!deferredSearch) {
      return {
        rootNodes: treeByPath[""] ?? [],
        childNodesByPath: treeByPath,
        autoExpandedPaths: new Set<string>(),
      };
    }

    const childNodesByPath: Record<string, WorkspaceTreeNodeDTO[]> = {};
    const autoExpandedPaths = new Set<string>();

    const filterNodes = (path: string): WorkspaceTreeNodeDTO[] => {
      const nodes = treeByPath[path] ?? [];
      const filteredNodes = nodes
        .map((node) => {
          const matchesSelf = node.name.toLowerCase().includes(deferredSearch);
          if (node.kind === "dir") {
            const children = filterNodes(node.path);
            childNodesByPath[node.path] = children;
            if (children.length > 0) autoExpandedPaths.add(node.path);
            if (matchesSelf || children.length > 0) return node;
            return null;
          }
          return matchesSelf ? node : null;
        })
        .filter((node): node is WorkspaceTreeNodeDTO => node !== null);
      childNodesByPath[path] = filteredNodes;
      return filteredNodes;
    };

    return {
      rootNodes: filterNodes(""),
      childNodesByPath,
      autoExpandedPaths,
    };
  }, [deferredSearch, treeByPath]);

  function renderNodes(nodes: WorkspaceTreeNodeDTO[], depth = 0): ReactNode[] {
    return nodes.flatMap((node) => {
      const isExpanded =
        Boolean(expanded[node.path]) || visibleTree.autoExpandedPaths.has(node.path);
      const children = deferredSearch
        ? visibleTree.childNodesByPath[node.path] ?? []
        : treeByPath[node.path] ?? [];
      const row = (
        <button
          key={node.path}
          type="button"
          onClick={() =>
            node.kind === "dir" ? toggleDir(node) : void openFile(node.path)
          }
          className={`flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left text-sm transition-colors ${
            selectedPath === node.path
              ? "bg-accent text-accent-foreground"
              : "hover:bg-muted/80"
          }`}
          style={{ paddingLeft: `${12 + depth * 16}px` }}
        >
          {node.kind === "dir" ? (
            isExpanded ? (
              <FolderOpen className="h-4 w-4 shrink-0" />
            ) : (
              <Folder className="h-4 w-4 shrink-0" />
            )
          ) : (
            <FileText className="h-4 w-4 shrink-0" />
          )}
          <span className="truncate">{node.name}</span>
        </button>
      );
      if (node.kind !== "dir" || !isExpanded) return [row];
      return [row, ...renderNodes(children, depth + 1)];
    });
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
          <div className="text-lg font-semibold">Files 当前不可用</div>
          <p className="mt-2 text-sm text-muted-foreground">
            {context.unavailable_reason ?? "当前 thread 缺少 workspaceRoot"}
          </p>
        </div>
      </div>
    );
  }
  return (
    <div
      data-testid="workspace-files-panel"
      className="flex h-full min-h-0 overflow-hidden"
    >
      <aside className="flex w-[20rem] min-w-[16rem] shrink-0 flex-col border-r border-border bg-card/40">
        <div className="border-b border-border px-4 py-4">
          <div className="flex items-center gap-2 text-sm font-medium text-foreground">
            <FolderTree className="h-4 w-4" />
            Files
          </div>
          <div className="mt-2 truncate text-xs text-muted-foreground">
            {context.workspace_root}
          </div>
          <Input
            value={search}
            onChange={(event) => startTransition(() => setSearch(event.target.value))}
            placeholder="搜索文件"
            className="mt-3 h-9"
          />
        </div>
        {treeError ? (
          <div className="px-4 py-3 text-xs text-destructive">{treeError}</div>
        ) : null}
        <div className="min-h-0 flex-1 overflow-auto px-2 py-3">
          {renderNodes(visibleTree.rootNodes)}
        </div>
      </aside>
      <section className="flex min-h-0 flex-1 flex-col">
        <div className="flex items-center justify-between border-b border-border px-4 py-4">
          <div className="min-w-0">
            <div className="truncate text-sm font-medium text-foreground">
              {currentFile?.path || "选择文件开始查看"}
            </div>
            <div className="mt-1 text-xs text-muted-foreground">
              {currentFile ? `${currentFile.size_bytes} bytes` : "文本文件支持编辑和保存"}
            </div>
          </div>
          <Button
            type="button"
            size="sm"
            onClick={() => void saveFile()}
            disabled={!currentFile || !dirty || saving || !currentFile.is_text}
            className="gap-1.5"
          >
            <Save className="h-3.5 w-3.5" />
            {saving ? "保存中" : "保存"}
          </Button>
        </div>
        {fileError ? (
          <div className="px-4 py-3 text-sm text-destructive">{fileError}</div>
        ) : null}
        {!currentFile ? (
          <div className="flex min-h-0 flex-1 items-center justify-center p-6 text-sm text-muted-foreground">
            从左侧选择一个文件
          </div>
        ) : !currentFile.is_text ? (
          <div className="flex min-h-0 flex-1 items-center justify-center p-6">
            <div className="max-w-md rounded-3xl border border-border bg-card p-6 text-center">
              <div className="text-lg font-semibold">当前文件按只读提示处理</div>
              <p className="mt-2 text-sm text-muted-foreground">
                {currentFile.too_large
                  ? "文件超过 256 KiB 限制。"
                  : "文件内容无法按 UTF-8 文本读取。"}
              </p>
            </div>
          </div>
        ) : (
          <div className="min-h-0 flex-1 p-4">
            <Textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              disabled={loadingFile}
              className="h-full min-h-0 resize-none rounded-2xl font-mono text-[13px] leading-6"
            />
          </div>
        )}
      </section>
    </div>
  );
}
