/**
 * kongming-agent web-network-layer v0.1 · NetworkManager 前端实现
 *
 * ## 这份文件是什么
 *
 * v0.1 网络层在浏览器侧的入口：socket 池 + per-connection Heartbeat +
 * visibilitychange 集中化 + 状态聚合到 `connectionStatusStore`。业务 hook
 * （`useClaudeCodeWS` 等）通过 `networkManager.openChannel(...)` 拿到
 * `ChannelHandle`，**不允许**直接 `new WebSocket()`。
 *
 * 设计文档：
 * - `docs/web-network-layer-v0.1/02-module-breakdown.md`（前端模块拆分）
 * - `docs/web-network-layer-v0.1/03-core-workflows.md`（心跳时序 + close code 分支）
 * - `docs/web-network-layer-v0.1/04-data-and-state.md`（ChannelHandle / 内部字段）
 *
 * ## v0.1 范围
 *
 * - 接入 Claude / generic 两类 per-thread channel
 * - close code 1000 → closed（不重连），1008 → failed + toast（不重连），
 *   其他（1006 / 1011 等） → reconnecting + 指数退避；10 次后转 failed
 */

import { toast } from "sonner";
import { useConnectionStatusStore } from "@/stores/connectionStatus";
import { Heartbeat, type HeartbeatConfig, type HeartbeatHooks } from "./heartbeat";
import {
  exponentialBackoff,
  isHeartbeatFrame,
  makeConnectionId,
} from "./tools";

// ---------------------------------------------------------------------------
// 公共类型
// ---------------------------------------------------------------------------

/**
 * 单条 socket 在 NetworkManager 内的生命周期阶段。
 */
export type SocketState =
  | "closed"
  | "connecting"
  | "open"
  | "reconnecting"
  | "failed";

/**
 * 频道种类标识。
 */
export type ChannelKind = "claude" | "generic" | "cron-run";

/**
 * `openChannel` 返回的业务侧句柄。
 *
 * 业务 hook 拿到 `ChannelHandle` 后只能 send / close / 订阅消息与状态，
 * **不能**接触底层 WebSocket 对象。
 */
export interface ChannelHandle {
  /** 该 channel 的 connection id（用于诊断 / 调 closeChannel）。 */
  readonly connId: string;
  /** 发送一帧业务数据（具体 frame 形态由 channel 决定，network 层不解释） */
  send(frame: unknown): void;
  /** 主动关闭此 channel（NetworkManager 会清理对应 Heartbeat / retry timer） */
  close(): void;
  /** 订阅入站消息；返回 unsubscribe 句柄 */
  onMessage(cb: (frame: unknown) => void): () => void;
  /** 订阅 socket 状态切换；返回 unsubscribe 句柄。首次订阅立即回调一次当前状态 */
  onState(cb: (state: SocketState) => void): () => void;
}

// ---------------------------------------------------------------------------
// 内部类型
// ---------------------------------------------------------------------------

type MessageListener = (frame: unknown) => void;
type StateListener = (state: SocketState) => void;

interface ConnectionRecord {
  connId: string;
  kind: ChannelKind;
  threadId: string;
  socket: WebSocket | null;
  heartbeat: Heartbeat | null;
  state: SocketState;
  /** 业务侧主动 close 标记；置位后 onclose 不再重连 */
  disposed: boolean;
  messageListeners: Set<MessageListener>;
  stateListeners: Set<StateListener>;
  retryCount: number;
  retryTimer: ReturnType<typeof setTimeout> | null;
}

const MAX_RETRY = 10;

/**
 * 把 ChannelKind 映射到后端 WS URL path：
 * - "claude" → "/ws/claude-code?thread_id=..."
 * - "generic" → "/ws/threads/{threadId}"
 * - "cron-run" → "/ws/cron/tasks/{taskId}/runs/{runId}"，threadId 形如
 *   "{taskId}:{runId}"
 */
function pathForKind(kind: ChannelKind, threadId: string): string {
  switch (kind) {
    case "claude":
      return `/ws/claude-code?thread_id=${encodeURIComponent(threadId)}`;
    case "generic":
      return `/ws/threads/${encodeURIComponent(threadId)}`;
    case "cron-run": {
      const separator = threadId.indexOf(":");
      const taskId = separator >= 0 ? threadId.slice(0, separator) : threadId;
      const runId = separator >= 0 ? threadId.slice(separator + 1) : "";
      return `/ws/cron/tasks/${encodeURIComponent(taskId)}/runs/${encodeURIComponent(runId)}`;
    }
  }
}

function isPositiveFiniteNumber(value: number): boolean {
  return Number.isFinite(value) && value > 0;
}

function isValidHeartbeatConfig(config: HeartbeatConfig): boolean {
  return (
    isPositiveFiniteNumber(config.intervalMs) &&
    isPositiveFiniteNumber(config.backgroundIntervalMs) &&
    isPositiveFiniteNumber(config.timeoutMs) &&
    Number.isFinite(config.maxMissed) &&
    config.maxMissed >= 1
  );
}

// ---------------------------------------------------------------------------
// NetworkManager
// ---------------------------------------------------------------------------

/**
 * 浏览器侧网络层单例。所有 channel 共用同一 visibilitychange 监听 + 重连退避
 * 策略；每条 socket 配一个 `Heartbeat` 实例。
 */
export class NetworkManager {
  /** connection_id → 整条记录（含 socket / heartbeat / listeners / retry 状态） */
  private readonly _records = new Map<string, ConnectionRecord>();

  /** 心跳配置；configure 前调 openChannel 会抛错 */
  private _config: HeartbeatConfig | null = null;

  /** visibilitychange 监听是否已挂（避免热模块替换时重复挂） */
  private _visibilityWired = false;

  constructor() {
    this._wireVisibilityChange();
  }

  /**
   * 注入心跳配置。NetworkManager 首次使用前必须调一次（由各频道装配点
   * 在 `useHeartbeatConfig` 拿到后注入）。
   *
   * 幂等：重复调用使用最新配置；不影响已经在跑的 Heartbeat（它们持有自己的
   * config 引用，由当时的快照决定行为）。
   */
  configure(config: HeartbeatConfig): void {
    if (!isValidHeartbeatConfig(config)) {
      throw new Error("[NetworkManager] invalid heartbeat config");
    }
    this._config = { ...config };
  }

  /**
   * 打开（或复用）一条 channel 连接，返回业务句柄。
   *
   * - 同 (kind, threadId) 已连接 → 共享底层 socket / heartbeat；每个句柄
   *   通过 unsubscribe 精准移除自身 listener（防 unmount 串扰）。
   *
   * @param kind     频道种类（当前接入 "claude" / "generic"）
   * @param threadId thread 标识
   */
  openChannel(kind: ChannelKind, threadId: string): ChannelHandle {
    if (this._config === null) {
      throw new Error(
        "[NetworkManager] configure() must be called before openChannel()",
      );
    }
    if (!threadId) {
      throw new Error("[NetworkManager] threadId is required");
    }

    const channelKey = `${kind}:${threadId}`;
    // 同 (kind, threadId) 复用记录；不同 mount/unmount 通过句柄独立 listener 隔离
    let record = this._findRecordByChannelKey(channelKey);
    if (record === null || record.disposed) {
      record = this._createRecord(kind, threadId);
      this._records.set(record.connId, record);
      this._setChannelActive(record, true);
      this._connect(record);
    }
    return this._makeHandle(record);
  }

  /**
   * 主动关闭一条 channel 连接。
   *
   * 会停止其 Heartbeat、清理 retry timer、关闭底层 socket、广播 `closed` 状态。
   */
  closeChannel(connId: string): void {
    const record = this._records.get(connId);
    if (!record) return;
    record.disposed = true;
    this._clearRetryTimer(record);
    if (record.heartbeat) {
      record.heartbeat.stop();
      record.heartbeat = null;
    }
    if (record.socket) {
      try {
        record.socket.close(1000, "client-close");
      } catch {
        // ignore
      }
      record.socket = null;
    }
    this._setState(record, "closed");
    this._setChannelActive(record, false);
    this._clearChannelLatency(record);
    this._records.delete(connId);
  }

  /**
   * 调试 / 测试用：当前活跃连接数。
   */
  get connectionCount(): number {
    return this._records.size;
  }

  // -------------------------------------------------------------------------
  // 内部实现
  // -------------------------------------------------------------------------

  private _findRecordByChannelKey(channelKey: string): ConnectionRecord | null {
    for (const record of this._records.values()) {
      if (`${record.kind}:${record.threadId}` === channelKey) return record;
    }
    return null;
  }

  private _createRecord(
    kind: ChannelKind,
    threadId: string,
  ): ConnectionRecord {
    const connId = `${kind}:${threadId}:${makeConnectionId()}`;
    return {
      connId,
      kind,
      threadId,
      socket: null,
      heartbeat: null,
      state: "closed",
      disposed: false,
      messageListeners: new Set(),
      stateListeners: new Set(),
      retryCount: 0,
      retryTimer: null,
    };
  }

  private _connect(record: ConnectionRecord): void {
    if (record.disposed) return;
    if (this._config === null) return;

    const initialState: SocketState =
      record.retryCount > 0 ? "reconnecting" : "connecting";
    this._setState(record, initialState);

    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const base =
      (import.meta.env.VITE_WS_BASE as string | undefined) ??
      `${proto}://${window.location.host}`;
    const url = `${base}${pathForKind(record.kind, record.threadId)}`;

    let socket: WebSocket;
    try {
      socket = new WebSocket(url);
    } catch (err) {
      console.error("[NetworkManager] new WebSocket failed", err);
      this._scheduleReconnect(record);
      return;
    }
    record.socket = socket;

    // 构造 hooks 闭包：捕获 record 本身，不持 socket 引用——重连后 socket 会换
    // 但 record 不变，hooks 通过 record.socket 取当前值
    const hooks: HeartbeatHooks = {
      isOpen: () =>
        record.socket !== null &&
        record.socket.readyState === WebSocket.OPEN,
      sendPing: (ts) => {
        if (record.socket && record.socket.readyState === WebSocket.OPEN) {
          record.socket.send(JSON.stringify({ frame_type: "ping", ts }));
        }
      },
      closeForReconnect: () => {
        if (record.socket) {
          try {
            record.socket.close(4000, "heartbeat-missed");
          } catch {
            // ignore
          }
        }
      },
      onLatency: (ms) => {
        // 单一真源：lifecycle 写 state，heartbeat 写 latency。
        if (record.kind === "claude") {
          useConnectionStatusStore.getState().setClaudeWsLatency(ms);
        } else if (record.kind === "generic") {
          useConnectionStatusStore.getState().setThreadWsLatency(ms);
        }
      },
    };
    record.heartbeat = new Heartbeat(this._config, hooks);

    socket.onopen = () => {
      if (record.disposed) {
        try {
          socket.close();
        } catch {
          // ignore
        }
        return;
      }
      record.retryCount = 0;
      this._setState(record, "open");
      record.heartbeat?.start();
      // 立即发首次 ping，避免用户刷新后等 intervalMs（30s）才看到 latency 数字。
      // 跟 visibilitychange 切回前台 probe 是同一语义（"刚连上 = 刚切回前台"）。
      record.heartbeat?.probe();
    };

    socket.onmessage = (ev) => {
      if (record.disposed) return;
      let raw: unknown;
      try {
        raw = JSON.parse(String(ev.data));
      } catch {
        // 非 JSON：静默丢弃，符合 ClaudeCodeSocket 老行为
        return;
      }
      // 心跳帧优先拦截：pong 路由到 Heartbeat，ping 不应该出现在客户端入站
      // （v0.1 心跳是客户端 ping → 服务端 pong 单向；防御性处理仅识别 pong）
      if (isHeartbeatFrame(raw) && raw.frame_type === "pong") {
        if (record.heartbeat && typeof raw.ts === "number") {
          record.heartbeat.handlePong(raw.ts);
        }
        return;
      }
      // 业务帧分发
      for (const listener of record.messageListeners) {
        try {
          listener(raw);
        } catch (err) {
          console.error("[NetworkManager] message listener threw", err);
        }
      }
    };

    socket.onclose = (ev) => {
      // 不论 disposed 与否都先收尾 heartbeat / socket 引用
      if (record.heartbeat) {
        record.heartbeat.stop();
        record.heartbeat = null;
      }
      record.socket = null;
      if (record.disposed) return;
      this._handleSocketClose(record, ev.code);
    };

    socket.onerror = () => {
      // WebSocket onerror 紧随 onclose；无需在这里处理
    };
  }

  private _handleSocketClose(record: ConnectionRecord, code: number): void {
    if (code === 1000) {
      // 正常关闭：不重连
      this._setState(record, "closed");
      this._retireRecord(record);
      return;
    }
    if (code === 1008) {
      // policy violation（鉴权失败 / cookie 过期）→ failed + toast，不重连
      this._setState(record, "failed");
      try {
        toast.error("登录已过期，请重新登录");
      } catch {
          // 测试环境 sonner 可能未初始化
      }
      this._retireRecord(record);
      return;
    }
    // 其他（1006 / 1011 / 4000=heartbeat-missed 等）：指数退避重连
    this._scheduleReconnect(record);
  }

  private _scheduleReconnect(record: ConnectionRecord): void {
    if (record.disposed) return;
    if (record.retryCount >= MAX_RETRY) {
      this._setState(record, "failed");
      try {
        toast.error(`${record.kind} 连接失败 10 次，请刷新页面重试`);
      } catch {
        // 测试环境 sonner 可能未初始化
      }
      this._retireRecord(record);
      return;
    }
    const delay = exponentialBackoff(record.retryCount);
    record.retryCount += 1;
    this._setState(record, "reconnecting");
    this._clearRetryTimer(record);
    record.retryTimer = setTimeout(() => {
      record.retryTimer = null;
      this._connect(record);
    }, delay);
  }

  private _clearRetryTimer(record: ConnectionRecord): void {
    if (record.retryTimer !== null) {
      clearTimeout(record.retryTimer);
      record.retryTimer = null;
    }
  }

  private _retireRecord(record: ConnectionRecord): void {
    record.disposed = true;
    this._clearRetryTimer(record);
    if (record.heartbeat) {
      record.heartbeat.stop();
      record.heartbeat = null;
    }
    record.socket = null;
    this._setChannelActive(record, false);
    this._clearChannelLatency(record);
    this._records.delete(record.connId);
  }

  private _setState(record: ConnectionRecord, next: SocketState): void {
    if (record.state === next) return;
    record.state = next;
    if (record.kind === "claude") {
      useConnectionStatusStore.getState().setClaudeWsState(next);
      if (next === "closed" || next === "failed") {
        this._clearChannelLatency(record);
      }
    } else if (record.kind === "generic") {
      useConnectionStatusStore.getState().setThreadWsState(next);
      if (next === "closed" || next === "failed") {
        this._clearChannelLatency(record);
      }
    }
    for (const listener of record.stateListeners) {
      try {
        listener(next);
      } catch (err) {
        console.error("[NetworkManager] state listener threw", err);
      }
    }
  }

  private _setChannelActive(record: ConnectionRecord, active: boolean): void {
    if (record.kind === "generic") {
      useConnectionStatusStore.getState().setThreadWsActive(active);
    }
  }

  private _clearChannelLatency(record: ConnectionRecord): void {
    if (record.kind === "claude") {
      useConnectionStatusStore.getState().setClaudeWsLatency(null);
    } else if (record.kind === "generic") {
      useConnectionStatusStore.getState().setThreadWsLatency(null);
    }
  }

  private _makeHandle(record: ConnectionRecord): ChannelHandle {
    const messageListeners = record.messageListeners;
    const stateListeners = record.stateListeners;
    const closeChannel = (connId: string) => this.closeChannel(connId);
    return {
      connId: record.connId,
      send: (frame: unknown): void => {
        if (
          !record.socket ||
          record.socket.readyState !== WebSocket.OPEN
        ) {
          throw new Error(
            `[NetworkManager] cannot send while state=${record.state}`,
          );
        }
        record.socket.send(JSON.stringify(frame));
      },
      close: (): void => {
        closeChannel(record.connId);
      },
      onMessage: (cb): (() => void) => {
        messageListeners.add(cb);
        return () => {
          messageListeners.delete(cb);
        };
      },
      onState: (cb): (() => void) => {
        stateListeners.add(cb);
        // 首次订阅立即同步一次当前状态，避免业务侧错过 onopen
        try {
          cb(record.state);
        } catch (err) {
          console.error("[NetworkManager] state listener threw", err);
        }
        return () => {
          stateListeners.delete(cb);
        };
      },
    };
  }

  private _wireVisibilityChange(): void {
    if (this._visibilityWired) return;
    if (typeof document === "undefined") return;
    this._visibilityWired = true;
    document.addEventListener("visibilitychange", () => {
      const hidden = document.hidden;
      // 切到后台：所有 heartbeat 切到 backgroundIntervalMs（默认 60s）；
      // 切回前台：切回 intervalMs（默认 30s） + 立即 probe 探测连接还活着
      // + 清掉可能在后台积压的 stale latency（防止 UI 显示节流期巨大 RTT）。
      for (const record of this._records.values()) {
        if (!record.heartbeat) continue;
        record.heartbeat.setBackgrounded(hidden);
        if (!hidden) {
          record.heartbeat.probe();
        }
      }
      if (!hidden) {
        // 切回前台时先清 latency，等下一个 pong 回来再灌真实 RTT
        useConnectionStatusStore.getState().setClaudeWsLatency(null);
        useConnectionStatusStore.getState().setThreadWsLatency(null);
      }
    });
  }
}

// ---------------------------------------------------------------------------
// 模块单例
// ---------------------------------------------------------------------------

/**
 * 浏览器侧唯一 NetworkManager 实例。业务侧统一通过此单例 `openChannel`，
 * 不允许 `new NetworkManager()`。
 */
export const networkManager = new NetworkManager();
