import { useCallback, useEffect, useRef, useState } from "react";
import { useThreadStatusStore } from "@/stores/threadStatus";
import { useConnectionStatusStore } from "@/stores/connectionStatus";
import { useChatStore } from "@/stores/chat";
import { useApprovalInboxStore } from "@/features/approval-inbox";
import { setSender as setInboxSender } from "@/features/approval-inbox/senderRef";
import type { ThreadStatusFrame, ThreadUsage } from "@/protocol";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type StatusWsState =
  | "closed"
  | "connecting"
  | "open"
  | "reconnecting"
  | "failed";

export interface HeartbeatConfig {
  /** ping 间隔（ms），默认 30 000 */
  intervalMs?: number;
  /**
   * 浏览器 tab 切到后台时 ping 间隔（ms），默认 60 000。
   * 仅 network-layer-v0.1 的 Claude 频道消费；
   * useThreadStatusWS 现在不消费（generic 通道沿用 intervalMs 单值）。
   */
  backgroundIntervalMs?: number;
  /** pong 超时（ms），默认 10 000 */
  timeoutMs?: number;
  /** 连续 pong 缺失上限，超过则主动断开触发重连，默认 3 */
  maxMissed?: number;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const MAX_RETRY = 10;
const MAX_BACKOFF_MS = 30_000;

const DEFAULT_HEARTBEAT: Required<HeartbeatConfig> = {
  intervalMs: 30_000,
  backgroundIntervalMs: 60_000,
  timeoutMs: 10_000,
  maxMissed: 3,
};

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

/**
 * 全局 thread-status 广播 WS 连接。
 *
 * 能力：
 * - 自动重连（指数退避 1 s → 30 s，最多 10 次）
 * - 心跳 ping/pong + RTT 计算
 * - 对外暴露 state / latencyMs
 *
 * StrictMode 安全：
 * - useEffect cleanup 设 disposedRef=true 并关闭 WS / 清定时器
 * - 第二次 mount 新建连接，旧的已 disposed 不会有 race
 */
export function useThreadStatusWS(heartbeatConfig?: HeartbeatConfig): {
  state: StatusWsState;
  latencyMs: number | null;
} {
  const setStatus = useThreadStatusStore((s) => s.setStatus);
  const setStatusWs = useConnectionStatusStore((s) => s.setStatusWs);

  const [state, setState] = useState<StatusWsState>("closed");
  const [latencyMs, setLatencyMs] = useState<number | null>(null);

  // --- refs（跨 render 保持的可变状态） ---
  const wsRef = useRef<WebSocket | null>(null);
  const disposedRef = useRef(false);
  const retryCountRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 心跳相关
  const heartbeatTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pongTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const missedPongsRef = useRef(0);
  const pendingPingTsRef = useRef<number | null>(null);

  // 心跳配置 ref（让 connect 内部始终拿到最新值）
  const hbCfgRef = useRef<Required<HeartbeatConfig>>(DEFAULT_HEARTBEAT);
  hbCfgRef.current = {
    intervalMs: heartbeatConfig?.intervalMs ?? DEFAULT_HEARTBEAT.intervalMs,
    backgroundIntervalMs:
      heartbeatConfig?.backgroundIntervalMs
      ?? DEFAULT_HEARTBEAT.backgroundIntervalMs,
    timeoutMs: heartbeatConfig?.timeoutMs ?? DEFAULT_HEARTBEAT.timeoutMs,
    maxMissed: heartbeatConfig?.maxMissed ?? DEFAULT_HEARTBEAT.maxMissed,
  };

  // --- 工具函数 ---

  const clearHeartbeat = useCallback(() => {
    if (heartbeatTimerRef.current) {
      clearInterval(heartbeatTimerRef.current);
      heartbeatTimerRef.current = null;
    }
    if (pongTimeoutRef.current) {
      clearTimeout(pongTimeoutRef.current);
      pongTimeoutRef.current = null;
    }
    missedPongsRef.current = 0;
    pendingPingTsRef.current = null;
  }, []);

  const clearRetryTimer = useCallback(() => {
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
  }, []);

  // --- 同步到全局 connection status store ---
  useEffect(() => {
    setStatusWs(state, latencyMs);
  }, [state, latencyMs, setStatusWs]);

  // --- 核心连接逻辑（useEffect 内定义，避免 stale closure） ---

  useEffect(() => {
    disposedRef.current = false;
    retryCountRef.current = 0;

    // ---- 本地辅助 ----

    function sendPing(ws: WebSocket) {
      if (ws.readyState !== WebSocket.OPEN) return;
      const ts = Date.now();
      pendingPingTsRef.current = ts;
      ws.send(JSON.stringify({ frame_type: "ping", ts }));

      // pong 超时检测
      pongTimeoutRef.current = setTimeout(() => {
        pongTimeoutRef.current = null;
        missedPongsRef.current += 1;
        if (missedPongsRef.current >= hbCfgRef.current.maxMissed) {
          // 超过容忍上限 → 主动关闭，onclose 触发重连
          console.warn(
            `[useThreadStatusWS] ${missedPongsRef.current} pongs missed, closing`,
          );
          ws.close();
        }
      }, hbCfgRef.current.timeoutMs);
    }

    function startHeartbeat(ws: WebSocket) {
      clearHeartbeat();
      missedPongsRef.current = 0;
      heartbeatTimerRef.current = setInterval(
        () => sendPing(ws),
        hbCfgRef.current.intervalMs,
      );
    }

    function handlePong(ts: number) {
      // 清超时计时器
      if (pongTimeoutRef.current) {
        clearTimeout(pongTimeoutRef.current);
        pongTimeoutRef.current = null;
      }
      missedPongsRef.current = 0;
      if (typeof ts === "number" && ts > 0) {
        setLatencyMs(Date.now() - ts);
      }
    }

    function scheduleReconnect() {
      if (disposedRef.current) return;
      if (retryCountRef.current >= MAX_RETRY) {
        setState("failed");
        return;
      }
      const delay = Math.min(
        1000 * 2 ** retryCountRef.current,
        MAX_BACKOFF_MS,
      );
      retryCountRef.current += 1;
      setState("reconnecting");
      retryTimerRef.current = setTimeout(() => {
        retryTimerRef.current = null;
        connect();
      }, delay);
    }

    function connect() {
      if (disposedRef.current) return;

      setState(retryCountRef.current > 0 ? "reconnecting" : "connecting");

      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${proto}//${window.location.host}/ws/thread-status`;

      let ws: WebSocket;
      try {
        ws = new WebSocket(url);
      } catch (err) {
        console.error("[useThreadStatusWS] new WebSocket failed", err);
        scheduleReconnect();
        return;
      }

      wsRef.current = ws;

      ws.onopen = () => {
        if (disposedRef.current) {
          ws.close();
          return;
        }
        retryCountRef.current = 0;
        setState("open");
        startHeartbeat(ws);
        // smart-approval-v2-inbox: 注入 send 通道到 module-level senderRef（**不进 store**，
        // 避免高频 mutate 触发 React 19 useSyncExternalStore tearing → #185 + ws 风暴）
        setInboxSender((frame) => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify(frame));
          } else {
            console.warn(
              "[useThreadStatusWS] inbox.resolve dropped — ws not open",
            );
          }
        });
      };

      ws.onmessage = (ev) => {
        if (disposedRef.current) return;
        try {
          const data = JSON.parse(ev.data as string);

          // pong 帧：计算 RTT
          if (data.frame_type === "pong" && typeof data.ts === "number") {
            handlePong(data.ts);
            return;
          }

          // approval.inbox.* 帧分派（smart-approval-v2-inbox）
          if (data.frame_type === "approval.inbox.add") {
            useApprovalInboxStore.getState().applyAddFrame(data);
            return;
          }
          if (data.frame_type === "approval.inbox.remove") {
            useApprovalInboxStore.getState().applyRemoveFrame(data);
            return;
          }
          if (data.frame_type === "approval.inbox.snapshot") {
            useApprovalInboxStore.getState().applySnapshotFrame(data);
            return;
          }

          // usage_summary_updated 帧（v2）：service 调 manager.get_thread_usage 后推送，
          // 含 v2 channel-specific DTO（自带 provider discriminator）。
          // 注：该帧来自 manager.broadcast，沿用 ``type`` 字段（非 wire 协议帧），
          // 不在 frame_type 统一范围内。
          if (
            data.type === "usage_summary_updated" &&
            typeof data.threadId === "string" &&
            data.usage
          ) {
            const usage = data.usage as ThreadUsage;
            const tid = data.threadId as string;
            const chatStore = useChatStore.getState();
            useChatStore.setState({
              usageByThread: {
                ...chatStore.usageByThread,
                [tid]: usage,
              },
            });
          }

          // thread-status 帧：原有逻辑
          const frame = data as ThreadStatusFrame;
          if (
            frame.frame_type === "thread-status" &&
            frame.threadId &&
            frame.phase
          ) {
            setStatus(
              frame.threadId,
              frame.phase,
              frame.toolName ?? undefined,
            );
          }
        } catch {
          // 非 JSON / 解析失败：静默丢弃
        }
      };

      ws.onclose = () => {
        wsRef.current = null;
        clearHeartbeat();
        // smart-approval-v2-inbox: 清 sender 避免旧 ws 闭包被复用（走 module-level ref）
        setInboxSender(null);
        if (disposedRef.current) return;
        scheduleReconnect();
      };

      ws.onerror = () => {
        // onclose 紧随；让 onclose 处理状态切换
      };
    }

    // ---- 启动 ----
    connect();

    // ---- cleanup ----
    return () => {
      disposedRef.current = true;
      clearHeartbeat();
      clearRetryTimer();
      setState("closed");
      // smart-approval-v2-inbox: 清 sender，避免 StrictMode 第二次 mount 前残留旧闭包（走 module-level ref）
      setInboxSender(null);
      if (wsRef.current) {
        try {
          wsRef.current.close();
        } catch {
          // ignore
        }
        wsRef.current = null;
      }
    };
  }, [setStatus, clearHeartbeat, clearRetryTimer]);

  return { state, latencyMs };
}
