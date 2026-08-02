import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useClientConfig } from "@/hooks/useClientConfig";
import { apiGet } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  apiGet: vi.fn(),
}));

describe("useClientConfig", () => {
  beforeEach(() => {
    vi.mocked(apiGet).mockReset();
  });

  it("uses server timezone and heartbeat config", async () => {
    vi.mocked(apiGet).mockResolvedValue({
      ws_heartbeat_interval_ms: 15_000,
      ws_heartbeat_background_interval_ms: 45_000,
      ws_heartbeat_timeout_ms: 5_000,
      ws_heartbeat_max_missed: 2,
      dashboard_poll_interval_seconds: 7,
      timezone: "Asia/Shanghai",
    });

    const { result } = renderHook(() => useClientConfig());

    await waitFor(() => {
      expect(result.current).toEqual({
        hostEnvironment: "browser",
        capabilities: {
          xspaceHost: false,
          nativeFileDialog: false,
        },
        heartbeat: {
          intervalMs: 15_000,
          backgroundIntervalMs: 45_000,
          timeoutMs: 5_000,
          maxMissed: 2,
        },
        dashboardPollIntervalSeconds: 7,
        timezone: "Asia/Shanghai",
      });
    });
  });

  it("falls back to UTC when server timezone is invalid", async () => {
    vi.mocked(apiGet).mockResolvedValue({
      ws_heartbeat_interval_ms: 15_000,
      ws_heartbeat_background_interval_ms: 45_000,
      ws_heartbeat_timeout_ms: 5_000,
      ws_heartbeat_max_missed: 2,
      dashboard_poll_interval_seconds: 7,
      timezone: "Mars/Base",
    });

    const { result } = renderHook(() => useClientConfig());

    await waitFor(() => {
      expect(result.current?.timezone).toBe("UTC");
    });
  });

  it("normalizes xspace host environment and capabilities", async () => {
    vi.mocked(apiGet).mockResolvedValue({
      host_environment: "xspace",
      capabilities: {
        xspace_host: true,
        native_file_dialog: true,
      },
      ws_heartbeat_interval_ms: 15_000,
      ws_heartbeat_background_interval_ms: 45_000,
      ws_heartbeat_timeout_ms: 5_000,
      ws_heartbeat_max_missed: 2,
      dashboard_poll_interval_seconds: 7,
      timezone: "Asia/Shanghai",
    });

    const { result } = renderHook(() => useClientConfig());

    await waitFor(() => {
      expect(result.current?.hostEnvironment).toBe("xspace");
      expect(result.current?.capabilities).toEqual({
        xspaceHost: true,
        nativeFileDialog: true,
      });
    });
  });

  it("falls back to browser capabilities when client config request fails", async () => {
    vi.mocked(apiGet).mockRejectedValue(new Error("network down"));

    const { result } = renderHook(() => useClientConfig());

    await waitFor(() => {
      expect(result.current).toEqual({
        hostEnvironment: "browser",
        capabilities: {
          xspaceHost: false,
          nativeFileDialog: false,
        },
        heartbeat: {
          intervalMs: 30_000,
          backgroundIntervalMs: 60_000,
          timeoutMs: 10_000,
          maxMissed: 3,
        },
        dashboardPollIntervalSeconds: 5,
        timezone: "UTC",
      });
    });
  });
});
