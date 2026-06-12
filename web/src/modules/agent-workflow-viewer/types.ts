export type WorkflowDiagnosticSeverity = "info" | "warning" | "error";
export type WorkflowArtifactKind =
  | "json"
  | "jsonl"
  | "markdown"
  | "text"
  | "directory";
export type WorkflowPanelKind =
  | "summary"
  | "table"
  | "markdown"
  | "json"
  | "timeline"
  | "review_board"
  | "map_reduce";

export interface WorkflowDiagnosticDTO {
  code: string;
  severity: WorkflowDiagnosticSeverity;
  message: string;
  path: string | null;
}

export interface WorkflowArtifactRefDTO {
  artifact_id: string;
  path: string;
  kind: WorkflowArtifactKind;
  title: string;
  size_bytes: number | null;
  available: boolean;
  missing_reason: string | null;
}

export interface WorkflowArtifactContentDTO {
  artifact_id: string;
  path: string;
  kind: string;
  title: string;
  content: unknown;
  truncated: boolean;
  diagnostics: WorkflowDiagnosticDTO[];
}

export interface WorkflowUsageRecordDTO {
  task_run_id: string | null;
  task_id: string | null;
  task_name: string | null;
  session_id: string | null;
  run_id: string | null;
  status: string | null;
  provider: string;
  source: string;
  usage: Record<string, number>;
}

export interface WorkflowUsageDTO {
  source: string;
  totals: Record<string, number>;
  provider_totals: Record<string, Record<string, number>>;
  records: WorkflowUsageRecordDTO[];
  diagnostics: WorkflowDiagnosticDTO[];
}

export interface WorkflowListItemDTO {
  workflow_id: string;
  thread_id: string;
  mode: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  desc: string | null;
  title: string;
  report_count: number;
  has_mode_panel: boolean;
  usage: WorkflowUsageDTO;
  diagnostics: WorkflowDiagnosticDTO[];
}

export interface WorkflowListDTO {
  thread_id: string;
  workflows: WorkflowListItemDTO[];
}

export interface WorkflowTimelineEventDTO {
  event_id: string;
  timestamp: string | null;
  action: string;
  label: string;
  payload: Record<string, unknown>;
}

export interface WorkflowFlowNodeDTO {
  id: string;
  label: string;
  kind: string;
  status: string | null;
  metadata: Record<string, unknown>;
}

export interface WorkflowFlowEdgeDTO {
  id: string;
  source: string;
  target: string;
  label: string | null;
}

export interface SubAgentReportSummaryDTO {
  task_run_id: string;
  task_id: string | null;
  task_name: string | null;
  status: string | null;
  summary: string | null;
  error_message: string | null;
  report_path: string | null;
  working_dir: string | null;
  session_id: string | null;
  run_id: string | null;
  reported_at: string | null;
  usage: Record<string, number>;
  conversation_available: boolean;
  conversation_source: string | null;
  diagnostics: WorkflowDiagnosticDTO[];
}

export interface WorkflowPanelDTO {
  panel_id: string;
  mode: string;
  kind: WorkflowPanelKind;
  title: string;
  payload: Record<string, unknown>;
  available: boolean;
  missing_reason: string | null;
}

export interface WorkflowDetailDTO {
  item: WorkflowListItemDTO;
  timeline: WorkflowTimelineEventDTO[];
  flow_nodes: WorkflowFlowNodeDTO[];
  flow_edges: WorkflowFlowEdgeDTO[];
  reports: SubAgentReportSummaryDTO[];
  panels: WorkflowPanelDTO[];
  artifacts: WorkflowArtifactRefDTO[];
  usage: WorkflowUsageDTO;
  diagnostics: WorkflowDiagnosticDTO[];
}

export interface ConversationMessageDTO {
  record_index: number;
  role: string;
  content: string;
  created_at: number | string | null;
  message_type: string | null;
  tool_calls: Record<string, unknown>[];
  usage: Record<string, unknown> | null;
  raw: Record<string, unknown>;
}

export interface ConversationDTO {
  thread_id: string;
  workflow_id: string;
  task_run_id: string;
  child_session_id: string | null;
  source_path: string | null;
  messages: ConversationMessageDTO[];
  next_cursor: string | null;
  diagnostics: WorkflowDiagnosticDTO[];
}
