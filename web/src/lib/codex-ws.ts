import { toast } from "sonner";
import type { CodexC2SFrame, CodexS2CFrame } from "@/protocol";

type Listener = (frame: CodexS2CFrame) => void;
type StateListener = (state: SocketState) => void;

export type SocketState =
  | "closed"
  | "connecting"
  | "open"
  | "reconnecting"
  | "failed";

const MAX_RETRY = 10;
const MAX_BACKOFF_MS = 30_000;

/**
 * `/ws/codex?thread_id=...` 的客户端。
 *
 * 与 `ClaudeCodeSocket` 状态机 / 重连策略一致，只是 URL 不同 + 帧类型不同。
 * 复制而非继承：两路径协议完全独立，未来可能会演化分歧；此处保留独立实现避免
 * 过早抽象。
 */
export class CodexSocket {
  private ws: WebSocket | null = null;
  private state: SocketState = "closed";
  private retryCount = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private listeners = new Set<Listener>();
  private stateListeners = new Set<StateListener>();
  private disposed = false;

  constructor(public threadId: string) {}

  getState(): SocketState {
    return this.state;
  }

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

  send(frame: CodexC2SFrame): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      throw new Error(
        `[CodexSocket] cannot send while state=${this.state}`,
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
    const url = `${base}/ws/codex?thread_id=${encodeURIComponent(this.threadId)}`;

    let socket: WebSocket;
    try {
      socket = new WebSocket(url);
    } catch (err) {
      console.error("[CodexSocket] new WebSocket failed", err);
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
    };

    socket.onmessage = (ev) => {
      if (this.disposed) return;
      try {
        const frame = JSON.parse(ev.data as string) as CodexS2CFrame;
        for (const l of this.listeners) {
          try {
            l(frame);
          } catch (err) {
            console.error("[CodexSocket] listener error", err);
          }
        }
      } catch {
        // 非 JSON / 解析失败：静默丢弃
      }
    };

    socket.onclose = () => {
      this.ws = null;
      if (this.disposed) return;
      this.scheduleReconnect();
    };

    socket.onerror = () => {
      // onclose 紧随
    };
  }

  close(): void {
    this.disposed = true;
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
    if (this.disposed) return;
    if (this.retryCount >= MAX_RETRY) {
      this.setState("failed");
      toast.error("Codex 连接失败 10 次，请刷新页面重试");
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
        console.error("[CodexSocket] state listener error", err);
      }
    }
  }
}
