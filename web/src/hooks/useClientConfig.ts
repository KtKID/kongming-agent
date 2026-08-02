import { useEffect, useState } from "react";
import { apiGet } from "@/lib/api";
import type { HeartbeatConfig } from "@/hooks/useThreadStatusWS";

interface ClientConfigDTO {
  host_environment?: string | null;
  capabilities?: ClientCapabilitiesDTO | null;
  ws_heartbeat_interval_ms: number;
  ws_heartbeat_background_interval_ms: number;
  ws_heartbeat_timeout_ms: number;
  ws_heartbeat_max_missed: number;
  dashboard_poll_interval_seconds: number;
  timezone?: string | null;
}

interface ClientCapabilitiesDTO {
  xspace_host?: boolean | null;
  native_file_dialog?: boolean | null;
}

export type WebHostEnvironment = "browser" | "xspace";

export interface WebShellCapabilities {
  xspaceHost: boolean;
  nativeFileDialog: boolean;
}

export interface ClientRuntimeConfig {
  hostEnvironment: WebHostEnvironment;
  capabilities: WebShellCapabilities;
  heartbeat: Required<HeartbeatConfig>;
  dashboardPollIntervalSeconds: number;
  timezone: string;
}

const DEFAULT_HEARTBEAT_CONFIG: Required<HeartbeatConfig> = {
  intervalMs: 30_000,
  backgroundIntervalMs: 60_000,
  timeoutMs: 10_000,
  maxMissed: 3,
};

const DEFAULT_CLIENT_CONFIG: ClientRuntimeConfig = {
  hostEnvironment: "browser",
  capabilities: {
    xspaceHost: false,
    nativeFileDialog: false,
  },
  heartbeat: DEFAULT_HEARTBEAT_CONFIG,
  dashboardPollIntervalSeconds: 5,
  timezone: "UTC",
};

function positiveFiniteOrDefault(value: number, fallback: number): number {
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

function maxMissedOrDefault(value: number, fallback: number): number {
  return Number.isFinite(value) && value >= 1 ? Math.floor(value) : fallback;
}

function normalizeTimezone(value: string | null | undefined): string {
  if (!value) return DEFAULT_CLIENT_CONFIG.timezone;
  try {
    new Intl.DateTimeFormat("zh-CN", { timeZone: value }).format(0);
    return value;
  } catch {
    return DEFAULT_CLIENT_CONFIG.timezone;
  }
}

function normalizeHostEnvironment(
  value: string | null | undefined,
): WebHostEnvironment {
  return value === "xspace" ? "xspace" : "browser";
}

function normalizeCapabilities(
  dto: ClientConfigDTO,
  hostEnvironment: WebHostEnvironment,
): WebShellCapabilities {
  const defaultValue = hostEnvironment === "xspace";
  return {
    xspaceHost:
      typeof dto.capabilities?.xspace_host === "boolean"
        ? dto.capabilities.xspace_host
        : defaultValue,
    nativeFileDialog:
      typeof dto.capabilities?.native_file_dialog === "boolean"
        ? dto.capabilities.native_file_dialog
        : defaultValue,
  };
}

function normalizeClientConfig(dto: ClientConfigDTO): ClientRuntimeConfig {
  const hostEnvironment = normalizeHostEnvironment(dto.host_environment);
  return {
    hostEnvironment,
    capabilities: normalizeCapabilities(dto, hostEnvironment),
    heartbeat: {
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
    },
    dashboardPollIntervalSeconds: positiveFiniteOrDefault(
      dto.dashboard_poll_interval_seconds,
      DEFAULT_CLIENT_CONFIG.dashboardPollIntervalSeconds,
    ),
    timezone: normalizeTimezone(dto.timezone),
  };
}

export function useClientConfig(): ClientRuntimeConfig | undefined {
  const [config, setConfig] = useState<ClientRuntimeConfig | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;

    apiGet<ClientConfigDTO>("/api/config/client")
      .then((dto) => {
        if (cancelled) return;
        setConfig(normalizeClientConfig(dto));
      })
      .catch(() => {
        if (cancelled) return;
        setConfig(DEFAULT_CLIENT_CONFIG);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  return config;
}

export const __clientConfigTest = {
  DEFAULT_CLIENT_CONFIG,
  normalizeCapabilities,
  normalizeClientConfig,
  normalizeHostEnvironment,
  normalizeTimezone,
};
