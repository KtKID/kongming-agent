/**
 * Log viewer REST API wrapper for full-log v0.2.
 *
 * Uses the shared apiGet helper, matching the dashboard module style.
 */

import { apiGet } from "@/lib/api";
import type { LogSource, LogReadResponse } from "./types";

/** Fetch all available log sources. */
export function fetchLogSources(): Promise<LogSource[]> {
  return apiGet<LogSource[]>("/api/manage/logs/sources");
}

/** Fetch the tail content for a specific log source. */
export function fetchLogRead(params: {
  type: string;
  tail_lines?: number;
  max_bytes?: number;
  query?: string;
}): Promise<LogReadResponse> {
  const sp = new URLSearchParams({ type: params.type });
  if (params.tail_lines != null) sp.set("tail_lines", String(params.tail_lines));
  if (params.max_bytes != null) sp.set("max_bytes", String(params.max_bytes));
  if (params.query) sp.set("query", params.query);
  return apiGet<LogReadResponse>(`/api/manage/logs/read?${sp.toString()}`);
}
