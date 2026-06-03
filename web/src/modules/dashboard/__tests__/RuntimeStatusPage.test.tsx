import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RuntimeStatusPage } from "@/modules/dashboard/components/RuntimeStatusPage";
import { useRuntimeStatusStore } from "@/modules/dashboard/store";

vi.mock("@/modules/dashboard/api", () => ({
  fetchDashboardPollIntervalMs: vi.fn().mockResolvedValue(5000),
}));

const refreshMock = vi.fn();

beforeEach(() => {
  refreshMock.mockReset().mockResolvedValue(undefined);
  useRuntimeStatusStore.setState({
    snapshot: {
      process: {
        running: true,
        pid: 123,
        host: "0.0.0.0",
        port: 60000,
        url: "http://localhost:60000",
        log_path: "/tmp/server.log",
      },
      polling: { interval_seconds: 5 },
      global_ws: {
        thread_status_connections: 2,
        cron_connections: 2,
        approval_subscribers: 2,
      },
      provider_sessions: {
        claude_active_sessions: 1,
        codex_active_sessions: 0,
      },
      cells_total: 1,
      chat_ws_connections_total: 2,
      approval_pending_total: 0,
      workspace_shell_connections: null,
      cells: [
        {
          thread_id: "thread-aaaaaaaaaaaa",
          thread_name: "demo",
          backend_kind: "generic_chat",
          preset_id: "p1",
          cwd: "/tmp/demo",
          created_at: 1,
          last_active_at: 2,
          pending_approval_count: 0,
          status: "running",
          chat_ws_connections: 2,
        },
      ],
      generated_at_ms: 1710000000000,
    },
    loading: false,
    error: null,
    refresh: refreshMock,
  });
});

describe("RuntimeStatusPage", () => {
  it("渲染 summary 和 active cells", async () => {
    render(<RuntimeStatusPage />);
    await waitFor(() => expect(refreshMock).toHaveBeenCalled());
    expect(screen.getByText("Active Cells")).toBeInTheDocument();
    expect(screen.getByText("demo")).toBeInTheDocument();
    expect(screen.getByText("thread-aaaaaaaaaaaa")).toBeInTheDocument();
    expect(screen.getByText("chat ws")).toBeInTheDocument();
  });
});
