/**
 * kongming-agent web-network-layer v0.1 · 模块入口
 *
 * ## v0.1 范围
 *
 * - 暴露 `networkManager` 单例（业务侧 `import { networkManager } from "@/network"`）
 * - 暴露 `Heartbeat` 类与 `HeartbeatConfig` / `HeartbeatHooks` 类型
 *   （供 manager 内部以及测试代码使用；业务侧通常**不应**直接 new Heartbeat）
 * - 暴露 `SocketState` / `ChannelKind` / `ChannelHandle` 三个公共类型
 *
 * **不**从此处 re-export `@/network/tools`——tools 是 network 包私有工具，
 * 由 eslint `no-restricted-imports` 规则强制只能在 `@/network/` 内部使用。
 *
 * 设计文档：
 * - `docs/web-network-layer-v0.1/01-goals-and-boundaries.md`
 * - `docs/web-network-layer-v0.1/02-module-breakdown.md`
 * - `docs/web-network-layer-v0.1/04-data-and-state.md`
 */

export { NetworkManager, networkManager } from "./manager";
export type { ChannelHandle, ChannelKind, SocketState } from "./manager";

export { Heartbeat } from "./heartbeat";
export type { HeartbeatConfig, HeartbeatHooks } from "./heartbeat";
