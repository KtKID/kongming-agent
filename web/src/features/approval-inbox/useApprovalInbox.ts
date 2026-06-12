import { create } from "zustand";
import { getSender } from "./senderRef";
import type {
  ApprovalInboxAddFrame,
  ApprovalInboxItem,
  ApprovalInboxRemoveFrame,
  ApprovalInboxResolveFrame,
  ApprovalInboxSnapshotFrame,
} from "./types";

/**
 * smart-approval-v2-inbox 前端 store。
 *
 * 设计原则：
 * - **不持有 WS**：store 不 new WebSocket / 不监听 onmessage。
 *   上层 hook（useThreadStatusWS）解析帧后调 ``applyAddFrame`` /
 *   ``applyRemoveFrame`` / ``applySnapshotFrame``。
 * - **sender 不在 store**：sender 走 module-level ref（``senderRef.ts``），
 *   因为 ws.onopen/onclose/cleanup 高频 mutate 会触发 React 19 useSyncExternalStore
 *   tearing 检测引爆 #185 + ws reconnect 风暴。`resolve` 调 `getSender()` 实时取。
 * - **byRequestId 索引**：增删 O(1)；排序由 ``selectSorted`` 在读侧算。
 * - **resolve 不抛**：sender = null 时 console.warn 但不抛，避免按钮卡死。
 *
 * 与 useAutoApprovalStore 的关系：完全独立 store（两者都是 cross-thread 全局，
 * 但 v1 是 per-cwd 开关状态、v2 是 per-request 队列）。
 */

interface ApprovalInboxState {
  /** key = requestId */
  byRequestId: Record<string, ApprovalInboxItem>;

  // ---- 协议帧 reducer ----
  applyAddFrame: (frame: ApprovalInboxAddFrame) => void;
  applyRemoveFrame: (frame: ApprovalInboxRemoveFrame) => void;
  applySnapshotFrame: (frame: ApprovalInboxSnapshotFrame) => void;

  // ---- 用户操作 ----
  /** 发 resolve 帧到后端；不本地删除（等后端回 remove 帧统一清） */
  resolve: (
    threadId: string,
    requestId: string,
    allow: boolean,
    opts?: {
      message?: string;
      rememberEntry?: string;
      rememberScope?: "session" | "persistent";
    },
  ) => void;

  // ---- 测试用 ----
  clear: () => void;
}

export const useApprovalInboxStore = create<ApprovalInboxState>((set) => ({
  byRequestId: {},

  applyAddFrame: (frame) => {
    // hardening：入口 guard —— requestId 必须是非空 string
    if (typeof frame?.requestId !== "string" || !frame.requestId) {
      console.warn(
        "[approval-inbox] applyAddFrame: invalid requestId, skip",
        frame,
      );
      return;
    }
    // 拆掉 frame_type，剩下的就是 ApprovalInboxItem
    const { frame_type: _frame_type, ...item } = frame;
    void _frame_type;
    // v0.5 兼容老后端：未注入 timeoutMs 字段时落 null，避免运行时 undefined
    // 触发 fallback 倒计时（fallback 仅在 number 且 > 0 时生效）
    const normalized: ApprovalInboxItem = {
      ...item,
      timeoutMs:
        typeof item.timeoutMs === "number" && item.timeoutMs > 0
          ? item.timeoutMs
          : null,
    };
    set((s) => ({
      byRequestId: {
        ...s.byRequestId,
        [normalized.requestId]: normalized,
      },
    }));
  },

  applyRemoveFrame: (frame) => {
    // hardening：入口 guard —— 非法 requestId early return
    if (typeof frame?.requestId !== "string" || !frame.requestId) {
      return;
    }
    set((s) => {
      if (!(frame.requestId in s.byRequestId)) return s;
      const next = { ...s.byRequestId };
      delete next[frame.requestId];
      return { byRequestId: next };
    });
  },

  applySnapshotFrame: (frame) => {
    // hardening：入口 guard —— items 必须是数组，单 item 必须有有效 requestId
    if (!Array.isArray(frame?.items)) {
      console.warn(
        "[approval-inbox] applySnapshotFrame: items not array, skip",
        frame,
      );
      return;
    }
    // 全量替换 —— snapshot 是真源
    const next: Record<string, ApprovalInboxItem> = {};
    for (const item of frame.items) {
      if (typeof item?.requestId !== "string" || !item.requestId) continue;
      // v0.5 兼容：snapshot 老条目可能也没有 timeoutMs，统一 normalize 成 null
      next[item.requestId] = {
        ...item,
        timeoutMs:
          typeof item.timeoutMs === "number" && item.timeoutMs > 0
            ? item.timeoutMs
            : null,
      };
    }
    set({ byRequestId: next });
  },

  resolve: (threadId, requestId, allow, opts) => {
    const sender = getSender();
    const frame: ApprovalInboxResolveFrame = {
      frame_type: "approval.inbox.resolve",
      threadId,
      requestId,
      allow,
      ...(opts?.message !== undefined ? { message: opts.message } : {}),
      ...(opts?.rememberEntry !== undefined
        ? { rememberEntry: opts.rememberEntry }
        : {}),
      ...(opts?.rememberScope !== undefined
        ? { rememberScope: opts.rememberScope }
        : {}),
    };
    if (sender === null) {
      console.warn(
        "[approval-inbox] resolve called but sender not set; frame dropped",
        frame,
      );
      return;
    }
    try {
      sender(frame);
    } catch (err) {
      // 发送失败：warn 不抛 —— 用户应可重试
      console.warn("[approval-inbox] sender threw:", err);
    }
  },

  clear: () => set({ byRequestId: {} }),
}));

// smart-approval-manager-stage1 e2e（task #8）：把 store 暴露到 window，
// 让 Playwright 能直接注入 fake inbox item 验证 UI 链路，无需启动真后端 /
// 真 LLM / 真 SafetyDecisionEngine。仅在 vite dev 模式（`import.meta.env.DEV
// === true`）生效；vite build / preview 时 DEV 是常量 false，整个 if 被
// tree-shake 掉，生产物理不带这块代码。e2e 通过 `pnpm run dev` 起 vite dev
// server 让 store 可被 page.evaluate 直接访问。
if (typeof window !== "undefined" && import.meta.env.DEV) {
  (
    window as unknown as {
      __APPROVAL_INBOX_STORE__: typeof useApprovalInboxStore;
    }
  ).__APPROVAL_INBOX_STORE__ = useApprovalInboxStore;
}

/**
 * 排序 selector（不写进 state）：
 * - 危险卡（``blockedByRule !== null``）置顶
 * - 每组内部按 ``arrivedAtMs`` 递增（旧 → 新）
 *
 * 用法：``useApprovalInboxStore(selectSorted)``
 */
export function selectSorted(state: ApprovalInboxState): ApprovalInboxItem[] {
  const all = Object.values(state.byRequestId);
  const danger = all
    .filter((i) => i.blockedByRule !== null)
    .sort((a, b) => a.arrivedAtMs - b.arrivedAtMs);
  const safe = all
    .filter((i) => i.blockedByRule === null)
    .sort((a, b) => a.arrivedAtMs - b.arrivedAtMs);
  return [...danger, ...safe];
}
