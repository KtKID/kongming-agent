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
import { useConnectionStatusStore } from "@/stores/connectionStatus";

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

function resetConnectionStatusStore(): void {
  useConnectionStatusStore.setState({
    threadWsState: "closed",
    threadWsLatencyMs: null,
    threadWsActive: false,
    claudeWsState: "closed",
    claudeWsLatencyMs: null,
    claudeWsActive: false,
    statusWsState: "closed",
    statusWsLatencyMs: null,
  });
}

describe("NetworkManager", () => {
  const RealWS = globalThis.WebSocket;

  beforeEach(() => {
    FakeWebSocket.instances = [];
    (globalThis as { WebSocket: unknown }).WebSocket = FakeWebSocket;
    vi.useFakeTimers();
    vi.setSystemTime(0);
    resetConnectionStatusStore();
  });

  afterEach(() => {
    (globalThis as { WebSocket: unknown }).WebSocket = RealWS;
    vi.useRealTimers();
  });

  it("openChannel_throws_before_configure", () => {
    const m = makeManager();
    expect(() => m.openChannel("claude", "thread-abc")).toThrow(/configure/);
    expect(() => m.openChannel("generic", "thread-abc")).toThrow(/configure/);
  });

  it("configure_rejects_invalid_heartbeat_config", () => {
    const m = makeManager();
    expect(() =>
      m.configure({
        intervalMs: Number.NaN,
        backgroundIntervalMs: 60_000,
        timeoutMs: 10_000,
        maxMissed: 3,
      }),
    ).toThrow(/invalid heartbeat config/);
    expect(() =>
      m.configure({
        intervalMs: 30_000,
        backgroundIntervalMs: 60_000,
        timeoutMs: 0,
        maxMissed: 3,
      }),
    ).toThrow(/invalid heartbeat config/);
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

    // open 之后 Heartbeat 立即 probe，首轮超时后 30s interval 再发一帧
    ws.triggerOpen();
    vi.advanceTimersByTime(30_000);
    const sent = ws.sent.filter((s) => s.includes('"ping"'));
    expect(sent.length).toBe(2);
    handle.close();
  });

  it("generic_channel_uses_thread_path_without_thread_id_query", () => {
    const m = makeManager();
    m.configure(CONFIG);
    const handle = m.openChannel("generic", "thread/generic 1");
    expect(FakeWebSocket.instances.length).toBe(1);
    const ws = FakeWebSocket.instances[0]!;
    expect(ws.url).toContain("/ws/threads/thread%2Fgeneric%201");
    expect(ws.url).not.toContain("thread_id=");
    expect(handle.connId).toMatch(/^generic:thread\/generic 1:[0-9a-f]{8}$/);
    expect(useConnectionStatusStore.getState().threadWsActive).toBe(true);
    handle.close();
  });

  it("cron_run_channel_uses_task_and_run_path", () => {
    const m = makeManager();
    m.configure(CONFIG);
    const handle = m.openChannel("cron-run", "task 1:run/1");
    expect(FakeWebSocket.instances.length).toBe(1);
    const ws = FakeWebSocket.instances[0]!;
    expect(ws.url).toContain("/ws/cron/tasks/task%201/runs/run%2F1");
    expect(handle.connId).toMatch(/^cron-run:task 1:run\/1:[0-9a-f]{8}$/);
    handle.close();
  });

  it("generic_channel_reuses_same_kind_and_thread_record", () => {
    const m = makeManager();
    m.configure(CONFIG);
    const h1 = m.openChannel("generic", "thread-reuse");
    const h2 = m.openChannel("generic", "thread-reuse");
    expect(FakeWebSocket.instances.length).toBe(1);
    expect(h2.connId).toBe(h1.connId);
    expect(m.connectionCount).toBe(1);
    h1.close();
    expect(m.connectionCount).toBe(0);
  });

  it("generic_channel_updates_thread_status_and_intercepts_pong", () => {
    const m = makeManager();
    m.configure(CONFIG);
    const handle = m.openChannel("generic", "thread-status");
    const ws = FakeWebSocket.instances[0]!;
    const messages = vi.fn();
    handle.onMessage(messages);

    expect(useConnectionStatusStore.getState().threadWsActive).toBe(true);
    expect(useConnectionStatusStore.getState().threadWsState).toBe("connecting");

    ws.triggerOpen();
    expect(useConnectionStatusStore.getState().threadWsState).toBe("open");
    const ping = JSON.parse(ws.sent.at(-1)!);
    vi.setSystemTime(42);
    ws.triggerMessage({ frame_type: "pong", ts: ping.ts });
    expect(messages).not.toHaveBeenCalled();
    expect(useConnectionStatusStore.getState().threadWsLatencyMs).toBe(42);

    ws.triggerMessage({ frame_type: "thread.history", messages: [] });
    expect(messages).toHaveBeenCalledTimes(1);

    handle.close();
    expect(useConnectionStatusStore.getState()).toMatchObject({
      threadWsActive: false,
      threadWsState: "closed",
      threadWsLatencyMs: null,
    });
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
    const ts1 = JSON.parse(ws1.sent.at(-1)!).ts as number;
    const ts2 = JSON.parse(ws2.sent.at(-1)!).ts as number;
    ws1.triggerMessage({ frame_type: "pong", ts: ts1 });
    ws2.triggerMessage({ frame_type: "pong", ts: ts2 });
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

  it("generic_pong_updates_thread_latency_and_does_not_reach_business_listeners", () => {
    const m = makeManager();
    m.configure(CONFIG);
    const handle = m.openChannel("generic", "thread-pong");
    const ws = FakeWebSocket.instances[0]!;
    const listener = vi.fn();
    handle.onMessage(listener);
    ws.triggerOpen();

    const pingFrame = JSON.parse(ws.sent.at(-1)!) as { frame_type: string; ts: number };
    expect(pingFrame.frame_type).toBe("ping");
    vi.setSystemTime(pingFrame.ts + 42);
    ws.triggerMessage({ frame_type: "pong", ts: pingFrame.ts });

    expect(listener).not.toHaveBeenCalled();
    expect(useConnectionStatusStore.getState().threadWsLatencyMs).toBe(42);
    handle.close();
  });

  it("message_listener_fanout_isolated_when_one_listener_throws", () => {
    const m = makeManager();
    m.configure(CONFIG);
    const handle = m.openChannel("generic", "thread-fanout");
    const ws = FakeWebSocket.instances[0]!;
    const throwing = vi.fn(() => {
      throw new Error("listener boom");
    });
    const receiving = vi.fn();
    handle.onMessage(throwing);
    handle.onMessage(receiving);

    ws.triggerMessage({ frame_type: "content.delta", delta: "ok" });

    expect(throwing).toHaveBeenCalledTimes(1);
    expect(receiving).toHaveBeenCalledTimes(1);
    expect(receiving.mock.calls[0]![0]).toMatchObject({ delta: "ok" });
    handle.close();
  });

  it("generic_state_aggregation_updates_thread_ws_state_and_cleanup", () => {
    const m = makeManager();
    m.configure(CONFIG);
    const handle = m.openChannel("generic", "thread-status");
    const ws = FakeWebSocket.instances[0]!;

    expect(useConnectionStatusStore.getState()).toMatchObject({
      threadWsActive: true,
      threadWsState: "connecting",
      threadWsLatencyMs: null,
    });

    ws.triggerOpen();
    expect(useConnectionStatusStore.getState().threadWsState).toBe("open");
    const pingFrame = JSON.parse(ws.sent.at(-1)!) as { ts: number };
    vi.setSystemTime(pingFrame.ts + 15);
    ws.triggerMessage({ frame_type: "pong", ts: pingFrame.ts });
    expect(useConnectionStatusStore.getState().threadWsLatencyMs).toBe(15);

    handle.close();
    expect(useConnectionStatusStore.getState()).toMatchObject({
      threadWsActive: false,
      threadWsState: "closed",
      threadWsLatencyMs: null,
    });
  });

  it("generic_failed_state_resets_thread_latency", () => {
    const m = makeManager();
    m.configure(CONFIG);
    const handle = m.openChannel("generic", "thread-failed");
    const ws = FakeWebSocket.instances[0]!;
    useConnectionStatusStore.getState().setThreadWsLatency(123);

    ws.triggerClose(1008);

    expect(useConnectionStatusStore.getState()).toMatchObject({
      threadWsState: "failed",
      threadWsLatencyMs: null,
    });
    expect(m.connectionCount).toBe(0);

    const nextHandle = m.openChannel("generic", "thread-failed");
    expect(nextHandle.connId).not.toBe(handle.connId);
    expect(FakeWebSocket.instances.length).toBe(2);
    nextHandle.close();
  });

  it("terminal_close_code_1000_retires_record_so_next_open_reconnects", () => {
    const m = makeManager();
    m.configure(CONFIG);
    const handle = m.openChannel("generic", "thread-normal-close");
    const ws = FakeWebSocket.instances[0]!;

    ws.triggerClose(1000);

    expect(useConnectionStatusStore.getState()).toMatchObject({
      threadWsActive: false,
      threadWsState: "closed",
      threadWsLatencyMs: null,
    });
    expect(m.connectionCount).toBe(0);

    const nextHandle = m.openChannel("generic", "thread-normal-close");
    expect(nextHandle.connId).not.toBe(handle.connId);
    expect(FakeWebSocket.instances.length).toBe(2);
    nextHandle.close();
  });

  it("max_retry_failure_retires_record_so_next_open_reconnects", () => {
    const m = makeManager();
    m.configure(CONFIG);
    const handle = m.openChannel("generic", "thread-max-retry");

    for (let i = 0; i < 10; i += 1) {
      const ws = FakeWebSocket.instances.at(-1)!;
      ws.triggerClose(1006);
      vi.advanceTimersByTime(30_000);
    }

    const lastWs = FakeWebSocket.instances.at(-1)!;
    lastWs.triggerClose(1006);

    expect(useConnectionStatusStore.getState()).toMatchObject({
      threadWsActive: false,
      threadWsState: "failed",
      threadWsLatencyMs: null,
    });
    expect(m.connectionCount).toBe(0);

    const nextHandle = m.openChannel("generic", "thread-max-retry");
    expect(nextHandle.connId).not.toBe(handle.connId);
    expect(FakeWebSocket.instances.at(-1)).not.toBe(lastWs);
    nextHandle.close();
  });

  it("generic_close_code_1006_reconnects_and_close_cleans_retry", () => {
    const m = makeManager();
    m.configure(CONFIG);
    const handle = m.openChannel("generic", "thread-reconnect");
    const ws = FakeWebSocket.instances[0]!;
    ws.triggerOpen();

    ws.triggerClose(1006);
    expect(useConnectionStatusStore.getState().threadWsState).toBe("reconnecting");
    vi.advanceTimersByTime(999);
    expect(FakeWebSocket.instances.length).toBe(1);

    handle.close();
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances.length).toBe(1);
    expect(useConnectionStatusStore.getState()).toMatchObject({
      threadWsActive: false,
      threadWsState: "closed",
      threadWsLatencyMs: null,
    });
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
