/**
 * Log viewer type definitions for full-log v0.2.
 *
 * Field names match backend DTOs in Python log_dto.py.
 */

/** Log file format hint. */
export type LogFormat = "jsonl" | "plain" | "mixed";

/** Log source metadata. */
export interface LogSource {
  type: string;
  label: string;
  format: LogFormat;
  description: string;
  path: string;
  exists: boolean;
  size_bytes?: number | null;
  updated_at_ms?: number | null;
}

/** Single log line. */
export interface LogLine {
  line_no?: number | null;
  raw: string;
  parsed?: Record<string, unknown> | null;
  parse_error?: string | null;
}

/** Tail-read response. */
export interface LogReadResponse {
  source: LogSource;
  lines: LogLine[];
  truncated: boolean;
  read_bytes: number;
  total_bytes?: number | null;
}
