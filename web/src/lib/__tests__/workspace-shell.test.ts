import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkspaceShellSocket } from "@/lib/workspace-shell";

class FakeWebSocket {
  static OPEN = 1;
  static CLOSED = 3;
  static instances: FakeWebSocket[] = [];
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  sent: string[] = [];

  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED;
    if (this.onclose) this.onclose();
  }

  triggerOpen() {
    this.readyState = FakeWebSocket.OPEN;
    if (this.onopen) this.onopen();
  }

  triggerMessage(data: unknown) {
    if (this.onmessage) {
      this.onmessage(
        new MessageEvent("message", {
          data: typeof data === "string" ? data : JSON.stringify(data),
        }),
      );
    }
  }
}

describe("WorkspaceShellSocket", () => {
  const RealWS = globalThis.WebSocket;

  beforeEach(() => {
    FakeWebSocket.instances = [];
    (globalThis as { WebSocket: unknown }).WebSocket = FakeWebSocket;
    vi.useFakeTimers();
  });

  afterEach(() => {
    (globalThis as { WebSocket: unknown }).WebSocket = RealWS;
    vi.useRealTimers();
  });

  it("URL 走 /ws/workspace-shell?thread_id=...", () => {
    const socket = new WorkspaceShellSocket("thread-abcdef123456");
    socket.connect();
    expect(FakeWebSocket.instances[0]?.url).toContain("/ws/workspace-shell");
    expect(FakeWebSocket.instances[0]?.url).toContain(
      "thread_id=thread-abcdef123456",
    );
    socket.close();
  });

  it("listener 接收 shell 帧", () => {
    const socket = new WorkspaceShellSocket("thread-abcdef123456");
    const listener = vi.fn();
    socket.on(listener);
    socket.connect();
    const ws = FakeWebSocket.instances[0]!;
    ws.triggerOpen();
    ws.triggerMessage({ type: "shell-output", data: "hello" });
    expect(listener).toHaveBeenCalledWith({ type: "shell-output", data: "hello" });
    socket.close();
  });
});
