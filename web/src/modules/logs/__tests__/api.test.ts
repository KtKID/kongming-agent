import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiGet } from "@/lib/api";
import { fetchLogRead, fetchLogSources } from "../api";

vi.mock("@/lib/api", () => ({
  apiGet: vi.fn(),
}));

describe("logs api", () => {
  beforeEach(() => {
    vi.mocked(apiGet).mockReset();
    vi.mocked(apiGet).mockResolvedValue([]);
  });

  it("fetches sources with optional thread context", async () => {
    await fetchLogSources({ threadId: "thread-abcdef123456" });

    expect(apiGet).toHaveBeenCalledWith(
      "/api/manage/logs/sources?thread_id=thread-abcdef123456",
    );
  });

  it("reads session conversation with thread context", async () => {
    vi.mocked(apiGet).mockResolvedValue({
      source: {
        type: "session_conversation",
        label: "Session Conversation",
        format: "jsonl",
        description: "",
        path: "/tmp/session.jsonl",
        exists: true,
      },
      lines: [],
      truncated: false,
      read_bytes: 0,
      total_bytes: 0,
    });

    await fetchLogRead({
      type: "session_conversation",
      tail_lines: 200,
      threadId: "thread-abcdef123456",
    });

    expect(apiGet).toHaveBeenCalledWith(
      "/api/manage/logs/read?type=session_conversation&tail_lines=200&thread_id=thread-abcdef123456",
    );
  });
});
