/**
 * NetworkManager 单元测试（network-layer v0.1 步骤 4.x）
 *
 * 覆盖：
 * - openChannel 同时创建 socket + Heartbeat 对
 * - closeChannel 清理 socket + heartbeat
 * - 多 channel 共存互不污染（NetworkManager 内 record 隔离 + Heartbeat 独立 timer）
 * - visibilitychange visible 触发 probe（spec 03 工作流 3）
 * - 入站 pong 帧路由到正确的 Heartbeat（不串到其他连接）
 * - close code 1008（policy violation）不重连 + 状态 failed
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { NetworkManager } from "@/network/manager";
import type { HeartbeatConfig } from "@/network/heartbeat";

// ---------------------------------------------------------------------------
// FakeWebSocket：vi.stubGlobal('WebSocket', FakeWebSocket) 注入
// ---------------------------------------------------------------------------

class FakeWebSocket {
  static OPEN = 1;
  static CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: ((ev: { code: number; reason?: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  sent: string[] = [];

  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(code = 1000, reason = ""): void {
    this.readyState = FakeWebSocket.CLOSED;
    if (this.onclose) this.onclose({ code, reason });
  }

  // helpers for tests
  triggerOpen(): void {
    this.readyState = FakeWebSocket.OPEN;
    if (this.onopen) this.onopen();
  }

  triggerMessage(payload: unknown): void {
    if (this.onmessage) {
      this.onmessage(
        new MessageEvent("message", {
          data: typeof payload === "string" ? payload : JSON.stringify(payload),
        }),
      );
    }
  }

  triggerClose(code = 1006): void {
    this.readyState = FakeWebSocket.CLOSED;
    if (this.onclose) this.onclose({ code });
  }
}

const CONFIG: HeartbeatConfig = {
  intervalMs: 30_000,
  backgroundIntervalMs: 60_000,
  timeoutMs: 10_000,
  maxMissed: 3,
};

function makeManager(): NetworkManager {
  return new NetworkManager();
}

describe("NetworkManager", () => {
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

  it("openChannel_throws_before_configure", () => {
    const m = makeManager();
    expect(() => m.openChannel("claude", "thread-abc")).toThrow(/configure/);
  });

  it("openChannel_creates_socket_and_heartbeat_pair", () => {
    const m = makeManager();
    m.configure(CONFIG);
    const handle = m.openChannel("claude", "thread-abc");
    expect(FakeWebSocket.instances.length).toBe(1);
    const ws = FakeWebSocket.instances[0]!;
    expect(ws.url).toContain("/ws/claude-code");
    expect(ws.url).toContain("thread_id=thread-abc");
    expect(handle.connId).toMatch(/^claude:thread-abc:[0-9a-f]{8}$/);
    expect(m.connectionCount).toBe(1);

    // open 之后 Heartbeat 启动，30s 后能看到 ping 帧
    ws.triggerOpen();
    vi.advanceTimersByTime(30_000);
    const sent = ws.sent.filter((s) => s.includes('"ping"'));
    expect(sent.length).toBe(1);
    handle.close();
  });

  it("closeChannel_removes_both_and_stops_heartbeat", () => {
    const m = makeManager();
    m.configure(CONFIG);
    const handle = m.openChannel("claude", "thread-xyz");
    const ws = FakeWebSocket.instances[0]!;
    ws.triggerOpen();
    expect(m.connectionCount).toBe(1);
    handle.close();
    expect(m.connectionCount).toBe(0);
    // close 之后即使时钟前进很久，原 socket 也不会再发 ping
    const sentBefore = ws.sent.length;
    vi.advanceTimersByTime(120_000);
    expect(ws.sent.length).toBe(sentBefore);
  });

  it("multiple_channels_coexist_without_interference", () => {
    // 注：v0.1 ChannelKind 只有 "claude"；这里用同 kind 不同 threadId 制造 2 个连接
    const m = makeManager();
    m.configure(CONFIG);
    const h1 = m.openChannel("claude", "thread-aaa");
    const h2 = m.openChannel("claude", "thread-bbb");
    expect(FakeWebSocket.instances.length).toBe(2);
    expect(m.connectionCount).toBe(2);
    const ws1 = FakeWebSocket.instances[0]!;
    const ws2 = FakeWebSocket.instances[1]!;
    ws1.triggerOpen();
    ws2.triggerOpen();
    // h1 关掉不影响 h2
    h1.close();
    expect(m.connectionCount).toBe(1);
    vi.advanceTimersByTime(30_000);
    const ws2Pings = ws2.sent.filter((s) => s.includes('"ping"')).length;
    expect(ws2Pings).toBeGreaterThanOrEqual(1);
    h2.close();
  });

  it("visibilitychange_visible_triggers_probe_on_all_heartbeats", () => {
    const m = makeManager();
    m.configure(CONFIG);
    const h1 = m.openChannel("claude", "thread-vis-1");
    const h2 = m.openChannel("claude", "thread-vis-2");
    const ws1 = FakeWebSocket.instances[0]!;
    const ws2 = FakeWebSocket.instances[1]!;
    ws1.triggerOpen();
    ws2.triggerOpen();
    // 还没到 interval；两个 socket 应都没发 ping
    const ws1Before = ws1.sent.length;
    const ws2Before = ws2.sent.length;
    // 模拟 tab 切回前台
    Object.defineProperty(document, "hidden", {
      value: false,
      configurable: true,
    });
    document.dispatchEvent(new Event("visibilitychange"));
    // 立即对所有 heartbeat 调 probe → 各发一帧 ping
    expect(ws1.sent.length).toBe(ws1Before + 1);
    expect(ws2.sent.length).toBe(ws2Before + 1);
    expect(ws1.sent.at(-1)).toContain('"ping"');
    expect(ws2.sent.at(-1)).toContain('"ping"');
    h1.close();
    h2.close();
  });

  it("incoming_pong_frame_routed_to_correct_heartbeat", () => {
    const m = makeManager();
    m.configure(CONFIG);
    const h1 = m.openChannel("claude", "thread-routing-1");
    const h2 = m.openChannel("claude", "thread-routing-2");
    const ws1 = FakeWebSocket.instances[0]!;
    const ws2 = FakeWebSocket.instances[1]!;
    ws1.triggerOpen();
    ws2.triggerOpen();
    // 各订阅一个 message listener；pong 应该被 manager 拦下不进入 listener
    const m1 = vi.fn();
    const m2 = vi.fn();
    h1.onMessage(m1);
    h2.onMessage(m2);
    // 各发 ping
    vi.advanceTimersByTime(30_000);
    const ts1 = JSON.parse(ws1.sent.at(-1)!).ts as number;
    const ts2 = JSON.parse(ws2.sent.at(-1)!).ts as number;
    // 给 ws1 回 pong；ws2 不回
    ws1.triggerMessage({ frame_type: "pong", ts: ts1 });
    expect(m1).not.toHaveBeenCalled(); // pong 不进入业务 listener
    expect(m2).not.toHaveBeenCalled();
    // 业务帧应进入对应 listener
    ws1.triggerMessage({ frame_type: "text", content: "hi-1" });
    ws2.triggerMessage({ frame_type: "text", content: "hi-2" });
    expect(m1).toHaveBeenCalledTimes(1);
    expect(m1.mock.calls[0]![0]).toMatchObject({ content: "hi-1" });
    expect(m2).toHaveBeenCalledTimes(1);
    expect(m2.mock.calls[0]![0]).toMatchObject({ content: "hi-2" });
    // ws2 没回 pong，下一个 ping 因 pending pongTimer 不会发；ws1 拿到 pong 后可继续
    void ts2;
    h1.close();
    h2.close();
  });

  it("close_code_1008_does_not_reconnect_and_sets_failed", () => {
    const m = makeManager();
    m.configure(CONFIG);
    const states: string[] = [];
    const handle = m.openChannel("claude", "thread-policy-violation");
    handle.onState((s) => states.push(s));
    const ws = FakeWebSocket.instances[0]!;
    ws.triggerOpen();
    // policy violation 关闭
    ws.triggerClose(1008);
    expect(states.at(-1)).toBe("failed");
    // 不应重连：等 60s 后仍只有 1 个 socket
    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances.length).toBe(1);
    handle.close();
  });

  it("close_code_1006_triggers_reconnect_with_backoff", () => {
    const m = makeManager();
    m.configure(CONFIG);
    const states: string[] = [];
    const handle = m.openChannel("claude", "thread-abnormal");
    handle.onState((s) => states.push(s));
    const ws = FakeWebSocket.instances[0]!;
    ws.triggerOpen();
    // 异常断开（1006）→ 应转 reconnecting
    ws.triggerClose(1006);
    expect(states).toContain("reconnecting");
    // 1s 后应新建 socket（exponentialBackoff(0)=1000）
    vi.advanceTimersByTime(1_000);
    expect(FakeWebSocket.instances.length).toBe(2);
    handle.close();
  });

  it("send_throws_when_not_open", () => {
    const m = makeManager();
    m.configure(CONFIG);
    const handle = m.openChannel("claude", "thread-not-open");
    // 还没 triggerOpen()，readyState 还是 0
    expect(() => handle.send({ frame_type: "claude-command", command: "x" })).toThrow();
    handle.close();
  });

  it("send_serializes_to_json", () => {
    const m = makeManager();
    m.configure(CONFIG);
    const handle = m.openChannel("claude", "thread-send-ok");
    const ws = FakeWebSocket.instances[0]!;
    ws.triggerOpen();
    handle.send({ frame_type: "claude-command", command: "hello" });
    const parsed = JSON.parse(ws.sent.at(-1)!);
    expect(parsed).toEqual({ frame_type: "claude-command", command: "hello" });
    handle.close();
  });
});
