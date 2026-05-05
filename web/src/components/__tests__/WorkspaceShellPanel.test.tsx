import { act, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { WorkspaceShellPanel } from "@/components/WorkspaceShellPanel";
import type {
  WorkspaceContextDTO,
  WorkspaceShellS2CFrame,
} from "@/protocol";

type FakeSocket = {
  send: ReturnType<typeof vi.fn>;
  on: (fn: (frame: WorkspaceShellS2CFrame) => void) => () => void;
  emit: (frame: WorkspaceShellS2CFrame) => void;
};

function createSocket(): FakeSocket {
  const listeners = new Set<(frame: WorkspaceShellS2CFrame) => void>();
  return {
    send: vi.fn(),
    on(fn) {
      listeners.add(fn);
      return () => {
        listeners.delete(fn);
      };
    },
    emit(frame) {
      for (const listener of listeners) {
        listener(frame);
      }
    },
  };
}

const hookMock = vi.hoisted(() => ({
  useWorkspaceShellWS: vi.fn(),
}));

vi.mock("@/hooks/useWorkspaceShellWS", () => ({
  useWorkspaceShellWS: hookMock.useWorkspaceShellWS,
}));

const baseContext: WorkspaceContextDTO = {
  thread_id: "thread-1",
  backend_kind: "claude_code",
  workspace_root: "/tmp/proj",
  sdk_session_id: "sdk-1",
  shell_provider: "claude_code",
  files_available: true,
  shell_available: true,
  unavailable_reason: null,
};

describe("WorkspaceShellPanel", () => {
  it("generic_chat 有 cwd 时会显示 system shell", () => {
    hookMock.useWorkspaceShellWS.mockReturnValue({ socket: null, state: "closed" });

    render(
      <WorkspaceShellPanel
        context={{
          ...baseContext,
          backend_kind: "generic_chat",
          sdk_session_id: "",
          workspace_root: "/tmp/generic",
          shell_provider: "system_shell",
        }}
      />,
    );

    expect(screen.getByText("Workspace Shell")).toBeInTheDocument();
    expect(screen.getByText("system_shell")).toBeInTheDocument();
    expect(screen.getByText("命令：workspace shell")).toBeInTheDocument();
  });

  it("thread 切换后会清空旧输出并隔离旧 socket 消息", async () => {
    const socket1 = createSocket();
    const socket2 = createSocket();

    hookMock.useWorkspaceShellWS.mockImplementation((threadId?: string) => {
      if (threadId === "thread-1") return { socket: socket1, state: "open" };
      if (threadId === "thread-2") return { socket: socket2, state: "open" };
      return { socket: null, state: "closed" };
    });

    const { rerender } = render(<WorkspaceShellPanel context={baseContext} />);

    act(() => {
      socket1.emit({ type: "shell-output", data: "hello from thread 1" });
    });
    expect(await screen.findByText("hello from thread 1")).toBeInTheDocument();

    rerender(
      <WorkspaceShellPanel
        context={{
          ...baseContext,
          thread_id: "thread-2",
          sdk_session_id: "sdk-2",
          workspace_root: "/tmp/other",
        }}
      />,
    );

    await waitFor(() => {
      expect(screen.queryByText("hello from thread 1")).toBeNull();
      expect(screen.getByText("命令：claude --resume sdk-2")).toBeInTheDocument();
    });

    act(() => {
      socket1.emit({ type: "shell-output", data: "late old output" });
    });
    await waitFor(() => {
      expect(screen.queryByText("late old output")).toBeNull();
    });

    act(() => {
      socket2.emit({ type: "shell-output", data: "hello from thread 2" });
    });
    expect(await screen.findByText("hello from thread 2")).toBeInTheDocument();
  });

  it("fallback 到 system shell 后会显示新的 provider", async () => {
    const socket = createSocket();
    hookMock.useWorkspaceShellWS.mockReturnValue({ socket, state: "open" });

    render(<WorkspaceShellPanel context={baseContext} />);

    act(() => {
      socket.emit({
        type: "shell-status",
        status: "running",
        cwd: "/tmp/proj",
        command: ["/bin/zsh", "-l"],
      });
    });

    expect(await screen.findByText("system_shell")).toBeInTheDocument();
    expect(screen.getByText("命令：/bin/zsh -l")).toBeInTheDocument();
  });
});
