import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useHeartbeatConfig } from "@/hooks/useHeartbeatConfig";
import { apiGet } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  apiGet: vi.fn(),
}));

const DEFAULT_CONFIG = {
  intervalMs: 30_000,
  backgroundIntervalMs: 60_000,
  timeoutMs: 10_000,
  maxMissed: 3,
};

describe("useHeartbeatConfig", () => {
  beforeEach(() => {
    vi.mocked(apiGet).mockReset();
  });

  it("uses default heartbeat config when client config request fails", async () => {
    vi.mocked(apiGet).mockRejectedValue(new Error("network down"));

    const { result } = renderHook(() => useHeartbeatConfig());

    expect(result.current).toBeUndefined();
    await waitFor(() => {
      expect(result.current).toEqual(DEFAULT_CONFIG);
    });
  });

  it("normalizes invalid numeric values to defaults", async () => {
    vi.mocked(apiGet).mockResolvedValue({
      ws_heartbeat_interval_ms: Number.NaN,
      ws_heartbeat_background_interval_ms: Number.POSITIVE_INFINITY,
      ws_heartbeat_timeout_ms: 0,
      ws_heartbeat_max_missed: -1,
      dashboard_poll_interval_seconds: 5,
    });

    const { result } = renderHook(() => useHeartbeatConfig());

    await waitFor(() => {
      expect(result.current).toEqual(DEFAULT_CONFIG);
    });
  });

  it("uses valid server heartbeat config", async () => {
    vi.mocked(apiGet).mockResolvedValue({
      ws_heartbeat_interval_ms: 15_000,
      ws_heartbeat_background_interval_ms: 45_000,
      ws_heartbeat_timeout_ms: 5_000,
      ws_heartbeat_max_missed: 2,
      dashboard_poll_interval_seconds: 5,
    });

    const { result } = renderHook(() => useHeartbeatConfig());

    await waitFor(() => {
      expect(result.current).toEqual({
        intervalMs: 15_000,
        backgroundIntervalMs: 45_000,
        timeoutMs: 5_000,
        maxMissed: 2,
      });
    });
  });
});
