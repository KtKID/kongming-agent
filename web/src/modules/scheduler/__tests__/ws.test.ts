/**
 * cron WS 客户端测试（reports/cr/cr-report-20260519 P1 #2 修复配套）。
 *
 * 覆盖场景：
 * 1. 普通连接 / open / message 分派
 * 2. 1008 policy violation → 立即放弃不重连
 * 3. 1006 abnormal close → 指数退避重连
 * 4. 退避序列 1s → 2s → 4s → ... → 30s 封顶
 * 5. MAX_RETRY=10 后放弃
 * 6. open 成功 → retry 计数复位
 * 7. disconnectCronWS → 清 timer + 复位状态
 *
 * 用 fake WebSocket + vi.useFakeTimers 模拟时间推进，不真发网络。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  _resetForTesting,
  connectCronWS,
  disconnectCronWS,
} from "../ws";

const REAL_LOCATION = globalThis.location;

class FakeWebSocket {
  static OPEN = 1;
  static CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;

  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(): void {}

  close(): void {
    this.readyState = FakeWebSocket.CLOSED;
    if (this.onclose) this.onclose({ code: 1000 });
  }

  // 测试 helper
  triggerOpen(): void {
    this.readyState = FakeWebSocket.OPEN;
    if (this.onopen) this.onopen();
  }

  triggerClose(code: number): void {
    this.readyState = FakeWebSocket.CLOSED;
    if (this.onclose) this.onclose({ code });
  }

  triggerMessage(data: unknown): void {
    if (this.onmessage) {
      this.onmessage(
        new MessageEvent("message", {
          data: typeof data === "string" ? data : JSON.stringify(data),
        }),
      );
    }
  }
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  (globalThis as { WebSocket: unknown }).WebSocket = FakeWebSocket;
  (globalThis as { location: object }).location = {
    protocol: "http:",
    host: "localhost:1987",
  };
  vi.useFakeTimers();
  _resetForTesting();
});

afterEach(() => {
  (globalThis as { location: unknown }).location = REAL_LOCATION;
  vi.useRealTimers();
  _resetForTesting();
});

describe("cron ws · 普通路径", () => {
  it("connect → open 后能分派 message", () => {
    const onMsg = vi.fn();
    connectCronWS(onMsg);

    const sock = FakeWebSocket.instances[0]!;
    expect(sock.url).toContain("/ws/cron");
    sock.triggerOpen();
    sock.triggerMessage({ frame_type: "cron.run.completed", taskId: "x" });

    expect(onMsg).toHaveBeenCalledWith({
      frame_type: "cron.run.completed",
      taskId: "x",
    });
  });

  it("非 JSON 消息静默忽略", () => {
    const onMsg = vi.fn();
    connectCronWS(onMsg);
    const sock = FakeWebSocket.instances[0]!;
    sock.triggerOpen();
    sock.triggerMessage("not-json{");
    expect(onMsg).not.toHaveBeenCalled();
  });
});

describe("cron ws · 1008 policy violation 停连", () => {
  it("收到 1008 立即放弃，不再开新 socket", () => {
    const onMsg = vi.fn();
    connectCronWS(onMsg);
    const sock = FakeWebSocket.instances[0]!;
    sock.triggerClose(1008);

    // 推进很长时间，确认没有新连接
    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances.length).toBe(1);
  });

  it("givenUp=true 后再调 connectCronWS 也不开新 socket", () => {
    const onMsg = vi.fn();
    connectCronWS(onMsg);
    FakeWebSocket.instances[0]!.triggerClose(1008);

    connectCronWS(onMsg);
    expect(FakeWebSocket.instances.length).toBe(1);
  });
});

describe("cron ws · 指数退避", () => {
  it("非 1008 close → 1s 后重连", () => {
    const onMsg = vi.fn();
    connectCronWS(onMsg);
    FakeWebSocket.instances[0]!.triggerClose(1006);

    // 不到 1s 时还没重连
    vi.advanceTimersByTime(900);
    expect(FakeWebSocket.instances.length).toBe(1);

    // 1s 后重连
    vi.advanceTimersByTime(100);
    expect(FakeWebSocket.instances.length).toBe(2);
  });

  it("连续失败按 1s/2s/4s/8s 指数退避", () => {
    const onMsg = vi.fn();
    connectCronWS(onMsg);

    // 第 1 次失败 → 1s 后重连
    FakeWebSocket.instances[0]!.triggerClose(1006);
    vi.advanceTimersByTime(1000);
    expect(FakeWebSocket.instances.length).toBe(2);

    // 第 2 次失败 → 2s 后
    FakeWebSocket.instances[1]!.triggerClose(1006);
    vi.advanceTimersByTime(1999);
    expect(FakeWebSocket.instances.length).toBe(2);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances.length).toBe(3);

    // 第 3 次失败 → 4s 后
    FakeWebSocket.instances[2]!.triggerClose(1006);
    vi.advanceTimersByTime(4000);
    expect(FakeWebSocket.instances.length).toBe(4);
  });

  it("退避封顶 30s（第 6 次后都是 30s）", () => {
    const onMsg = vi.fn();
    connectCronWS(onMsg);
    // 第 1~6 次失败：1s, 2s, 4s, 8s, 16s, 30s（封顶，2^5=32 截到 30）
    const delays = [1000, 2000, 4000, 8000, 16000, 30000];
    for (let i = 0; i < delays.length; i++) {
      FakeWebSocket.instances[i]!.triggerClose(1006);
      vi.advanceTimersByTime(delays[i]!);
    }
    expect(FakeWebSocket.instances.length).toBe(delays.length + 1);

    // 第 7 次仍是 30s
    FakeWebSocket.instances[delays.length]!.triggerClose(1006);
    vi.advanceTimersByTime(30_000);
    expect(FakeWebSocket.instances.length).toBe(delays.length + 2);
  });

  it("open 成功后 retry 计数复位", () => {
    const onMsg = vi.fn();
    connectCronWS(onMsg);

    // 失败 2 次
    FakeWebSocket.instances[0]!.triggerClose(1006);
    vi.advanceTimersByTime(1000);
    FakeWebSocket.instances[1]!.triggerClose(1006);
    vi.advanceTimersByTime(2000);

    // 第 3 次连接 + open 成功
    FakeWebSocket.instances[2]!.triggerOpen();

    // 再失败 → 应当回到 1s（不是 4s）
    FakeWebSocket.instances[2]!.triggerClose(1006);
    vi.advanceTimersByTime(1000);
    expect(FakeWebSocket.instances.length).toBe(4);
  });
});

describe("cron ws · MAX_RETRY=10 上限", () => {
  it("第 11 次失败放弃，不再开新 socket", () => {
    const onMsg = vi.fn();
    connectCronWS(onMsg);

    // 触发 10 次失败 + 退避
    // 序列：1s, 2s, 4s, 8s, 16s, 30s, 30s, 30s, 30s, 30s
    const delays = [1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000, 30000, 30000];
    for (let i = 0; i < 10; i++) {
      FakeWebSocket.instances[i]!.triggerClose(1006);
      vi.advanceTimersByTime(delays[i]!);
    }
    // 已经开了 11 个 socket（第 11 次重连刚开）
    expect(FakeWebSocket.instances.length).toBe(11);

    // 第 11 次又失败 → retryCount 已经是 10 → 放弃
    FakeWebSocket.instances[10]!.triggerClose(1006);
    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances.length).toBe(11);
  });
});

describe("cron ws · disconnectCronWS", () => {
  it("清 timer + 关闭 ws + 复位 retryCount", () => {
    const onMsg = vi.fn();
    connectCronWS(onMsg);
    FakeWebSocket.instances[0]!.triggerClose(1006);

    // pending timer 中
    disconnectCronWS();

    // 推进时间不应触发新连接
    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances.length).toBe(1);
  });

  it("disconnect 后再 connect 重新计数", () => {
    const onMsg = vi.fn();
    connectCronWS(onMsg);
    FakeWebSocket.instances[0]!.triggerClose(1006);
    vi.advanceTimersByTime(1000);
    // 第 2 个 socket 已开
    expect(FakeWebSocket.instances.length).toBe(2);

    disconnectCronWS();
    connectCronWS(onMsg);

    // 立刻开第 3 个，第一次失败应该回到 1s 退避
    expect(FakeWebSocket.instances.length).toBe(3);
    FakeWebSocket.instances[2]!.triggerClose(1006);
    vi.advanceTimersByTime(1000);
    expect(FakeWebSocket.instances.length).toBe(4);
  });
});
