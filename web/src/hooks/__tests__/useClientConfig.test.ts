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
});
