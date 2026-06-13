/**
 * SitianReportDialog —— 司天报告居中 Modal
 *
 * 4 个区块自上而下：
 *   1. DialogHeader（title + 生成时间/模型名/报告 ID 小字 meta 条）
 *   2. summary 正文
 *   3. errors 警示条（数量 > 0 才显示）
 *   4. topAlerts 列表（用 sortedAlerts，已按 priority + severity 排序）
 *   5. projects 列表（用 dedupedProjects，已按 projectId 去重）
 *
 * 状态分支：
 *   - loading → 顶部 skeleton
 *   - noReport（404）→ 友好空态
 *   - error → 红色错误条 + 刷新按钮
 *   - 正常 → 渲染报告
 *
 * ScrollArea 包裹滚动内容；外层 DialogContent 高度限制 max-h-[85vh]。
 * 注意：内部内容不使用 truncate（避免 Radix ScrollArea 内 truncate 失效，见用户全局偏好）。
 */

import { RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { useSitian } from "../hooks/useSitian";
import { AlertCard } from "./AlertCard";
import { ProjectStatusRow } from "./ProjectStatusRow";

function formatTimestamp(iso: string): string {
  if (!iso) return "";
  try {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    return date.toLocaleString();
  } catch {
    return iso;
  }
}

function shortReportId(id: string): string {
  if (!id) return "";
  return id.length > 12 ? id.slice(0, 12) : id;
}

export function SitianReportDialog() {
  const {
    isOpen,
    close,
    refresh,
    loading,
    error,
    noReport,
    report,
    sortedAlerts,
    dedupedProjects,
  } = useSitian();

  return (
    <Dialog open={isOpen} onOpenChange={(open) => (!open ? close() : undefined)}>
      <DialogContent className="flex max-h-[85vh] w-full max-w-2xl flex-col gap-0 p-0">
        <DialogHeader className="flex-shrink-0 border-b border-border px-6 py-4">
          <DialogTitle>司天报告</DialogTitle>
          {report && (
            <DialogDescription className="mt-1 text-xs">
              生成时间 {formatTimestamp(report.generatedAt)}
              {report.modelName && (
                <>
                  <span className="mx-1.5">·</span>
                  {report.modelName}
                </>
              )}
              {report.reportId && (
                <>
                  <span className="mx-1.5">·</span>
                  <span className="font-mono">
                    {shortReportId(report.reportId)}
                  </span>
                </>
              )}
            </DialogDescription>
          )}
        </DialogHeader>

        <ScrollArea className="flex-1 overflow-hidden">
          <div className="space-y-4 px-6 py-4">
            {/* 错误条 */}
            {error && (
              <div className="flex items-center gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                <span className="flex-1">{error}</span>
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-7 px-2 text-xs text-destructive hover:bg-destructive/20"
                  onClick={() => void refresh()}
                  disabled={loading}
                >
                  重试
                </Button>
              </div>
            )}

            {/* 空态：报告不存在 */}
            {noReport && !loading && (
              <div className="rounded-md border border-dashed border-border px-4 py-8 text-center text-sm text-muted-foreground">
                <p>尚未生成司天报告。</p>
                <p className="mt-1 text-xs">
                  司天扫描 + LLM 分析完成后，报告会自动出现在这里。
                </p>
              </div>
            )}

            {/* 加载态 skeleton */}
            {loading && !report && (
              <div className="space-y-3">
                <Skeleton className="h-4 w-3/4" />
                <Skeleton className="h-4 w-2/3" />
                <Skeleton className="h-20 w-full" />
                <Skeleton className="h-20 w-full" />
              </div>
            )}

            {/* 正常内容 */}
            {report && (
              <>
                {/* summary */}
                {report.summary && (
                  <section>
                    <p className="whitespace-pre-wrap text-sm text-foreground/90">
                      {report.summary}
                    </p>
                  </section>
                )}

                {/* errors 警示条 */}
                {report.errors && report.errors.length > 0 && (
                  <section className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                    <div className="mb-1 text-xs font-medium uppercase tracking-wider">
                      报告错误 ({report.errors.length})
                    </div>
                    <ul className="list-disc space-y-0.5 pl-5">
                      {report.errors.map((err, idx) => (
                        <li key={idx} className="whitespace-pre-wrap">
                          {err}
                        </li>
                      ))}
                    </ul>
                  </section>
                )}

                {/* topAlerts */}
                <section>
                  <h3 className="mb-2 text-sm font-semibold text-foreground">
                    需关注 ({sortedAlerts.length})
                  </h3>
                  {sortedAlerts.length === 0 ? (
                    <p className="text-xs text-muted-foreground">无</p>
                  ) : (
                    <div className="space-y-2">
                      {sortedAlerts.map((alert) => (
                        <AlertCard key={alert.alertId} alert={alert} />
                      ))}
                    </div>
                  )}
                </section>

                {/* projects */}
                <section>
                  <h3 className="mb-2 text-sm font-semibold text-foreground">
                    项目状态 ({dedupedProjects.length})
                  </h3>
                  {dedupedProjects.length === 0 ? (
                    <p className="text-xs text-muted-foreground">无</p>
                  ) : (
                    <div className="space-y-2">
                      {dedupedProjects.map((project) => (
                        <ProjectStatusRow
                          key={project.projectId}
                          project={project}
                        />
                      ))}
                    </div>
                  )}
                </section>
              </>
            )}
          </div>
        </ScrollArea>

        {/* 底部刷新条 */}
        {(report || noReport) && (
          <div className="flex flex-shrink-0 items-center justify-end border-t border-border px-6 py-2.5">
            <Button
              size="sm"
              variant="ghost"
              className="h-7 text-xs"
              onClick={() => void refresh()}
              disabled={loading}
            >
              <RefreshCw
                className={`mr-1 h-3 w-3 ${loading ? "animate-spin" : ""}`}
              />
              刷新
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
