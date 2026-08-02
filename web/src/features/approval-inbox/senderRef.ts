import type { ApprovalInboxResolveFrame } from "@/protocol";

export type ApprovalInboxSender = (
  frame: ApprovalInboxResolveFrame,
) => boolean | void | Promise<boolean | void>;

/**
 * Module-level sender 引用（**不进 zustand store**）。
 *
 * 为什么不放 store：
 * useThreadStatusWS 在 ws.onopen / onclose / cleanup 三处会高频调 sender 注册和清理。
 * 如果 sender 字段在 zustand store 内，每次 mutate 都通知所有 subscriber（哪怕用 useShallow），
 * 在 React 19 + useSyncExternalStore 的 double-snapshot tearing 检测下会被判"snapshot 不一致"
 * 触发 forceStoreRerender，反复 commit → setSender → commit ... 最终 Maximum update depth
 * exceeded（React error #185）+ ws 反复 reconnect 风暴。
 *
 * 解决：sender 不是 React 渲染需要消费的"状态"，只是给 store.resolve 用的 callback channel。
 * 放在 module-level let + 简单 getter/setter，完全绕开 React reconciliation。
 *
 * 使用约定：
 * - hook（useThreadStatusWS）调 setSender(real, owner) / clearSender(owner)
 * - store.resolve 用 getSender() 取当前 sender 调用
 * - 测试用 resetSender() 清空（避免跨用例污染）
 */

let _sender: ApprovalInboxSender | null = null;
let _owner: symbol | null = null;

export function setSender(s: ApprovalInboxSender, owner?: symbol): void {
  _sender = s;
  _owner = owner ?? null;
}

export function getSender(): ApprovalInboxSender | null {
  return _sender;
}

export function clearSender(owner?: symbol): void {
  if (owner !== undefined && _owner !== owner) {
    return;
  }
  _sender = null;
  _owner = null;
}

export function resetSender(): void {
  clearSender();
}
