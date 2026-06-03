import { toast } from "sonner";
import type { NormalizedMessage, UserInputAttachment } from "@/protocol";

/**
 * Codex endpoint 客户端帧。
 *
 * 这是 `/ws/codex` 接受的 3 类入站帧，对应后端 codex route 的 dispatch。
 *
 * 与 claude-code 的差异：
 * - command type 为 `"codex-command"`
 * - 无 `"claude-permission-response"` 帧（codex 权限由 permissionMode 一次性设定）
 * - options 增加 permissionMode 字段
 */
export type CodexC2SFrame =
  | {
      frame_type: "codex-command";
      command: string;
      options?: {
        cwd?: string;
        sessionId?: string;
        resume?: boolean;
        model?: string;
        permissionMode?: "default" | "acceptEdits" | "bypassPermissions";
        // message-runtime #8: 三频道 reasoning 字段贯通 — Composer 选择的思考等级
        // 透传到 wire 帧。codex 后端目前不消费此字段（独立后端 task），但前端
        // 契约层先打通，避免 Composer→视图→ChatManager→provider 链路断点。
        reasoningEffort?: "low" | "medium" | "high";
      };
      /**
       * 图片附件（codex-channel-image-paste）。
       *
       * 后端 ``_handle_codex_command`` 解析后传给
       * ``CodexService.query(attachments=...)``，由 ``CodexImageCliArgsBuilder``
       * 拼成 ``--image <path>`` CLI flag 注入 codex exec 子进程；Rust 端转 base64
       * 后注入 OpenAI Responses API。缺省 / 空数组 → 走纯文本 prompt，向后兼容。
       *
       * **范围限定**：仅本轮发送 + 本轮 optimistic 缩略图。**不含**刷新后历史回显
       * （Codex jsonl 存 base64 而非 asset_id，独立后续 task 处理）。
       */
      attachments?: UserInputAttachment[];
    }
  | { frame_type: "abort-session"; sessionId: string }
  | { frame_type: "check-session-status"; sessionId: string };

/**
 * 后端到前端的两类帧：NormalizedMessage（主流）+ session-status（特殊）。
 *
 * `session-status` 是 `check-session-status` 的应答，字段集与 NormalizedMessage
 * 完全不同（用 `type` 而非 `kind`），单独建模。
 */
export interface SessionStatusFrame {
  frame_type: "session-status";
  sessionId: string;
  isProcessing: boolean;
}

export type CodexS2CFrame = NormalizedMessage | SessionStatusFrame;

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
