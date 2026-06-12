import { useCallback, useEffect, useRef, useState } from "react";
import { FileText, AlertTriangle, LoaderCircle } from "lucide-react";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { apiGet } from "@/lib/api";
import { Markdown } from "@/lib/markdown";
import { cn } from "@/lib/utils";
import type { WorkspaceFileDTO } from "@/protocol";
import { useWorkspaceStore } from "@/stores/workspace";

const WIDTH_STORAGE_KEY = "kongming.fileDrawer.width";
const DEFAULT_WIDTH = 640;
const MIN_WIDTH = 360;
const MOBILE_QUERY = "(max-width: 640px)";

interface FileDrawerProps {
  mobileMode?: boolean;
}

function loadStoredWidth(): number {
  try {
    const stored = localStorage.getItem(WIDTH_STORAGE_KEY);
    if (!stored) return DEFAULT_WIDTH;
    const value = Number(stored);
    return Number.isFinite(value) && value >= MIN_WIDTH ? value : DEFAULT_WIDTH;
  } catch {
    return DEFAULT_WIDTH;
  }
}

function clampWidth(px: number): number {
  const max = Math.max(MIN_WIDTH, window.innerWidth - 80);
  return Math.min(Math.max(px, MIN_WIDTH), max);
}

export function FileDrawer({ mobileMode }: FileDrawerProps = {}) {
  const drawerFile = useWorkspaceStore((s) => s.drawerFile);
  const closeFileDrawer = useWorkspaceStore((s) => s.closeFileDrawer);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fileData, setFileData] = useState<WorkspaceFileDTO | null>(null);
  const [width, setWidth] = useState<number>(() => loadStoredWidth());
  const [isMobile, setIsMobile] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.matchMedia(MOBILE_QUERY).matches;
  });

  const draggingRef = useRef(false);
  const widthRef = useRef(width);
  widthRef.current = width;

  const open = drawerFile !== null;
  const effectiveIsMobile = mobileMode ?? isMobile;

  function persistWidth(px: number) {
    try {
      localStorage.setItem(WIDTH_STORAGE_KEY, String(Math.round(px)));
    } catch {
      // Ignore localStorage failures in privacy mode or quota limits.
    }
  }

  useEffect(() => {
    const mediaQuery = window.matchMedia(MOBILE_QUERY);
    const handleChange = (event: MediaQueryListEvent) => {
      setIsMobile(event.matches);
    };
    setIsMobile(mediaQuery.matches);
    mediaQuery.addEventListener("change", handleChange);
    return () => mediaQuery.removeEventListener("change", handleChange);
  }, []);

  useEffect(() => {
    if (!drawerFile) return;

    let cancelled = false;
    setLoading(true);
    setError(null);
    setFileData(null);

    apiGet<WorkspaceFileDTO>(
      `/api/threads/${drawerFile.threadId}/workspace-file?path=${encodeURIComponent(drawerFile.relativePath)}`,
    )
      .then((data) => {
        if (!cancelled) setFileData(data);
      })
      .catch(() => {
        if (!cancelled) setError("文件不存在或无法读取");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [drawerFile]);

  const handleMouseDown = useCallback((event: React.MouseEvent) => {
    event.preventDefault();
    draggingRef.current = true;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    const onMove = (moveEvent: MouseEvent) => {
      if (!draggingRef.current) return;
      const next = clampWidth(window.innerWidth - moveEvent.clientX);
      widthRef.current = next;
      setWidth(next);
    };

    const onUp = () => {
      if (!draggingRef.current) return;
      draggingRef.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      persistWidth(widthRef.current);
    };

    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }, []);

  const handleDoubleClick = useCallback(() => {
    setWidth(DEFAULT_WIDTH);
    try {
      localStorage.removeItem(WIDTH_STORAGE_KEY);
    } catch {
      // Ignore reset failures for the same reason as persist.
    }
  }, []);

  const displayName =
    fileData?.name ??
    (drawerFile?.absolutePath.split("/").filter(Boolean).pop() || "文件预览");
  const isMarkdown = /\.(md|markdown)$/i.test(displayName);
  const isHtml = /\.html?$/i.test(displayName);
  const desktopStyle = effectiveIsMobile
    ? undefined
    : { width: `${width}px`, maxWidth: "unset" as const };

  return (
    <Sheet open={open} onOpenChange={(nextOpen) => !nextOpen && closeFileDrawer()}>
      <SheetContent
        side="right"
        className={cn(
          "flex flex-col sm:max-w-none",
          effectiveIsMobile ? "w-[90vw] max-w-[90vw] p-0" : "",
        )}
        style={desktopStyle}
      >
        {!effectiveIsMobile && (
          <div
            data-testid="file-drawer-resize-handle"
            role="separator"
            aria-orientation="vertical"
            aria-label="拖拽改变抽屉宽度"
            onMouseDown={handleMouseDown}
            onDoubleClick={handleDoubleClick}
            className="absolute left-0 top-0 z-10 h-full w-1 cursor-col-resize bg-transparent transition-colors hover:bg-primary/40"
          />
        )}

        <SheetHeader className={cn(effectiveIsMobile && "px-3 pt-3")}>
          <SheetTitle className="flex items-center gap-2 truncate">
            <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
            <span className="truncate">{displayName}</span>
          </SheetTitle>
          <SheetDescription className="truncate">
            {drawerFile?.relativePath ?? ""}
          </SheetDescription>
        </SheetHeader>

        <div
          className={cn(
            "flex-1 min-h-0 overflow-y-auto",
            effectiveIsMobile ? "p-3" : "p-4",
          )}
        >
          {loading && (
            <div className="flex flex-col items-center justify-center gap-2 py-12 text-muted-foreground">
              <LoaderCircle className="h-6 w-6 animate-spin" />
              <span className="text-sm">加载中...</span>
            </div>
          )}

          {error && (
            <div className="flex flex-col items-center justify-center gap-2 py-12 text-destructive">
              <AlertTriangle className="h-6 w-6" />
              <span className="text-sm">{error}</span>
            </div>
          )}

          {fileData && !loading && !error && (
            <>
              {fileData.too_large && (
                <div className="flex flex-col items-center justify-center gap-2 py-12 text-muted-foreground">
                  <AlertTriangle className="h-6 w-6" />
                  <span className="text-sm">文件过大（&gt;256KiB），无法预览</span>
                </div>
              )}

              {!fileData.too_large && !fileData.is_text && (
                <div className="flex flex-col items-center justify-center gap-2 py-12 text-muted-foreground">
                  <FileText className="h-6 w-6" />
                  <span className="text-sm">二进制文件，无法预览</span>
                </div>
              )}

              {!fileData.too_large && fileData.is_text && isMarkdown && (
                <Markdown
                  text={fileData.content}
                  className="prose-sm max-w-none"
                />
              )}

              {!fileData.too_large && fileData.is_text && isHtml && (
                <iframe
                  srcDoc={fileData.content}
                  className="h-full min-h-[60vh] w-full rounded-lg border-0"
                  sandbox="allow-scripts"
                  title={displayName}
                />
              )}

              {!fileData.too_large && fileData.is_text && !isMarkdown && !isHtml && (
                <pre
                  className={cn(
                    "font-mono text-sm leading-relaxed",
                    "whitespace-pre-wrap break-words",
                  )}
                >
                  {fileData.content}
                </pre>
              )}
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
