import type { HeartbeatConfig } from "@/hooks/useThreadStatusWS";
import { useClientConfig } from "@/hooks/useClientConfig";

export function useHeartbeatConfig(): HeartbeatConfig | undefined {
  return useClientConfig()?.heartbeat;
}
