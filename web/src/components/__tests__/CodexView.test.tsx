import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CodexView } from "@/components/CodexView";
import type { NormalizedMessage, ThreadMetadataDTO } from "@/protocol";
import { useThreadsStore } from "@/stores/threads";

const mockApiGet = vi.fn();
const mockUseCodexWS = vi.fn();

vi.mock("@/lib/api", () => ({
  apiGet: (...args: unknown[]) => mockApiGet(...args),
}));

vi.mock("@/hooks/useCodexWS", () => ({
  useCodexWS: (...args: unknown[]) => mockUseCodexWS(...args),
}));

interface SocketHandle {
  socket: { on: ReturnType<typeof vi.fn>; send: ReturnType<typeof vi.fn> };
  inject: (frame: NormalizedMessage) => void;
}

function makeSocket(): SocketHandle {
  let listener: ((frame: NormalizedMessage) => void) | null = null;
  const socket = {
    on: vi.fn((cb: (frame: NormalizedMessage) => void) => {
      listener = cb;
      return () => {
        listener = null;
      };
    }),
    send: vi.fn(),
  };
  return {
    socket,
    inject: (frame) => {
      act(() => {
        listener?.(frame);
      });
    },
  };
}

describe("CodexView", () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    mockUseCodexWS.mockReset();
    useThreadsStore.setState({
      pendingNewSession: null,
      initialMessage: null,
    });
  });

  it("pending 模式下发送首条消息时才创建 codex thread", async () => {
    const createThread = vi.fn().mockResolvedValue({ id: "thread-new" });
    mockUseCodexWS.mockReturnValue({
      socket: null,
      state: "closed",
    });
    useThreadsStore.setState({
      pendingNewSession: {
        cwd: "/foo/codex",
        projectName: "codex-bar",
        backendKind: "codex",
      },
      createThread,
    });

    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route path="/chat" element={<CodexView />} />
          <Route path="/chat/:thread_id" element={<div data-testid="codex-target" />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(createThread).not.toHaveBeenCalled();

    await userEvent.type(screen.getByLabelText("消息输入"), "启动 Codex");
    await userEvent.click(screen.getByTestId("composer-send"));

    await waitFor(() =>
      expect(createThread).toHaveBeenCalledWith(
        "codex-bar",
        "",
        "codex",
        "/foo/codex",
      ),
    );
    expect(useThreadsStore.getState().initialMessage).toBe("启动 Codex");
    expect(useThreadsStore.getState().pendingNewSession).toBeNull();
    expect(await screen.findByTestId("codex-target")).toBeInTheDocument();
  });

  it("创建后的首条消息会在 codex websocket 打开后自动发出", async () => {
    const handle = makeSocket();
    const fetchThreads = vi.fn();
    mockUseCodexWS.mockReturnValue({
      socket: handle.socket,
      state: "open",
    });
    mockApiGet.mockResolvedValue({ messages: [] });
    useThreadsStore.setState({
      pendingNewSession: null,
      initialMessage: "自动补发首条消息",
      fetchThreads,
    });

    render(
      <MemoryRouter>
        <CodexView
          threadId="thread-1"
          thread={{ codex_thread_id: "" } as ThreadMetadataDTO}
        />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(handle.socket.send).toHaveBeenCalledWith({
        frame_type: "codex-command",
        command: "自动补发首条消息",
        options: {
          permissionMode: "acceptEdits",
        },
      }),
    );
    expect(fetchThreads).toHaveBeenCalled();
    expect(useThreadsStore.getState().initialMessage).toBeNull();
    expect(await screen.findByText("自动补发首条消息")).toBeInTheDocument();
  });
});
