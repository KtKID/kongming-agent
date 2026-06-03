/**
 * Heartbeat 单元测试（network-layer v0.1 步骤 3.7）
 *
 * 关键覆盖：
 * - start / stop / handlePong / probe / latencyMs 基础 happy path
 * - maxMissed 触发 closeForReconnect
 * - **多实例并发**（两个 Heartbeat 独立 mock hooks，杀一不影响另）—— spec 03 核心约束
 * - onLatency 抛错不传染（listener 健壮性）
 *
 * 用 vi.useFakeTimers() + vi.advanceTimersByTime() 精确控制 interval / timeout。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { Heartbeat, type HeartbeatConfig, type HeartbeatHooks } from "@/network/heartbeat";

const CONFIG: HeartbeatConfig = {
  intervalMs: 30_000,
  backgroundIntervalMs: 60_000,
  timeoutMs: 10_000,
  maxMissed: 3,
};

function makeHooks(overrides: Partial<HeartbeatHooks> = {}): {
  hooks: HeartbeatHooks;
  isOpen: ReturnType<typeof vi.fn>;
  sendPing: ReturnType<typeof vi.fn>;
  closeForReconnect: ReturnType<typeof vi.fn>;
  onLatency: ReturnType<typeof vi.fn>;
} {
  const isOpen = vi.fn(() => true);
  const sendPing = vi.fn();
  const closeForReconnect = vi.fn();
  const onLatency = vi.fn();
  const hooks: HeartbeatHooks = {
    isOpen,
    sendPing,
    closeForReconnect,
    onLatency,
    ...overrides,
  };
  return { hooks, isOpen, sendPing, closeForReconnect, onLatency };
}

describe("Heartbeat", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("sends_ping_every_intervalMs_after_start", () => {
    const { hooks, sendPing } = makeHooks();
    const hb = new Heartbeat(CONFIG, hooks);
    hb.start();
    expect(sendPing).not.toHaveBeenCalled(); // start 不立刻发，等 interval
    vi.advanceTimersByTime(30_000);
    expect(sendPing).toHaveBeenCalledTimes(1);
    // 推进期间要回 pong，否则下一次 _sendPing 因 pongTimer pending 会跳过
    hb.handlePong((sendPing.mock.calls[0]![0] as number));
    vi.advanceTimersByTime(30_000);
    expect(sendPing).toHaveBeenCalledTimes(2);
    hb.handlePong(sendPing.mock.calls[1]![0] as number);
    vi.advanceTimersByTime(30_000);
    expect(sendPing).toHaveBeenCalledTimes(3);
    hb.stop();
  });

  it("updates_latencyMs_on_handlePong_with_matching_ts", () => {
    const { hooks, onLatency } = makeHooks();
    const hb = new Heartbeat(CONFIG, hooks);
    hb.start();
    // 模拟 1234ms 后回 pong
    const sentAt = Date.now();
    vi.advanceTimersByTime(30_000); // 第一次 sendPing
    // 推进时钟，模拟 RTT
    vi.setSystemTime(new Date(sentAt + 30_000 + 1234));
    hb.handlePong(sentAt + 30_000);
    expect(hb.latencyMs).toBe(1234);
    expect(onLatency).toHaveBeenCalledWith(1234);
    hb.stop();
  });

  it("closes_for_reconnect_after_maxMissed_unanswered_pings", () => {
    const { hooks, sendPing, closeForReconnect } = makeHooks();
    const hb = new Heartbeat(CONFIG, hooks);
    hb.start();
    // 第 1 次 ping → 等 timeout → missed=1
    vi.advanceTimersByTime(30_000);
    expect(sendPing).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(10_000);
    // 此时 pongTimer 触发，missed=1，pongTimer 已清，下一个 interval 能再发
    // 第 2 次 ping
    vi.advanceTimersByTime(20_000); // 30s - 10s 已过
    expect(sendPing).toHaveBeenCalledTimes(2);
    vi.advanceTimersByTime(10_000); // pong timeout 2
    // 第 3 次 ping
    vi.advanceTimersByTime(20_000);
    expect(sendPing).toHaveBeenCalledTimes(3);
    vi.advanceTimersByTime(10_000); // pong timeout 3 → missed=3 ≥ maxMissed
    expect(closeForReconnect).toHaveBeenCalledTimes(1);
    hb.stop();
  });

  it("probe_sends_immediate_ping_outside_interval", () => {
    const { hooks, sendPing } = makeHooks();
    const hb = new Heartbeat(CONFIG, hooks);
    hb.start();
    // 才刚 start，距下次 interval 还有 30s
    vi.advanceTimersByTime(5_000);
    expect(sendPing).not.toHaveBeenCalled();
    hb.probe();
    expect(sendPing).toHaveBeenCalledTimes(1);
    hb.stop();
  });

  it("probe_is_noop_when_not_started", () => {
    const { hooks, sendPing } = makeHooks();
    const hb = new Heartbeat(CONFIG, hooks);
    // 没 start，直接 probe
    hb.probe();
    expect(sendPing).not.toHaveBeenCalled();
  });

  it("stop_halts_heartbeat_and_pong_timer", () => {
    const { hooks, sendPing, closeForReconnect } = makeHooks();
    const hb = new Heartbeat(CONFIG, hooks);
    hb.start();
    vi.advanceTimersByTime(30_000);
    expect(sendPing).toHaveBeenCalledTimes(1);
    hb.stop();
    // stop 之后无论怎么推时钟，sendPing / closeForReconnect 都不应再触发
    vi.advanceTimersByTime(120_000);
    expect(sendPing).toHaveBeenCalledTimes(1);
    expect(closeForReconnect).not.toHaveBeenCalled();
  });

  it("start_is_idempotent", () => {
    const { hooks, sendPing } = makeHooks();
    const hb = new Heartbeat(CONFIG, hooks);
    hb.start();
    hb.start(); // 再 start 应该是 no-op，不重复注册 timer
    hb.start();
    vi.advanceTimersByTime(30_000);
    // 只有一个 interval，所以一次 ping
    expect(sendPing).toHaveBeenCalledTimes(1);
    hb.stop();
  });

  it("two_instances_run_independently", () => {
    // 关键并发用例：杀一不影响另
    const a = makeHooks();
    const b = makeHooks();
    const hbA = new Heartbeat(CONFIG, a.hooks);
    const hbB = new Heartbeat(CONFIG, b.hooks);
    hbA.start();
    hbB.start();
    vi.advanceTimersByTime(30_000);
    expect(a.sendPing).toHaveBeenCalledTimes(1);
    expect(b.sendPing).toHaveBeenCalledTimes(1);

    // 把 A 打挂（连续 3 次超时 → closeForReconnect）
    vi.advanceTimersByTime(10_000); // A pong timeout 1 + B pong timeout 1
    vi.advanceTimersByTime(20_000); // A ping 2 + B ping 2
    expect(a.sendPing).toHaveBeenCalledTimes(2);
    expect(b.sendPing).toHaveBeenCalledTimes(2);
    vi.advanceTimersByTime(10_000); // timeout 2
    vi.advanceTimersByTime(20_000); // ping 3
    vi.advanceTimersByTime(10_000); // timeout 3 → closeForReconnect 双方都触发
    expect(a.closeForReconnect).toHaveBeenCalledTimes(1);
    expect(b.closeForReconnect).toHaveBeenCalledTimes(1);

    // 现在把 A 停掉，B 应该不受影响：handlePong 给 B 应正常更新 latency
    hbA.stop();
    // B 还在跑，给 B 灌一个 pong
    const sentAt = Date.now();
    b.sendPing.mockClear();
    // B 在下一个 interval 还能发 ping（manager 会调 stop+重连，但单测我们绕过 manager）
    // 这里换一种验证：直接 handlePong B，确认 latency 写入
    vi.setSystemTime(new Date(sentAt + 500));
    hbB.handlePong(sentAt);
    expect(hbB.latencyMs).toBe(500);
    // A 已 stop，latency 应保持 stop 前最后一次值（这里从未更新过，为 null）
    expect(hbA.latencyMs).toBe(null);
    hbB.stop();
  });

  it("does_not_crash_when_hooks_onLatency_throws", () => {
    const consoleErr = vi.spyOn(console, "error").mockImplementation(() => {});
    const { hooks } = makeHooks({
      onLatency: () => {
        throw new Error("listener boom");
      },
    });
    const hb = new Heartbeat(CONFIG, hooks);
    const sentAt = Date.now();
    // 不抛 = 通过
    expect(() => hb.handlePong(sentAt)).not.toThrow();
    expect(consoleErr).toHaveBeenCalled();
    hb.stop();
    consoleErr.mockRestore();
  });

  it("skips_ping_when_socket_not_open", () => {
    const { hooks, isOpen, sendPing } = makeHooks();
    isOpen.mockReturnValue(false);
    const hb = new Heartbeat(CONFIG, hooks);
    hb.start();
    vi.advanceTimersByTime(30_000);
    expect(sendPing).not.toHaveBeenCalled();
    hb.stop();
  });
});
