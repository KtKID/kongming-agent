import { toast } from "sonner";
import type {
  WorkspaceShellC2SFrame,
  WorkspaceShellS2CFrame,
} from "@/protocol";

type Listener = (frame: WorkspaceShellS2CFrame) => void;
type StateListener = (state: SocketState) => void;

export type SocketState =
  | "closed"
  | "connecting"
  | "open"
  | "reconnecting"
  | "failed";

const MAX_RETRY = 8;
const MAX_BACKOFF_MS = 15_000;

export class WorkspaceShellSocket {
  private ws: WebSocket | null = null;
  private state: SocketState = "closed";
  private retryCount = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private listeners = new Set<Listener>();
  private stateListeners = new Set<StateListener>();
  private disposed = false;

  constructor(public threadId: string) {}

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

  send(frame: WorkspaceShellC2SFrame): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      throw new Error(`[WorkspaceShellSocket] cannot send while state=${this.state}`);
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
    const url = `${base}/ws/workspace-shell?thread_id=${encodeURIComponent(this.threadId)}`;
    let socket: WebSocket;
    try {
      socket = new WebSocket(url);
    } catch (err) {
      console.error("[WorkspaceShellSocket] new WebSocket failed", err);
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
        const frame = JSON.parse(ev.data as string) as WorkspaceShellS2CFrame;
        for (const listener of this.listeners) {
          listener(frame);
        }
      } catch {
        // ignore invalid frames
      }
    };
    socket.onclose = () => {
      this.ws = null;
      if (this.disposed) return;
      this.scheduleReconnect();
    };
    socket.onerror = () => {
      // onclose follows
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
      toast.error("Workspace shell 重连失败，请刷新页面重试");
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

  private setState(state: SocketState): void {
    if (this.state === state) return;
    this.state = state;
    for (const listener of this.stateListeners) {
      listener(state);
    }
  }
}
