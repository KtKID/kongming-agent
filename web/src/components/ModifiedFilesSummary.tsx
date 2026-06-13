/** 本轮修改文件摘要卡片——展示当前 run 内被写入的文件列表，支持点击查看 */
import { FileText } from "lucide-react";
import { useWorkspaceStore } from "@/stores/workspace";

interface ModifiedFilesSummaryProps {
  /** 去重后的绝对路径数组（由调用方计算） */
  files: string[];
  threadId: string;
}

export function ModifiedFilesSummary({
  files,
  threadId,
}: ModifiedFilesSummaryProps) {
  const openFileDrawer = useWorkspaceStore((s) => s.openFileDrawer);
  const workspaceRoot = useWorkspaceStore(
    (s) => s.contextsByThread[threadId]?.workspace_root,
  );

  if (files.length === 0) return null;

  return (
    <div className="rounded-lg border border-border bg-card p-3 text-xs">
      <div className="mb-2 flex items-center gap-2">
        <FileText className="h-3.5 w-3.5 text-muted-foreground" />
        <span className="text-sm font-medium text-foreground">
          本轮修改文件
        </span>
        <span className="ml-auto text-[11px] text-muted-foreground">
          {files.length}
        </span>
      </div>
      <div className="flex flex-col gap-1">
        {files.map((absolutePath) => {
          const basename = absolutePath.split("/").pop() ?? absolutePath;
          return (
            <div
              key={absolutePath}
              className="flex items-center gap-2 rounded px-2 py-1.5 transition-colors hover:bg-muted/50"
            >
              <span className="min-w-0 flex-1 truncate font-mono text-[12px]">
                {basename}
              </span>
              <button
                type="button"
                className="flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-primary transition-colors hover:bg-accent hover:text-accent-foreground"
                onClick={() =>
                  openFileDrawer(threadId, absolutePath, workspaceRoot ?? "")
                }
              >
                <FileText className="h-3 w-3" />
                查看文件
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
