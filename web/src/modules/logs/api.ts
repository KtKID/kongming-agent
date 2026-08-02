/**
 * Log viewer REST API wrapper for full-log v0.2.
 *
 * Uses the shared apiGet helper, matching the dashboard module style.
 */

import { apiGet } from "@/lib/api";
import type { LogSource, LogReadResponse } from "./types";

/** Fetch all available log sources. */
export function fetchLogSources(params?: {
  threadId?: string | null;
}): Promise<LogSource[]> {
  const sp = new URLSearchParams();
  if (params?.threadId) sp.set("thread_id", params.threadId);
  const suffix = sp.toString();
  return apiGet<LogSource[]>(
    suffix ? `/api/manage/logs/sources?${suffix}` : "/api/manage/logs/sources",
  );
}

/** Fetch the tail content for a specific log source. */
export function fetchLogRead(params: {
  type: string;
  tail_lines?: number;
  max_bytes?: number;
  query?: string;
  threadId?: string | null;
}): Promise<LogReadResponse> {
  const sp = new URLSearchParams({ type: params.type });
  if (params.tail_lines != null) sp.set("tail_lines", String(params.tail_lines));
  if (params.max_bytes != null) sp.set("max_bytes", String(params.max_bytes));
  if (params.query) sp.set("query", params.query);
  if (params.threadId) sp.set("thread_id", params.threadId);
  return apiGet<LogReadResponse>(`/api/manage/logs/read?${sp.toString()}`);
}
