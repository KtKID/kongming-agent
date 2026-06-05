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

const DEFAULT_HEARTBEAT_CONFIG: Required<HeartbeatConfig> = {
  intervalMs: 30_000,
  backgroundIntervalMs: 60_000,
  timeoutMs: 10_000,
  maxMissed: 3,
};

function positiveFiniteOrDefault(value: number, fallback: number): number {
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

function maxMissedOrDefault(value: number, fallback: number): number {
  return Number.isFinite(value) && value >= 1 ? Math.floor(value) : fallback;
}

function normalizeHeartbeatConfig(dto: ClientConfigDTO): Required<HeartbeatConfig> {
  return {
    intervalMs: positiveFiniteOrDefault(
      dto.ws_heartbeat_interval_ms,
      DEFAULT_HEARTBEAT_CONFIG.intervalMs,
    ),
    backgroundIntervalMs: positiveFiniteOrDefault(
      dto.ws_heartbeat_background_interval_ms,
      DEFAULT_HEARTBEAT_CONFIG.backgroundIntervalMs,
    ),
    timeoutMs: positiveFiniteOrDefault(
      dto.ws_heartbeat_timeout_ms,
      DEFAULT_HEARTBEAT_CONFIG.timeoutMs,
    ),
    maxMissed: maxMissedOrDefault(
      dto.ws_heartbeat_max_missed,
      DEFAULT_HEARTBEAT_CONFIG.maxMissed,
    ),
  };
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
        setConfig(normalizeHeartbeatConfig(dto));
      })
      .catch(() => {
        if (cancelled) return;
        // 请求失败（未登录、网络错误等）→ 用默认值，不阻塞
        setConfig(DEFAULT_HEARTBEAT_CONFIG);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  return config;
}
