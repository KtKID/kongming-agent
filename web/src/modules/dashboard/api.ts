import { apiGet } from "@/lib/api";
import type { RuntimeStatusSnapshotDTO } from "@/protocol";

interface ClientConfigDTO {
  ws_heartbeat_interval_ms: number;
  ws_heartbeat_timeout_ms: number;
  ws_heartbeat_max_missed: number;
  dashboard_poll_interval_seconds: number;
}

export function fetchRuntimeStatus() {
  return apiGet<RuntimeStatusSnapshotDTO>("/api/manage/runtime-status");
}

export async function fetchDashboardPollIntervalMs(): Promise<number> {
  const config = await apiGet<ClientConfigDTO>("/api/config/client");
  return config.dashboard_poll_interval_seconds * 1000;
}
