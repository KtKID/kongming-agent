import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ClaudeCodeView } from "@/components/ClaudeCodeView";
import type { NormalizedMessage, ThreadMetadataDTO } from "@/protocol";

const mockApiGet = vi.fn();
const mockUseClaudeCodeWS = vi.fn();

vi.mock("@/lib/api", () => ({
  apiGet: (...args: unknown[]) => mockApiGet(...args),
}));

vi.mock("@/hooks/useClaudeCodeWS", () => ({
  useClaudeCodeWS: (...args: unknown[]) => mockUseClaudeCodeWS(...args),
}));

describe("ClaudeCodeView", () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    mockUseClaudeCodeWS.mockReset();
  });

  it("历史消息复用通用聊天 renderer，并把 tool_result 合并到工具卡片", async () => {
    const socket = {
      on: vi.fn(() => () => {}),
      send: vi.fn(),
    };
    const history: NormalizedMessage[] = [
      {
        kind: "text",
        role: "assistant",
        content: "**历史回复**",
        timestamp: "2026-05-02T00:00:00Z",
      },
      {
        kind: "tool_use",
        toolId: "tool-1",
        toolName: "Shell",
        toolInput: { cmd: "ls" },
        timestamp: "2026-05-02T00:00:01Z",
      },
      {
        kind: "tool_result",
        toolId: "tool-1",
        content: "done",
        timestamp: "2026-05-02T00:00:02Z",
      },
    ];

    mockUseClaudeCodeWS.mockReturnValue({
      socket,
      state: "open",
    });
    mockApiGet.mockResolvedValue({ messages: history });

    render(
      <MemoryRouter>
        <ClaudeCodeView
          threadId="thread-1"
          thread={{ claude_thread_id: "sdk-1" } as ThreadMetadataDTO}
        />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(mockApiGet).toHaveBeenCalledWith(
        "/api/threads/thread-1/claude_history",
      ),
    );

    expect(await screen.findByText("历史回复")).toBeInTheDocument();

    const toolButton = await screen.findByRole("button", { name: /Shell/ });
    await userEvent.click(toolButton);

    expect(screen.getByText("done")).toBeInTheDocument();
    expect(screen.getByLabelText("消息输入")).toBeInTheDocument();
    expect(screen.getByTestId("claude-code-layout").className).toContain("min-h-0");
    expect(screen.getByTestId("claude-code-viewport").className).toContain("overflow-hidden");
  });
});
