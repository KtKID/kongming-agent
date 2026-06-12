/**
 * sitian 模块公开导出入口
 *
 * 外部模块（Layout / router 等）只从这里导入。
 *
 * 禁止 re-export manager / store / api —— 组件层只能通过 useSitian() 访问。
 */

export { SitianReportEntryButton } from "./components/SitianReportEntryButton";
export { SitianReportDialog } from "./components/SitianReportDialog";
export { useSitian } from "./hooks/useSitian";
export type {
  SitianReport,
  TopAlert,
  ProjectStatus,
  SessionStats,
  SessionRef,
  EvidenceItem,
  AlertPriority,
  AlertSeverity,
} from "./types";
