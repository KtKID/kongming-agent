import { toast } from "sonner";
import type { WSFrameC2S, WSFrameS2C } from "@/protocol";

// ---------------------------------------------------------------------------
// HeartbeatConfig
// ---------------------------------------------------------------------------

export interface HeartbeatConfig {
  intervalMs: number;     // ping 间隔
  timeoutMs: number;      // 单次 pong 超时
  maxMissed: number;      // 连续丢几次判死
}

const DEFAULT_HEARTBEAT: HeartbeatConfig = {
  intervalMs: 30_000,
  timeoutMs: 10_000,
  maxMissed: 3,
};

/**
 * kongming-agent v0.1.5 ThreadSocket
 *
 * 单 thread 的 WS 连接 + 自动重连 + 帧分发 + 心跳 RTT。
 *
 * ## 状态机
 *
 *   closed ──connect()──▶ connecting ──open──▶ open
 *      ▲                       │
 *      │                       │ close/error
 *      │                       ▼
 *      └──── close() ──── reconnecting (指数退避 1s..30s, 上限 10 次)
 *                              │
 *                              │ 10 次失败
 *                              ▼
 *                            failed (toast 提示用户刷新)
 *
 * ## 设计点
 *
 * - StrictMode 双 mount 安全：`connect()` 幂等（已 connecting/open 直接返回）
 * - close() 后清 retryTimer 防止 unmount 后还在重连
 * - 重连计数在 onopen 时清零
 * - 非法 JSON 帧静默丢弃（保留连接）；解析失败的责任不在 caller
 * - 心跳：定时发 ping + 等 pong；连续 maxMissed 次超时判定死连接并触发重连
 */

type Listener = (frame: WSFrameS2C) => void;
type StateListener = (state: SocketState) => void;

export type SocketState =
  | "closed"
  | "connecting"
  | "open"
  | "reconnecting"
  | "failed";

const MAX_RETRY = 10;
const MAX_BACKOFF_MS = 30_000;

export class ThreadSocket {
  private ws: WebSocket | null = null;
  private state: SocketState = "closed";
  private retryCount = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private listeners = new Set<Listener>();
  private stateListeners = new Set<StateListener>();
  /** 标识本实例是否已被 close()，防止 onclose 触发的 reconnect race */
  private disposed = false;

  // ---- heartbeat ----
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private pongTimer: ReturnType<typeof setTimeout> | null = null;
  private missedPongs = 0;
  private _latencyMs: number | null = null;
  private _heartbeatConfig: HeartbeatConfig;
  private pendingPingTs: number | null = null;

  /** 最近一次 RTT（毫秒），未收到 pong 时为 null */
  get latencyMs(): number | null {
    return this._latencyMs;
  }

  constructor(
    public threadId: string,
    heartbeatConfig?: Partial<HeartbeatConfig>,
  ) {
    this._heartbeatConfig = { ...DEFAULT_HEARTBEAT, ...heartbeatConfig };
  }

  getState(): SocketState {
    return this.state;
  }

  /** 注册帧 listener；返回反注册函数 */
  on(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  }

  onState(fn: StateListener): () => void {
    this.stateListeners.add(fn);
    return () => {
      this.stateListeners.delete(fn);
    };
  }

  send(frame: WSFrameC2S): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      throw new Error(
        `[ThreadSocket] cannot send while state=${this.state}`,
      );
    }
    this.ws.send(JSON.stringify(frame));
  }

  connect(): void {
    if (this.disposed) return;
    if (this.state === "connecting" || this.state === "open") return;

    this.setState(this.retryCount > 0 ? "reconnecting" : "connecting");

    const base =
      (import.meta.env.VITE_WS_BASE as string | undefined) ??
      `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}`;
    const url = `${base}/ws/threads/${encodeURIComponent(this.threadId)}`;

    let socket: WebSocket;
    try {
      socket = new WebSocket(url);
    } catch (err) {
      // 构造异常（极少见，URL 非法等）→ 进入重连流程
      console.error("[ThreadSocket] new WebSocket failed", err);
      this.scheduleReconnect();
      return;
    }

    this.ws = socket;

    socket.onopen = () => {
      if (this.disposed) {
        socket.close();
        return;
      }
      this.retryCount = 0;
      this.setState("open");
      this.startHeartbeat();
    };

    socket.onmessage = (ev) => {
      if (this.disposed) return;
      try {
        const frame = JSON.parse(ev.data as string) as WSFrameS2C;
        // pong 帧：更新 RTT，重置 missed 计数（仍然分发给 listeners）
        if (frame.frame_type === "pong" && typeof frame.ts === "number") {
          this.handlePong(frame.ts);
        }
        for (const l of this.listeners) {
          try {
            l(frame);
          } catch (err) {
            // listener 异常隔离，不影响其它 listener
            console.error("[ThreadSocket] listener error", err);
          }
        }
      } catch {
        // 非 JSON / 解析失败：静默丢弃；后端协议 union 是 source of truth
      }
    };

    socket.onclose = () => {
      this.ws = null;
      if (this.disposed) return;
      this.scheduleReconnect();
    };

    socket.onerror = () => {
      // onclose 紧随；让 onclose 处理状态切换，避免双触发
    };
  }

  /** 显式关闭：阻止后续 reconnect；listener 不清（caller 自管） */
  close(): void {
    this.disposed = true;
    this.stopHeartbeat();
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.setState("closed");
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        // ignore
      }
      this.ws = null;
    }
  }

  private scheduleReconnect(): void {
    this.stopHeartbeat();
    if (this.disposed) return;
    if (this.retryCount >= MAX_RETRY) {
      this.setState("failed");
      toast.error("连接失败 10 次，请刷新页面重试");
      return;
    }
    const delay = Math.min(1000 * 2 ** this.retryCount, MAX_BACKOFF_MS);
    this.retryCount += 1;
    this.setState("reconnecting");
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.connect();
    }, delay);
  }

  private setState(s: SocketState): void {
    if (this.state === s) return;
    this.state = s;
    for (const l of this.stateListeners) {
      try {
        l(s);
      } catch (err) {
        console.error("[ThreadSocket] state listener error", err);
      }
    }
  }

  // ---- heartbeat methods ----

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.missedPongs = 0;
    this._latencyMs = null;
    this.pendingPingTs = null;
    this.heartbeatTimer = setInterval(() => {
      this.sendPing();
    }, this._heartbeatConfig.intervalMs);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    if (this.pongTimer) {
      clearTimeout(this.pongTimer);
      this.pongTimer = null;
    }
    this.pendingPingTs = null;
  }

  private sendPing(): void {
    if (this.disposed) return;
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    const ts = Date.now();
    this.pendingPingTs = ts;
    this.ws.send(JSON.stringify({ frame_type: "ping", ts }));

    // 单次 pong 超时检测
    this.pongTimer = setTimeout(() => {
      this.pongTimer = null;
      this.missedPongs++;
      if (this.missedPongs >= this._heartbeatConfig.maxMissed) {
        console.warn(
          `[ThreadSocket] ${this._heartbeatConfig.maxMissed} pongs missed, reconnecting`,
        );
        if (this.ws) {
          this.ws.close();
        }
      }
    }, this._heartbeatConfig.timeoutMs);
  }

  private handlePong(ts: number): void {
    if (this.pongTimer) {
      clearTimeout(this.pongTimer);
      this.pongTimer = null;
    }
    this.missedPongs = 0;
    if (ts === this.pendingPingTs) {
      this._latencyMs = Date.now() - ts;
    }
    this.pendingPingTs = null;
  }

  /** 运行时更新心跳配置（从后端拉取后调用） */
  updateHeartbeatConfig(config: Partial<HeartbeatConfig>): void {
    this._heartbeatConfig = { ...this._heartbeatConfig, ...config };
    // 如果正在运行，重启心跳定时器
    if (this.state === "open") {
      this.stopHeartbeat();
      this.startHeartbeat();
    }
  }
}
