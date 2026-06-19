export type ThreadArtifactKind =
  | "json"
  | "jsonl"
  | "markdown"
  | "text"
  | "directory";

export interface ThreadArtifactDiagnosticDTO {
  code: string;
  severity: "info" | "warning" | "error";
  message: string;
  path?: string | null;
}

export interface ThreadArtifactRefDTO {
  artifact_id: string;
  path: string;
  kind: ThreadArtifactKind;
  title: string;
  size_bytes?: number | null;
  available: boolean;
  record_count?: number | null;
  missing_reason?: string | null;
}

export interface ThreadArtifactContentDTO {
  artifact_id: string;
  path: string;
  kind: ThreadArtifactKind | string;
  title: string;
  content: unknown;
  truncated: boolean;
  diagnostics: ThreadArtifactDiagnosticDTO[];
}

export interface ThreadArtifactListDTO {
  thread_id: string;
  files: ThreadArtifactRefDTO[];
  diagnostics: ThreadArtifactDiagnosticDTO[];
}
