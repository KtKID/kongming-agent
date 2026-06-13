import { useEffect, useState } from "react";
import { apiGet } from "@/lib/api";
import type { HeartbeatConfig } from "@/hooks/useThreadStatusWS";

/**
 * 客户端配置 DTO（后端 GET /api/config/client 返回）。
 */
interface ClientConfigDTO {
  ws_heartbeat_interval_ms: number;
  ws_heartbeat_background_interval_ms: number;
  ws_heartbeat_timeout_ms: number;
  ws_heartbeat_max_missed: number;
  dashboard_poll_interval_seconds: number;
}

/**
 * 从后端拉取心跳配置，返回 {@link HeartbeatConfig}。
 *
 * 首次 mount 时发一次 GET /api/config/client，
 * 拿到后更新返回值。请求失败时用默认值。
 */
export function useHeartbeatConfig(): HeartbeatConfig | undefined {
  const [config, setConfig] = useState<HeartbeatConfig | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;

    apiGet<ClientConfigDTO>("/api/config/client")
      .then((dto) => {
        if (cancelled) return;
        setConfig({
          intervalMs: dto.ws_heartbeat_interval_ms,
          backgroundIntervalMs: dto.ws_heartbeat_background_interval_ms,
          timeoutMs: dto.ws_heartbeat_timeout_ms,
          maxMissed: dto.ws_heartbeat_max_missed,
        });
      })
      .catch(() => {
        // 请求失败（未登录、网络错误等）→ 用默认值，不阻塞
      });

    return () => {
      cancelled = true;
    };
  }, []);

  return config;
}
