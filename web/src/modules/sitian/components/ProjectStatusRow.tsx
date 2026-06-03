/**
 * ProjectStatusRow —— 项目状态行（纯渲染）
 *
 * 视觉规约：
 *   - projectName 粗体 + statusByRule badge（status 字符串自由文本，统一用 secondary 灰）
 *   - sessionStats：等宽字体小字 total/1h/24h/3d
 *   - narrative 正文
 */

import { Badge } from "@/components/ui/badge";
import type { ProjectStatus } from "../types";

interface ProjectStatusRowProps {
  project: ProjectStatus;
}

function statusVariant(
  statusByRule: string,
): "default" | "secondary" | "destructive" | "warning" | "success" | "outline" {
  const lc = statusByRule.toLowerCase();
  if (lc.includes("active") || lc.includes("活跃")) return "success";
  if (lc.includes("idle") || lc.includes("沉默") || lc.includes("inactive")) {
    return "secondary";
  }
  if (lc.includes("stuck") || lc.includes("阻塞") || lc.includes("blocked")) {
    return "destructive";
  }
  return "secondary";
}

export function ProjectStatusRow({ project }: ProjectStatusRowProps) {
  const { sessionStats } = project;

  return (
    <div className="rounded-md border border-border bg-card p-3 text-sm">
      {/* 行 1：名字 + status */}
      <div className="mb-1.5 flex flex-wrap items-center gap-2">
        <span className="font-semibold text-foreground">
          {project.projectName}
        </span>
        <Badge
          variant={statusVariant(project.statusByRule)}
          className="px-1.5 py-0 text-[10px]"
        >
          {project.statusByRule}
        </Badge>
      </div>

      {/* 行 2：sessionStats */}
      <div className="mb-2 font-mono text-xs text-muted-foreground">
        total={sessionStats.total} · 1h={sessionStats.activeWithin1h} · 24h=
        {sessionStats.activeWithin24h} · 3d={sessionStats.activeWithin3d}
      </div>

      {/* 行 3：narrative */}
      {project.narrative && (
        <p className="whitespace-pre-wrap text-sm text-foreground/90">
          {project.narrative}
        </p>
      )}

      {/* 行 4：statusReason（如果与 statusByRule 等不同则显示） */}
      {project.statusReason && project.statusReason !== project.statusByRule && (
        <p className="mt-1 text-xs text-muted-foreground">
          原因：{project.statusReason}
        </p>
      )}
    </div>
  );
}
