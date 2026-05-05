import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { ClaudeCodeSocket } from "@/lib/claude-ws";

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
    if (this.onmessage)
      this.onmessage(
        new MessageEvent("message", {
          data: typeof data === "string" ? data : JSON.stringify(data),
        }),
      );
  }
}

describe("ClaudeCodeSocket", () => {
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

  it("URL 走 /ws/claude-code?thread_id=...", () => {
    const sock = new ClaudeCodeSocket("thread-abcdef123456");
    sock.connect();
    const ws = FakeWebSocket.instances[0];
    expect(ws).toBeDefined();
    expect(ws!.url).toContain("/ws/claude-code");
    expect(ws!.url).toContain("thread_id=thread-abcdef123456");
    sock.close();
  });

  it("listener 接收 NormalizedMessage 帧", () => {
    const sock = new ClaudeCodeSocket("thread-aaaaaa111111");
    const fn = vi.fn();
    sock.on(fn);
    sock.connect();
    const ws = FakeWebSocket.instances[0]!;
    ws.triggerOpen();
    ws.triggerMessage({ kind: "text", content: "hi", provider: "claude" });
    expect(fn).toHaveBeenCalledWith({
      kind: "text",
      content: "hi",
      provider: "claude",
    });
    sock.close();
  });

  it("send claude-command 序列化为 JSON 字符串", () => {
    const sock = new ClaudeCodeSocket("thread-bbbbbb222222");
    sock.connect();
    const ws = FakeWebSocket.instances[0]!;
    ws.triggerOpen();
    sock.send({
      type: "claude-command",
      command: "hello",
      options: { model: "sonnet" },
    });
    expect(ws.sent).toHaveLength(1);
    const parsed = JSON.parse(ws.sent[0]!);
    expect(parsed).toEqual({
      type: "claude-command",
      command: "hello",
      options: { model: "sonnet" },
    });
    sock.close();
  });

  it("send 在未 open 时抛错", () => {
    const sock = new ClaudeCodeSocket("thread-cccccc333333");
    expect(() =>
      sock.send({ type: "abort-session", sessionId: "x" }),
    ).toThrow();
  });

  it("非法 JSON 帧静默丢弃", () => {
    const sock = new ClaudeCodeSocket("thread-dddddd444444");
    const fn = vi.fn();
    sock.on(fn);
    sock.connect();
    const ws = FakeWebSocket.instances[0]!;
    ws.triggerOpen();
    ws.triggerMessage("not json {{{");
    expect(fn).not.toHaveBeenCalled();
    sock.close();
  });

  it("close() 后 onclose 不触发重连", () => {
    const sock = new ClaudeCodeSocket("thread-eeeeee555555");
    sock.connect();
    const ws = FakeWebSocket.instances[0]!;
    ws.triggerOpen();
    sock.close();
    expect(FakeWebSocket.instances.length).toBe(1);
    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances.length).toBe(1);
  });
});
