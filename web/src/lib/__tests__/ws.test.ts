import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { ThreadSocket } from "@/lib/ws";

/**
 * 用 fake WebSocket 模拟连接生命周期。
 */
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
  triggerClose() {
    this.readyState = FakeWebSocket.CLOSED;
    if (this.onclose) this.onclose();
  }
}

describe("ThreadSocket", () => {
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

  it("connect → open 状态切换", () => {
    const sock = new ThreadSocket("thread-abc");
    sock.connect();
    expect(sock.getState()).toBe("connecting");
    const ws = FakeWebSocket.instances[0];
    expect(ws).toBeDefined();
    expect(ws!.url).toContain("/ws/threads/thread-abc");
    ws!.triggerOpen();
    expect(sock.getState()).toBe("open");
    sock.close();
  });

  it("listener 接收帧", () => {
    const sock = new ThreadSocket("t1");
    const fn = vi.fn();
    sock.on(fn);
    sock.connect();
    const ws = FakeWebSocket.instances[0]!;
    ws.triggerOpen();
    ws.triggerMessage({ frame_type: "pong", timestamp_ms: 1 });
    expect(fn).toHaveBeenCalledWith({ frame_type: "pong", timestamp_ms: 1 });
    sock.close();
  });

  it("非法 JSON 帧静默丢弃", () => {
    const sock = new ThreadSocket("t1");
    const fn = vi.fn();
    sock.on(fn);
    sock.connect();
    const ws = FakeWebSocket.instances[0]!;
    ws.triggerOpen();
    ws.triggerMessage("not json {{{");
    expect(fn).not.toHaveBeenCalled();
    sock.close();
  });

  it("close 后 onclose 不触发重连", () => {
    const sock = new ThreadSocket("t1");
    sock.connect();
    const ws = FakeWebSocket.instances[0]!;
    ws.triggerOpen();
    sock.close();
    // 模拟服务端断开（已在 close 内部）
    expect(FakeWebSocket.instances.length).toBe(1);
    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances.length).toBe(1); // 没新建
  });

  it("断开后指数退避重连", () => {
    const sock = new ThreadSocket("t1");
    sock.connect();
    const ws1 = FakeWebSocket.instances[0]!;
    ws1.triggerOpen();
    expect(sock.getState()).toBe("open");
    // 服务端断开
    ws1.triggerClose();
    expect(sock.getState()).toBe("reconnecting");
    // 第一次退避 1s
    vi.advanceTimersByTime(1000);
    expect(FakeWebSocket.instances.length).toBe(2);
    sock.close();
  });

  it("10 次重连后 → failed", () => {
    const sock = new ThreadSocket("t1");
    sock.connect();
    // 第一个 ws → close → 退避 1s → 新 ws → close → ... 直到 retryCount 触顶
    // MAX_RETRY=10：每次 close 后 retryCount++；retryCount>=10 时停下
    for (let i = 0; i < 11; i++) {
      const ws = FakeWebSocket.instances[i];
      if (!ws) break;
      ws.triggerClose();
      vi.advanceTimersByTime(MAX_BACKOFF_MS);
    }
    expect(sock.getState()).toBe("failed");
    sock.close();
  });

  it("send 在非 open 状态抛错", () => {
    const sock = new ThreadSocket("t1");
    expect(() =>
      sock.send({ frame_type: "ping" }),
    ).toThrowError(/cannot send/);
    sock.close();
  });

  it("send 在 open 状态写入 ws.send", () => {
    const sock = new ThreadSocket("t1");
    sock.connect();
    const ws = FakeWebSocket.instances[0]!;
    ws.triggerOpen();
    sock.send({ frame_type: "ping" });
    expect(ws.sent).toEqual([JSON.stringify({ frame_type: "ping" })]);
    sock.close();
  });
});

const MAX_BACKOFF_MS = 30_000;
