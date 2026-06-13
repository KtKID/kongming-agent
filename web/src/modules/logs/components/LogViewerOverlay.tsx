/**
 * LogViewerOverlay is a page-level log viewer overlay.
 *
 * It loads log sources on open, shows source tabs on the left, renders formatted
 * log content on the right, and covers loading/error/empty/truncated states.
 */

import { useEffect, useCallback, useMemo } from "react";
import { RefreshCw, FileText, AlertCircle } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useLogViewerStore } from "../store";
import { fetchLogSources, fetchLogRead } from "../api";
import { formatLogLines } from "../formatter";
import type { LogSource } from "../types";
import { LogContentPane } from "./LogContentPane";

function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return "--";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTimestamp(ms: number | null | undefined): string {
  if (ms == null) return "--";
  try {
    const d = new Date(ms);
    if (Number.isNaN(d.getTime())) return "--";
    return d.toLocaleString();
  } catch {
    return "--";
  }
}

interface SourceTabProps {
  source: LogSource;
  selected: boolean;
  onClick: () => void;
}

function SourceTab({ source, selected, onClick }: SourceTabProps) {
  return (
    <button
      type="button"
      onClick={source.exists ? onClick : undefined}
      className={`flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-xs transition-colors ${
        selected
          ? "bg-primary/10 text-primary font-medium"
          : source.exists
            ? "text-muted-foreground hover:bg-muted/60"
            : "cursor-not-allowed text-muted-foreground/40"
      }`}
    >
      <FileText className="h-3.5 w-3.5 flex-shrink-0" />
      <span className="min-w-0 flex-1 truncate">
        {source.label}
        {!source.exists && (
          <span className="ml-1 text-muted-foreground/50">(不存在)</span>
        )}
      </span>
    </button>
  );
}

export function LogViewerOverlay() {
  const isOpen = useLogViewerStore((s) => s.isOpen);
  const close = useLogViewerStore((s) => s.close);
  const sources = useLogViewerStore((s) => s.sources);
  const selectedType = useLogViewerStore((s) => s.selectedType);
  const lines = useLogViewerStore((s) => s.lines);
  const loadingSources = useLogViewerStore((s) => s.loadingSources);
  const loadingContent = useLogViewerStore((s) => s.loadingContent);
  const error = useLogViewerStore((s) => s.error);
  const truncated = useLogViewerStore((s) => s.truncated);
  const tailLines = useLogViewerStore((s) => s.tailLines);
  const lastLoadedAt = useLogViewerStore((s) => s.lastLoadedAt);
  const readBytes = useLogViewerStore((s) => s.readBytes);
  const totalBytes = useLogViewerStore((s) => s.totalBytes);

  const setSources = useLogViewerStore((s) => s.setSources);
  const setSelectedType = useLogViewerStore((s) => s.setSelectedType);
  const setLines = useLogViewerStore((s) => s.setLines);
  const setLoadingSources = useLogViewerStore((s) => s.setLoadingSources);
  const setLoadingContent = useLogViewerStore((s) => s.setLoadingContent);
  const setError = useLogViewerStore((s) => s.setError);
  const setTruncated = useLogViewerStore((s) => s.setTruncated);
  const setReadMeta = useLogViewerStore((s) => s.setReadMeta);
  const setLastLoadedAt = useLogViewerStore((s) => s.setLastLoadedAt);

  const selectedSource = useMemo(
    () => sources.find((s) => s.type === selectedType) ?? null,
    [sources, selectedType],
  );

  const loadContent = useCallback(
    async (type: string) => {
      setLoadingContent(true);
      setError(null);
      try {
        const resp = await fetchLogRead({
          type,
          tail_lines: tailLines,
        });
        setLines(resp.lines);
        setTruncated(resp.truncated);
        setReadMeta(resp.read_bytes, resp.total_bytes ?? null);
        setLastLoadedAt(Date.now());
      } catch (err) {
        setError(String(err));
      } finally {
        setLoadingContent(false);
      }
    },
    [
      tailLines,
      setLines,
      setTruncated,
      setReadMeta,
      setLastLoadedAt,
      setError,
      setLoadingContent,
    ],
  );

  useEffect(() => {
    if (!isOpen) return;

    let cancelled = false;

    async function load() {
      setLoadingSources(true);
      setError(null);
      try {
        const srcs = await fetchLogSources();
        if (cancelled) return;
        setSources(srcs);

        const first = srcs.find((s) => s.exists) || srcs[0];
        if (first) {
          setSelectedType(first.type);
          if (first.exists) {
            await loadContent(first.type);
          }
        }
      } catch (err) {
        if (!cancelled) setError(String(err));
      } finally {
        if (!cancelled) setLoadingSources(false);
      }
    }

    load();
    return () => {
      cancelled = true;
    };
  }, [
    isOpen,
    setSources,
    setSelectedType,
    setLoadingSources,
    setError,
    loadContent,
  ]);

  const handleTabClick = useCallback(
    (type: string) => {
      if (type === selectedType) return;
      setSelectedType(type);
      void loadContent(type);
    },
    [selectedType, setSelectedType, loadContent],
  );

  const handleRefresh = useCallback(() => {
    if (selectedType) {
      void loadContent(selectedType);
    }
  }, [selectedType, loadContent]);

  const viewModels = useMemo(() => {
    if (!selectedSource) return [];
    return formatLogLines(lines, selectedSource.format);
  }, [lines, selectedSource]);

  const currentFormat = selectedSource?.format ?? "plain";

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && close()}>
      <DialogContent className="flex max-w-[95vw] max-h-[90vh] w-full h-full flex-col gap-0 p-0">
        <DialogHeader className="flex-shrink-0 border-b border-border px-6 py-3">
          <DialogTitle>日志查看</DialogTitle>
          <DialogDescription className="sr-only">
            查看服务端日志文件内容
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-1 overflow-hidden">
          <div className="flex w-[220px] flex-shrink-0 flex-col border-r border-border">
            <div className="px-3 py-2 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
              日志文件
            </div>

            {loadingSources && sources.length === 0 ? (
              <div className="flex items-center justify-center py-8 text-xs text-muted-foreground">
                <RefreshCw className="mr-1.5 h-3 w-3 animate-spin" />
                加载中...
              </div>
            ) : (
              <ScrollArea className="flex-1">
                <div className="space-y-0.5 px-2 pb-2">
                  {sources.map((src) => (
                    <SourceTab
                      key={src.type}
                      source={src}
                      selected={src.type === selectedType}
                      onClick={() => handleTabClick(src.type)}
                    />
                  ))}
                </div>
              </ScrollArea>
            )}
          </div>

          <div className="flex flex-1 flex-col overflow-hidden">
            <div className="flex flex-shrink-0 items-center gap-3 border-b border-border px-4 py-2 text-[11px] text-muted-foreground">
              {selectedSource && (
                <>
                  <span className="font-mono" title={selectedSource.path}>
                    {selectedSource.path}
                  </span>
                  <span className="text-border">|</span>
                  <span>{formatBytes(selectedSource.size_bytes)}</span>
                  <span className="text-border">|</span>
                  <span>更新 {formatTimestamp(selectedSource.updated_at_ms)}</span>
                </>
              )}

              <div className="flex-1" />

              {lastLoadedAt && (
                <span className="tabular-nums">
                  已加载 {formatTimestamp(lastLoadedAt)}
                </span>
              )}

              <Button
                variant="ghost"
                size="sm"
                className="h-6 px-2 text-[11px]"
                onClick={handleRefresh}
                disabled={loadingContent}
              >
                <RefreshCw
                  className={`mr-1 h-3 w-3 ${loadingContent ? "animate-spin" : ""}`}
                />
                刷新
              </Button>
            </div>

            {truncated && (
              <div className="flex items-center gap-2 bg-amber-500/10 px-4 py-1.5 text-xs text-amber-600 dark:text-amber-400">
                <AlertCircle className="h-3 w-3 flex-shrink-0" />
                <span>仅显示尾部 {tailLines} 行，文件已截断</span>
              </div>
            )}

            {error && (
              <div className="flex items-center gap-2 bg-destructive/10 px-4 py-2 text-xs text-destructive">
                <AlertCircle className="h-3 w-3 flex-shrink-0" />
                <span className="flex-1">{error}</span>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-[11px] text-destructive hover:bg-destructive/20"
                  onClick={handleRefresh}
                  disabled={loadingContent}
                >
                  重试
                </Button>
              </div>
            )}

            <div className="flex-1 overflow-hidden">
              {selectedSource && !selectedSource.exists ? (
                <div className="flex flex-col items-center justify-center gap-2 py-16 text-sm text-muted-foreground">
                  <FileText className="h-8 w-8 text-muted-foreground/40" />
                  <p>日志文件尚未产生</p>
                  <p className="text-xs font-mono text-muted-foreground/60">
                    {selectedSource.path}
                  </p>
                </div>
              ) : loadingContent && lines.length === 0 ? (
                <div className="flex items-center justify-center py-16 text-xs text-muted-foreground">
                  <RefreshCw className="mr-1.5 h-3 w-3 animate-spin" />
                  加载中...
                </div>
              ) : (
                <LogContentPane lines={viewModels} format={currentFormat} />
              )}
            </div>

            {selectedSource?.exists && lines.length > 0 && (
              <div className="flex flex-shrink-0 items-center gap-2 border-t border-border px-4 py-1.5 text-[10px] text-muted-foreground/60">
                <span>已读取 {formatBytes(readBytes)}</span>
                {totalBytes != null && (
                  <>
                    <span>/</span>
                    <span>共 {formatBytes(totalBytes)}</span>
                  </>
                )}
                <span className="text-border">|</span>
                <span>{lines.length} 行</span>
              </div>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
