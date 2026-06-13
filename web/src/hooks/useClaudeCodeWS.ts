import { useEffect, useMemo, useRef, useState } from "react";
import { networkManager } from "@/network";
import type { ChannelHandle, SocketState } from "@/network/manager";
import type {
  ClaudeCodeC2SFrame,
  ClaudeCodeS2CFrame,
} from "@/protocol";
import { useConnectionStatusStore } from "@/stores/connectionStatus";
import { useHeartbeatConfig } from "@/hooks/useHeartbeatConfig";

/**
 * 包装 ChannelHandle 让旧 ClaudeCodeView API（`.on(cb)` / `.send(frame)`）
 * 平滑迁移。`.state` 也保留供 ClaudeCodeView 旧用法。
 */
export interface ClaudeCodeSocketAdapter {
  /** 订阅入站帧；返回 unsubscribe 句柄 */
  on(cb: (frame: ClaudeCodeS2CFrame) => void): () => void;
  /** 发送一帧业务数据（心跳帧由 NetworkManager 自动处理，不要走这里） */
  send(frame: ClaudeCodeC2SFrame): void;
}

/**
 * 给定 claude_code thread 的 thread_id，通过 NetworkManager 建立
 * `/ws/claude-code` 长连接。
 *
 * v0.1 web-network-layer 改造：
 * - 旧 `new ClaudeCodeSocket()` 路径整体废弃
 * - 心跳（应用层 ping/pong）走 NetworkManager + Heartbeat，治根本（中间代理
 *   60s idle 切 TCP 的痛点）
 * - 状态与 latency 上报拆成两个独立 setter
 */
export function useClaudeCodeWS(threadId: string | undefined) {
  const [state, setState] = useState<SocketState>("closed");
  const [handle, setHandle] = useState<ChannelHandle | null>(null);
  const setClaudeWsActive = useConnectionStatusStore(
    (s) => s.setClaudeWsActive,
  );
  const setClaudeWsState = useConnectionStatusStore(
    (s) => s.setClaudeWsState,
  );
  const setClaudeWsLatency = useConnectionStatusStore(
    (s) => s.setClaudeWsLatency,
  );
  const handleRef = useRef<ChannelHandle | null>(null);

  // useHeartbeatConfig 拉一次 /api/config/client；返回 undefined 表示还没拿到，
  // 此时不开连接（避免心跳跑 default 值再被覆盖）。
  const config = useHeartbeatConfig();

  useEffect(() => {
    if (!threadId) {
      setHandle(null);
      setState("closed");
      setClaudeWsActive(false);
      setClaudeWsState("closed");
      setClaudeWsLatency(null);
      return;
    }
    if (!config) {
      // 配置未到位：等下一次 effect 触发（config 变化时）
      return;
    }
    // useHeartbeatConfig 拿到 config 时三个字段一定都 set；旧 HeartbeatConfig
    // 类型把字段标 optional 是给老 useThreadStatusWS 默认值兜底用，这里收紧
    // 类型断言，避免把可选 number 推到 network/heartbeat 的严格非 optional 字段。
    if (
      typeof config.intervalMs !== "number" ||
      typeof config.backgroundIntervalMs !== "number" ||
      typeof config.timeoutMs !== "number" ||
      typeof config.maxMissed !== "number"
    ) {
      return;
    }
    setClaudeWsActive(true);
    networkManager.configure({
      intervalMs: config.intervalMs,
      backgroundIntervalMs: config.backgroundIntervalMs,
      timeoutMs: config.timeoutMs,
      maxMissed: config.maxMissed,
    });
    const h = networkManager.openChannel("claude", threadId);
    handleRef.current = h;
    setHandle(h);
    const offState = h.onState((next) => {
      setState(next);
    });
    return () => {
      offState();
      h.close();
      setClaudeWsActive(false);
      setClaudeWsState("closed");
      setClaudeWsLatency(null);
      if (handleRef.current === h) handleRef.current = null;
    };
  }, [
    threadId,
    config,
    setClaudeWsActive,
    setClaudeWsState,
    setClaudeWsLatency,
  ]);

  // 给 ClaudeCodeView 等老调用方一个稳定形态的 adapter
  const socket = useMemo<ClaudeCodeSocketAdapter | null>(() => {
    if (!handle) return null;
    return {
      on(cb): () => void {
        return handle.onMessage((raw) => {
          // raw 是 NetworkManager 已剥过心跳帧的业务帧 unknown
          cb(raw as ClaudeCodeS2CFrame);
        });
      },
      send(frame: ClaudeCodeC2SFrame): void {
        handle.send(frame);
      },
    };
  }, [handle]);

  return { socket, state };
}
