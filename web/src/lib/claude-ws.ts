import { toast } from "sonner";
import type { NormalizedMessage } from "@/protocol";

/**
 * Claude Code endpoint 客户端帧（v0.1.6）。
 *
 * 这是 `/ws/claude-code` 接受的 4 类入站帧，对应 `src/web/claude_code/route.py::_dispatch`。
 *
 * 跟 generic_chat 的 WSFrameC2S **完全独立** —— 字段命名走 SDK / ccui 风格
 * （camelCase + `type` 判别字段），不复用 generic_chat 的 `kind` 模式。
 */
export type ClaudeCodeC2SFrame =
  | {
      type: "claude-command";
      command: string;
      options?: Record<string, unknown>;
    }
  | {
      type: "claude-permission-response";
      requestId: string;
      allow: boolean;
      message?: string;
      rememberEntry?: string;
    }
  | { type: "abort-session"; sessionId: string }
  | { type: "check-session-status"; sessionId: string };

/**
 * 后端到前端的两类帧：NormalizedMessage（主流）+ session-status（特殊）。
 *
 * `session-status` 是 `check-session-status` 的应答，字段集与 NormalizedMessage
 * 完全不同（用 `type` 而非 `kind`），单独建模。
 */
export interface SessionStatusFrame {
  type: "session-status";
  sessionId: string;
  isProcessing: boolean;
}

export type ClaudeCodeS2CFrame = NormalizedMessage | SessionStatusFrame;

type Listener = (frame: ClaudeCodeS2CFrame) => void;
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
 * `/ws/claude-code?thread_id=...` 的客户端。
 *
 * 与 `ThreadSocket` 状态机 / 重连策略一致，只是 URL 不同 + 帧类型不同。
 * 复制而非继承：两路径协议完全独立，未来可能会演化分歧；此处保留独立实现避免
 * 过早抽象。
 */
export class ClaudeCodeSocket {
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

  send(frame: ClaudeCodeC2SFrame): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      throw new Error(
        `[ClaudeCodeSocket] cannot send while state=${this.state}`,
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
    const url = `${base}/ws/claude-code?thread_id=${encodeURIComponent(this.threadId)}`;

    let socket: WebSocket;
    try {
      socket = new WebSocket(url);
    } catch (err) {
      console.error("[ClaudeCodeSocket] new WebSocket failed", err);
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
        const frame = JSON.parse(ev.data as string) as ClaudeCodeS2CFrame;
        for (const l of this.listeners) {
          try {
            l(frame);
          } catch (err) {
            console.error("[ClaudeCodeSocket] listener error", err);
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
      toast.error("Claude Code 连接失败 10 次，请刷新页面重试");
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
        console.error("[ClaudeCodeSocket] state listener error", err);
      }
    }
  }
}
