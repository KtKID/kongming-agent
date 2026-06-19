import { useEffect, useState, type ReactNode } from "react";
import { FileJson, FileText, Folder, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Markdown } from "@/lib/markdown";
import { cn } from "@/lib/utils";
import {
  fetchThreadArtifactContent,
  fetchThreadArtifacts,
} from "./api";
import { JsonBlock, StructuredRecordRenderer } from "./renderers/StructuredRecordRenderer";
import type {
  ThreadArtifactContentDTO,
  ThreadArtifactDiagnosticDTO,
  ThreadArtifactRefDTO,
} from "./types";

export function ThreadFileBrowser({
  threadId,
  selectedArtifactId,
  onSelectArtifact,
}: {
  threadId: string;
  selectedArtifactId?: string;
  onSelectArtifact: (artifactId: string) => void;
}) {
  const [files, setFiles] = useState<ThreadArtifactRefDTO[]>([]);
  const [content, setContent] = useState<ThreadArtifactContentDTO | null>(null);
  const [loadingList, setLoadingList] = useState(false);
  const [loadingContent, setLoadingContent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const activeArtifactId =
    selectedArtifactId || files.find((file) => file.available)?.artifact_id || files[0]?.artifact_id;

  const loadList = async () => {
    setLoadingList(true);
    setError(null);
    try {
      const listing = await fetchThreadArtifacts(threadId);
      setFiles(listing.files);
      const preferred =
        selectedArtifactId ||
        listing.files.find((file) => file.path === "manifest.json" && file.available)?.artifact_id ||
        listing.files.find((file) => file.available)?.artifact_id;
      if (preferred && preferred !== selectedArtifactId) onSelectArtifact(preferred);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoadingList(false);
    }
  };

  useEffect(() => {
    void loadList();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId]);

  useEffect(() => {
    if (!activeArtifactId) return;
    setLoadingContent(true);
    setError(null);
    void fetchThreadArtifactContent({ threadId, artifactId: activeArtifactId })
      .then(setContent)
      .catch((err: unknown) => setError(errorMessage(err)))
      .finally(() => setLoadingContent(false));
  }, [activeArtifactId, threadId]);

  return (
    <div className="grid min-h-0 min-w-0 flex-1 gap-3 lg:grid-cols-[18rem_minmax(0,1fr)]">
      <aside className="obsidian-panel-soft flex min-h-0 min-w-0 flex-col rounded-xl border border-border/70">
        <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border/70 px-3 py-2.5">
          <div className="text-sm font-semibold text-foreground">文件</div>
          <Button
            variant="ghost"
            size="icon"
            onClick={() => void loadList()}
            disabled={loadingList}
            aria-label="刷新文件列表"
          >
            <RefreshCw className={cn("h-4 w-4", loadingList ? "animate-spin" : "")} />
          </Button>
        </div>
        <ScrollArea className="min-h-0 flex-1">
          <div className="space-y-1 p-2">
            {files.map((file) => (
              <button
                key={file.artifact_id}
                type="button"
                onClick={() => file.available && onSelectArtifact(file.artifact_id)}
                disabled={!file.available}
                className={cn(
                  "flex w-full min-w-0 items-center gap-2 rounded-lg border px-2.5 py-2 text-left text-sm transition-colors",
                  file.artifact_id === activeArtifactId
                    ? "border-primary/35 bg-primary/10 text-foreground"
                    : "border-transparent text-muted-foreground hover:border-border/70 hover:bg-secondary/60",
                  !file.available ? "opacity-45" : "",
                )}
              >
                <ArtifactIcon kind={file.kind} />
                <span className="min-w-0 flex-1 truncate">{file.title}</span>
                {file.record_count != null ? (
                  <span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px]">
                    {file.record_count}
                  </span>
                ) : null}
              </button>
            ))}
            {!loadingList && files.length === 0 ? (
              <div className="rounded-lg border border-dashed border-border/70 px-3 py-8 text-center text-sm text-muted-foreground">
                暂无文件
              </div>
            ) : null}
          </div>
        </ScrollArea>
      </aside>

      <section className="obsidian-panel-soft flex min-h-0 min-w-0 flex-col rounded-xl border border-border/70">
        <div className="shrink-0 border-b border-border/70 px-4 py-3">
          <div className="min-w-0 truncate text-sm font-semibold text-foreground">
            {content?.title ?? "选择文件"}
          </div>
          {content?.path ? (
            <div className="mt-1 truncate font-mono text-[11px] text-muted-foreground">
              {content.path}
            </div>
          ) : null}
        </div>
        <ScrollArea className="min-h-0 flex-1">
          <div className="min-w-0 p-3">
            {error ? (
              <div className="rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
                {error}
              </div>
            ) : loadingContent ? (
              <div className="rounded-lg border border-dashed border-border/70 px-3 py-8 text-center text-sm text-muted-foreground">
                加载中
              </div>
            ) : content ? (
              <ArtifactContent content={content} />
            ) : (
              <div className="rounded-lg border border-dashed border-border/70 px-3 py-8 text-center text-sm text-muted-foreground">
                选择一个文件
              </div>
            )}
          </div>
        </ScrollArea>
      </section>
    </div>
  );
}

function ArtifactContent({ content }: { content: ThreadArtifactContentDTO }) {
  return (
    <div className="space-y-3">
      <Diagnostics diagnostics={content.diagnostics} />
      {content.truncated ? (
        <div className="rounded-lg border border-amber-400/30 bg-amber-400/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-200">
          文件已截断
        </div>
      ) : null}
      {renderContent(content)}
    </div>
  );
}

function renderContent(content: ThreadArtifactContentDTO): ReactNode {
  if (content.kind === "jsonl") {
    const records = Array.isArray(content.content) ? content.content : [];
    return (
      <div className="space-y-3" data-testid="thread-jsonl-records">
        {records.map((record, index) => (
          <StructuredRecordRenderer key={index} record={record} index={index} />
        ))}
      </div>
    );
  }
  if (content.kind === "json") return <JsonBlock value={content.content} />;
  if (content.kind === "markdown") {
    return (
      <Markdown
        text={String(content.content ?? "").replace(/\n(?!\n)/g, "  \n")}
        className="text-sm leading-relaxed"
      />
    );
  }
  if (content.kind === "directory") return <JsonBlock value={content.content} />;
  return (
    <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-muted-foreground">
      {String(content.content ?? "")}
    </pre>
  );
}

function Diagnostics({ diagnostics }: { diagnostics: ThreadArtifactDiagnosticDTO[] }) {
  if (diagnostics.length === 0) return null;
  return (
    <div className="space-y-1">
      {diagnostics.map((diagnostic, index) => (
        <div
          key={`${diagnostic.code}-${index}`}
          className="rounded-lg border border-border/70 bg-muted/40 px-3 py-2 text-xs text-muted-foreground"
        >
          <span className="font-medium text-foreground">{diagnostic.code}</span>
          {" "}
          {diagnostic.message}
        </div>
      ))}
    </div>
  );
}

function ArtifactIcon({ kind }: { kind: string }) {
  if (kind === "directory") return <Folder className="h-4 w-4 shrink-0" />;
  if (kind === "json" || kind === "jsonl") return <FileJson className="h-4 w-4 shrink-0" />;
  return <FileText className="h-4 w-4 shrink-0" />;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
