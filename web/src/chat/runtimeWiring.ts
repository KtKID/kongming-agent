/**
 * message-runtime-v0.1 · 运行时装配接缝（#5）
 *
 * 把现有传输层（ThreadSocket / CodexSocket / claude socket）包成 ChatManager 需要的
 * `NetworkHandle`，并提供 per-thread 的时间线状态机缓存。三个频道视图复用同一套装配，
 * 避免各自重写 ChatManager 依赖注入。
 *
 * 注意：本 task 不收编 transport，这里只做「现有 socket → NetworkHandle」的薄适配。
 * `network-manager-multi-channel` 收编后，本文件可改为直接向 NetworkManager 取 handle。
 */
import { useCallback, useEffect, useMemo, useSyncExternalStore } from "react";
import { ChatTimelineStore } from "@/chat/ChatTimelineStore";
import type {
  NetworkHandle,
  ChatTimelineStoreApi,
  ChatTimelineState,
} from "@/chat/types";
import { useThreadStatusStore } from "@/stores/threadStatus";

/**
 * 把一个「发送函数」包成 NetworkHandle。
 *
 * `close` 默认 no-op：generic 等频道的 socket 生命周期归各自的 hook（useWS 等）管理，
 * ChatManager 不负责关闭底层连接。需要时由 caller 显式传入 close。
 */
export function makeNetworkHandle(
  connectionId: string,
  send: (frame: unknown) => void,
  close: () => void = () => {},
): NetworkHandle {
  return { connectionId, send, close };
}

// per-thread 时间线状态机缓存。接收侧（#3）通过 useChatTimeline 订阅；发送侧（#5）
// 经 ChatManager.ingestFrame 灌入。
const TIMELINE_STORES = new Map<string, ChatTimelineStore>();

/**
 * 订阅引用计数（refcount）：记录当前有多少个 useChatTimeline 实例在用某个 thread 的 store。
 *
 * 为什么需要它：`disposeTimelineStore` 不能在还有组件订阅时误删 store，否则正在
 * 展示的视图会丢状态。refcount=0 时（最后一个订阅者切走 / unmount）才真正释放，
 * 既保证「切 N 个 thread 后缓存不无界增长」，又不误删活跃 store。
 */
const TIMELINE_REFCOUNT = new Map<string, number>();

/**
 * threadId=undefined 时回退用的稳定空 store sentinel。
 *
 * 固定 key 保证 hook 顺序稳定、永远返回同一个空 store 实例（其 state 引用稳定 →
 * useSyncExternalStore 不会因引用抖动重渲染）。此 sentinel 不计入业务 thread，
 * 也不参与 refcount 释放（避免反复 new 空 store）。
 */
const EMPTY_TIMELINE_THREAD_ID = "__empty__";

/** 取（或惰性创建）指定 thread 的时间线状态机。 */
export function getTimelineStore(threadId: string): ChatTimelineStoreApi {
  let store = TIMELINE_STORES.get(threadId);
  if (!store) {
    store = new ChatTimelineStore(threadId);
    TIMELINE_STORES.set(threadId, store);
  }
  return store;
}

/**
 * 丢弃指定 thread 的时间线缓存（thread 关闭 / 最后一个订阅者切走时调用）。
 *
 * 直接调用即无条件释放（用于显式关闭 thread）。refcount 收口在
 * `releaseTimelineStore`，只在 refcount 归零时才转调此函数。空 sentinel 不释放。
 */
export function disposeTimelineStore(threadId: string): void {
  if (threadId === EMPTY_TIMELINE_THREAD_ID) return;
  TIMELINE_STORES.delete(threadId);
  TIMELINE_REFCOUNT.delete(threadId);
}

/** 订阅者 +1（useChatTimeline 挂载 / threadId 切入时）。 */
function retainTimelineStore(threadId: string): void {
  if (threadId === EMPTY_TIMELINE_THREAD_ID) return;
  TIMELINE_REFCOUNT.set(threadId, (TIMELINE_REFCOUNT.get(threadId) ?? 0) + 1);
}

/** 订阅者 -1（useChatTimeline 卸载 / threadId 切走时）；归零则释放 store。 */
function releaseTimelineStore(threadId: string): void {
  if (threadId === EMPTY_TIMELINE_THREAD_ID) return;
  const next = (TIMELINE_REFCOUNT.get(threadId) ?? 0) - 1;
  if (next <= 0) {
    disposeTimelineStore(threadId);
  } else {
    TIMELINE_REFCOUNT.set(threadId, next);
  }
}

/** 当前缓存的 store 数量（测试 / 诊断用）。`excludeEmpty` 排除空 sentinel。 */
export function timelineStoreCount(opts?: { excludeEmpty?: boolean }): number {
  if (opts?.excludeEmpty && TIMELINE_STORES.has(EMPTY_TIMELINE_THREAD_ID)) {
    return TIMELINE_STORES.size - 1;
  }
  return TIMELINE_STORES.size;
}

/**
 * chat-receive-side-unify #3 · 视图订阅时间线状态的 React hook。
 *
 * 通过 `useSyncExternalStore` 订阅指定 thread 的 ChatTimelineStore，返回原始
 * `ChatTimelineState`（稳定引用）。视图侧再用 useMemo 做 toViewModel 投影 —— 投影
 * **不归本 hook**，本 hook 的 getSnapshot 只透传 `store.getSnapshot()`，禁止 new
 * 对象 / map / filter，否则相同 state 返回新引用 → 无限重渲染（React #185）。
 *
 * - `threadId=undefined`：回退到稳定空 store（`__empty__`），保证 hook 顺序不变、
 *   不报错，state 引用稳定。
 * - `threadId` 变化：useMemo 取到新 store，subscribe/getSnapshot 随之换引用，
 *   useSyncExternalStore 自动重订阅新 store。
 * - 清理：useEffect 在 threadId 切走 / 组件卸载时对旧 thread refcount -1，归零释放
 *   store，避免长会话 / 频繁切 thread 时缓存无界增长（disposeTimelineStore 真实接线）。
 */
export function useChatTimeline(threadId: string | undefined): ChatTimelineState {
  const effectiveId = threadId ?? EMPTY_TIMELINE_THREAD_ID;

  // store 引用随 threadId 变；同 threadId 不变时引用稳定。
  const store = useMemo(() => getTimelineStore(effectiveId), [effectiveId]);

  // subscribe / getSnapshot bind 到当前 store 实例（避免 this 丢失 + 引用随 store 稳定）。
  const subscribe = useCallback(
    (listener: () => void) => store.subscribe(listener),
    [store],
  );
  const getSnapshot = useCallback(() => store.getSnapshot(), [store]);

  // refcount 生命周期：挂载 / threadId 切入 +1，卸载 / 切走 -1（归零释放）。
  useEffect(() => {
    retainTimelineStore(effectiveId);
    return () => {
      releaseTimelineStore(effectiveId);
    };
  }, [effectiveId]);

  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

/**
 * chat-running-state-unify #6：用户发送瞬间预设 phase=responding。
 *
 * 解决「LLM 回复前无法打断」+「发送即停止状态」两个需求：让 isRunning 立即为 true
 * （不必等后端 turn.start 到达），三频道按钮立即变停止。后端真实 phase（responding
 * → complete/error/idle）会**幂等覆盖** —— turn.end 到达时 phase=complete 复位，
 * 按停止按钮立即触发 `provider.interrupt`（claude 后端 SDK 已自动**递归打断**子 agent
 * 树 + Bash subprocess，见 `src/web/claude_code/service.py::abort` 注释）。
 *
 * 集中放 ChatManager 内一处调用——三频道自动一致，符合 task "一处真源"原则。
 */
export function markThreadRunning(threadId: string): void {
  useThreadStatusStore.getState().setStatus(threadId, "responding");
}
