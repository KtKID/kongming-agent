/**
 * kongming-agent web-network-layer v0.1 · network 包私有工具
 *
 * ## 边界（强制）
 *
 * 本文件**只服务**于 `@/network/manager` 与 `@/network/heartbeat`。
 * `web/eslint.config.js` 的 `no-restricted-imports` 规则禁止业务层 import
 * `@/network/tools`；如果业务模块发现"需要"从这里 import 某个函数，
 * 说明该函数**不属于 network 层**，应重新评估归属（搬到 `@/lib/` 或
 * 业务模块内）。
 *
 * ## v0.1 范围
 *
 * 4 个纯函数 / type guard：
 * - `buildPingFrame(ts)`              —— 构造 PingFrame
 * - `isHeartbeatFrame(raw)`           —— narrow 到心跳帧（ping / pong）
 * - `exponentialBackoff(retryCount)`  —— 重连退避计算（1s → 30s 上限）
 * - `makeConnectionId()`              —— 8 位 hex connection id
 */

import type { PingFrame } from "@/protocol";

/**
 * 心跳帧通用形态：`frame_type` 是 `"ping"` 或 `"pong"`，`ts` 可选（client 发 ping 时填
 * `Date.now()`，server 回 pong 时原样 echo）。
 *
 * 这里定义本地宽松类型而不直接用 `@/protocol` 的 `PingFrame | PongFrame`，原因：
 * protocol 里的 `PongFrame` 要求 `timestamp_ms: number`，但 narrowing 阶段我们
 * 只关心 `frame_type` 是不是心跳种类，不强求服务端字段齐全（向前兼容字段缺失场景）。
 */
export interface HeartbeatFrameShape {
  frame_type: "ping" | "pong";
  ts?: number;
}

/**
 * 构造客户端 → 服务端的 ping 帧。
 *
 * server 收到后原样 echo `ts` 字段在 pong 帧里返回，客户端用 `Date.now() - ts`
 * 算 RTT。
 */
export function buildPingFrame(ts: number): PingFrame {
  return { frame_type: "ping", ts };
}

/**
 * Type guard：判断 raw（来自 WebSocket onmessage 的 unknown）是否为心跳帧。
 *
 * protocol-frame-type-unify-v0.2 起，所有 wire 帧统一用 `frame_type` 判别字段
 * （含心跳 / claude / codex / generic_chat / approval.inbox / cron / SSE 进度），
 * REST DTO / render item 仍保留各自的 `kind` 业务字段。
 * 详见 docs/web-network-layer-v0.1/03-core-workflows.md。
 */
export function isHeartbeatFrame(raw: unknown): raw is HeartbeatFrameShape {
  if (typeof raw !== "object" || raw === null) return false;
  if (!("frame_type" in raw)) return false;
  const ft = (raw as { frame_type: unknown }).frame_type;
  return ft === "ping" || ft === "pong";
}

/**
 * 指数退避计算：1s / 2s / 4s / 8s / 16s / 30s（上限）。
 *
 * @param retryCount 当前已重连次数（0 = 第一次重试）
 * @returns 距下次尝试的毫秒数
 */
export function exponentialBackoff(retryCount: number): number {
  const safeCount = retryCount < 0 ? 0 : retryCount;
  return Math.min(1000 * 2 ** safeCount, 30_000);
}

/**
 * 生成 8 位 hex 的 connection id 后缀（用于 conn_id 唯一化，防止同 (kind, threadId)
 * 重连后 listener 串到老连接）。
 *
 * 浏览器原生有 `crypto.randomUUID`（HTTPS + 现代浏览器全支持）；
 * 兜底 `Math.random` 仅在测试环境（jsdom 个别版本）使用。
 */
export function makeConnectionId(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return crypto.randomUUID().slice(0, 8);
  }
  return Math.random().toString(16).slice(2, 10).padEnd(8, "0");
}
