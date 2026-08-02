import type { ReactNode } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LogViewerOverlay } from "../components/LogViewerOverlay";
import { useLogViewerStore } from "../store";
import * as api from "../api";
import type { LogSource } from "../types";

vi.mock("../api", () => ({
  fetchLogSources: vi.fn(),
  fetchLogRead: vi.fn(),
}));

vi.mock("@/components/ui/dialog", () => ({
  Dialog: ({ open, children }: { open: boolean; children: ReactNode }) =>
    open ? <div>{children}</div> : null,
  DialogContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: ReactNode }) => <h2>{children}</h2>,
  DialogDescription: ({ children }: { children: ReactNode }) => <p>{children}</p>,
}));

vi.mock("@/components/ui/scroll-area", () => ({
  ScrollArea: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));

const THREAD_ID = "thread-abcdef123456";

const sessionSource: LogSource = {
  type: "session_conversation",
  label: "Session Conversation",
  format: "jsonl",
  description: "当前 thread 的 FileSession 完整对话记录",
  path: "/tmp/.kongming/sessions/thread-abcdef123456/thread-abcdef123456.jsonl",
  exists: true,
  size_bytes: 120,
  updated_at_ms: 1_720_000_000_000,
};

const webSource: LogSource = {
  type: "web_server",
  label: "Web Server Log",
  format: "plain",
  description: "Web 服务 stdout/stderr",
  path: "/tmp/.kongming/web/server.log",
  exists: false,
};

describe("LogViewerOverlay", () => {
  beforeEach(() => {
    act(() => {
      useLogViewerStore.getState().reset();
      useLogViewerStore.getState().open();
    });
    vi.mocked(api.fetchLogSources).mockReset();
    vi.mocked(api.fetchLogRead).mockReset();
    vi.mocked(api.fetchLogSources).mockResolvedValue([webSource, sessionSource]);
    vi.mocked(api.fetchLogRead).mockResolvedValue({
      source: sessionSource,
      lines: [
        {
          raw: '{"record_type":"message","message":{"role":"user","content":"hello"}}',
          parsed: {
            record_type: "message",
            message: { role: "user", content: "hello" },
          },
        },
      ],
      truncated: false,
      read_bytes: 120,
      total_bytes: 120,
    });
  });

  afterEach(() => {
    act(() => {
      useLogViewerStore.getState().reset();
    });
  });

  it("shows Session as an independent source group and keeps thread context", async () => {
    render(<LogViewerOverlay activeThreadId={THREAD_ID} />);

    expect(await screen.findByText("Session")).toBeInTheDocument();
    expect(screen.getByText("Session Conversation")).toBeInTheDocument();
    expect(api.fetchLogSources).toHaveBeenCalledWith({ threadId: THREAD_ID });

    await waitFor(() => {
      expect(api.fetchLogRead).toHaveBeenCalledWith(
        expect.objectContaining({
          type: "session_conversation",
          threadId: THREAD_ID,
        }),
      );
    });
  });
});
