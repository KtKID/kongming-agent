import { create } from "zustand";
import type { SocketState } from "@/network/manager";
import type { StatusWsState } from "@/hooks/useThreadStatusWS";

/**
 * 全局连接状态 store。
 *
 * 让 Layout header 能读到两条 WS 的状态和延迟，
 * 无需通过 props 层层传递。
 *
 * 写入方：
 * - NetworkManager (generic 频道) → setThreadWsState(state) + setThreadWsLatency(ms)
 * - useThreadStatusWS hook → setStatusWs(state, latencyMs)
 * - NetworkManager (claude 频道) → setClaudeWsState(state) + setClaudeWsLatency(ms)
 *   （v0.1 web-network-layer 拆分：state 与 latency 来自不同真源，
 *    不再用旧合并 setter `setClaudeWs(state, latency)`）
 *
 * 读取方：
 * - Layout → ConnectionIndicator × 2
 */

interface ConnectionStatusState {
  /** 对话 WS 状态 */
  threadWsState: SocketState;
  threadWsLatencyMs: number | null;
  /** 是否有活跃的对话 WS（在对话页时为 true） */
  threadWsActive: boolean;

  claudeWsState: SocketState;
  claudeWsLatencyMs: number | null;
  claudeWsActive: boolean;

  /** 全局状态 WS */
  statusWsState: StatusWsState;
  statusWsLatencyMs: number | null;

  setThreadWs: (state: SocketState, latencyMs: number | null) => void;
  setThreadWsState: (state: SocketState) => void;
  setThreadWsLatency: (ms: number | null) => void;
  setThreadWsActive: (active: boolean) => void;
  /**
   * Claude 频道连接状态（onopen/onclose/reconnecting 等生命周期变化触发）。
   * v0.1：从旧的 `setClaudeWs(state, latency)` 拆出来，独立写 `claudeWsState`，
   * 避免心跳每次更新 latency 顺便把刚切换的 state 误覆盖。
   */
  setClaudeWsState: (state: SocketState) => void;
  /**
   * Claude 频道心跳 latency（每次 pong 回来时更新）。
   * 触发频率约每 30s 一次（与 cfg.web.ws_heartbeat_interval_ms 同源）。
   */
  setClaudeWsLatency: (ms: number | null) => void;
  setClaudeWsActive: (active: boolean) => void;
  setStatusWs: (state: StatusWsState, latencyMs: number | null) => void;
}

export const useConnectionStatusStore = create<ConnectionStatusState>(
  (set) => ({
    threadWsState: "closed",
    threadWsLatencyMs: null,
    threadWsActive: false,

    claudeWsState: "closed",
    claudeWsLatencyMs: null,
    claudeWsActive: false,

    statusWsState: "closed",
    statusWsLatencyMs: null,

    setThreadWs: (state, latencyMs) =>
      set({ threadWsState: state, threadWsLatencyMs: latencyMs }),
    setThreadWsState: (state) => set({ threadWsState: state }),
    setThreadWsLatency: (ms) => set({ threadWsLatencyMs: ms }),
    setThreadWsActive: (active) =>
      set({ threadWsActive: active }),
    setClaudeWsState: (state) => set({ claudeWsState: state }),
    setClaudeWsLatency: (ms) => set({ claudeWsLatencyMs: ms }),
    setClaudeWsActive: (active) =>
      set({ claudeWsActive: active }),
    setStatusWs: (state, latencyMs) =>
      set({ statusWsState: state, statusWsLatencyMs: latencyMs }),
  }),
);
