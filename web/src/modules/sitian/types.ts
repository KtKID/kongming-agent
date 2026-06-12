/**
 * Sitian 司天报告 DTO（前端真源）
 *
 * 保持与后端 `.kongming/sitian/sitian_report.json` 原样字段，
 * 不在前端做字段名映射（后端用什么命名前端就用什么）。
 *
 * 字段说明详见：dev-pipeline/tasks/sitian-web-report-dialog/README.md "DTO 字段对齐"
 */

// ---------------------------------------------------------------------------
// 顶部报告
// ---------------------------------------------------------------------------

export type AlertPriority = "P0" | "P1" | "P2";
export type AlertSeverity = "high" | "medium" | "low";

export interface SessionRef {
  projectId: string;
  projectName: string;
  sessionId: string;
}

export interface EvidenceItem {
  quote: string;
  threadId?: string;
}

export interface DispatchInfo {
  instructionDraft: string;
  targetThreadId: string;
}

export interface TopAlert {
  alertId: string;
  dedupeKey: string;
  priority: AlertPriority;
  severity: AlertSeverity;
  title: string;
  displayMessage: string;
  recommendation: string;
  sessionRef: SessionRef;
  evidence: EvidenceItem[];
  /** 前端目前不使用，保留字段以便未来扩展。 */
  dispatch?: DispatchInfo;
  severityReasons?: string[];
}

// ---------------------------------------------------------------------------
// 项目状态
// ---------------------------------------------------------------------------

export interface SessionStats {
  total: number;
  activeWithin1h: number;
  activeWithin24h: number;
  activeWithin3d: number;
}

export interface ProjectStatus {
  projectId: string;
  projectName: string;
  statusByRule: string;
  statusReason: string;
  narrative: string;
  alertIds: string[];
  sessionStats: SessionStats;
}

// ---------------------------------------------------------------------------
// 顶层报告
// ---------------------------------------------------------------------------

export interface SitianReport {
  reportId: string;
  generatedAt: string; // ISO
  modelName: string;
  summary: string;
  errors: string[];
  topAlerts: TopAlert[];
  projects: ProjectStatus[];
}
