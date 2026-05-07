import { useCallback, useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { apiGet } from "@/lib/api";

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

interface DiagramEntry {
  id: string;
  source: "docs" | "tasks";
  title: string;
  path: string;
}

interface DiagramListResponse {
  diagrams: DiagramEntry[];
}

interface ArchitectureDiagramDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

const SOURCE_LABELS: Record<DiagramEntry["source"], string> = {
  docs: "设计文档",
  tasks: "任务图",
};

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

async function fetchDiagramContent(path: string): Promise<string> {
  const resp = await fetch(
    `${BASE}/api/diagrams/content?path=${encodeURIComponent(path)}`,
    {
      credentials: "include",
      headers: { "X-Requested-With": "XMLHttpRequest" },
    },
  );
  if (!resp.ok) {
    throw new Error(`加载架构图失败：${resp.status} ${resp.statusText}`);
  }
  return resp.text();
}

/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

export function ArchitectureDiagramDialog({
  open,
  onOpenChange,
}: ArchitectureDiagramDialogProps) {
  const [diagrams, setDiagrams] = useState<DiagramEntry[]>([]);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [htmlContent, setHtmlContent] = useState<string | null>(null);
  const [contentLoading, setContentLoading] = useState(false);
  const [contentError, setContentError] = useState<string | null>(null);

  /* ---- fetch list on open ---- */
  useEffect(() => {
    if (!open) return;
    setListLoading(true);
    setListError(null);
    setSelectedId(null);
    setHtmlContent(null);
    setContentError(null);

    let cancelled = false;
    apiGet<DiagramListResponse>("/api/diagrams")
      .then((data) => {
        if (cancelled) return;
        setDiagrams(data.diagrams);
      })
      .catch((err) => {
        if (cancelled) return;
        setListError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setListLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  /* ---- fetch content on select ---- */
  const selectDiagram = useCallback(
    (entry: DiagramEntry) => {
      if (entry.id === selectedId) return;
      setSelectedId(entry.id);
      setHtmlContent(null);
      setContentError(null);
      setContentLoading(true);

      let cancelled = false;
      fetchDiagramContent(entry.path)
        .then((html) => {
          if (!cancelled) setHtmlContent(html);
        })
        .catch((err) => {
          if (!cancelled)
            setContentError(err instanceof Error ? err.message : String(err));
        })
        .finally(() => {
          if (!cancelled) setContentLoading(false);
        });

      // cleanup: cannot truly cancel fetch, but we ignore stale results
      return () => {
        cancelled = true;
      };
    },
    [selectedId],
  );

  /* ---- group by source ---- */
  const grouped = diagrams.reduce<Record<string, DiagramEntry[]>>(
    (acc, d) => {
      (acc[d.source] ??= []).push(d);
      return acc;
    },
    {},
  );

  /* ---- render ---- */
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[90vw] w-[90vw] h-[80vh] flex flex-col gap-0 p-0">
        <DialogHeader className="px-6 pt-6 pb-4 border-b border-border/80">
          <DialogTitle>架构图</DialogTitle>
        </DialogHeader>

        <div className="flex flex-1 min-h-0">
          {/* ---------- left: list ---------- */}
          <div className="w-[240px] shrink-0 border-r border-border/80 overflow-y-auto p-3">
            {listLoading && (
              <p className="text-sm text-muted-foreground px-2 py-4">
                加载中...
              </p>
            )}
            {listError && (
              <p className="text-sm text-destructive px-2 py-4">{listError}</p>
            )}
            {!listLoading && !listError && diagrams.length === 0 && (
              <p className="text-sm text-muted-foreground px-2 py-4">
                暂无架构图
              </p>
            )}
            {!listLoading &&
              !listError &&
              (["docs", "tasks"] as const).map((source) => {
                const items = grouped[source];
                if (!items || items.length === 0) return null;
                return (
                  <div key={source} className="mb-4">
                    <h3 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide px-2 mb-1.5">
                      {SOURCE_LABELS[source]}
                    </h3>
                    <ul className="space-y-0.5">
                      {items.map((entry) => (
                        <li key={entry.id}>
                          <button
                            type="button"
                            className={`w-full text-left text-sm rounded-md px-2 py-1.5 transition-colors truncate ${
                              selectedId === entry.id
                                ? "bg-accent text-accent-foreground"
                                : "text-foreground hover:bg-accent/50"
                            }`}
                            onClick={() => selectDiagram(entry)}
                          >
                            {entry.title}
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                );
              })}
          </div>

          {/* ---------- right: content ---------- */}
          <div className="flex-1 min-w-0 p-4 flex items-center justify-center bg-card">
            {!selectedId && (
              <p className="text-muted-foreground text-sm">
                选择左侧架构图查看
              </p>
            )}
            {selectedId && contentLoading && (
              <p className="text-muted-foreground text-sm">加载中...</p>
            )}
            {selectedId && contentError && (
              <p className="text-destructive text-sm">{contentError}</p>
            )}
            {selectedId && !contentLoading && !contentError && htmlContent && (
              <iframe
                srcDoc={htmlContent}
                className="w-full h-full border-0 rounded-lg"
                sandbox="allow-scripts"
                title="架构图预览"
              />
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
