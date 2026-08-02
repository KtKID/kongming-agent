import { create } from "zustand";
import type {
  ApprovalInboxAddFrame,
  ApprovalInboxItem,
  ApprovalInboxRemoveFrame,
  ApprovalInboxResolveFrame,
  ApprovalInboxResolveResultFrame,
  ApprovalInboxSnapshotFrame,
  RememberRule,
} from "@/protocol";
import { getSender } from "./senderRef";

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
 * inbox 是 cross-thread 全局展示队列，每条决议仍按 threadId 路由。
 */

interface ApprovalInboxState {
  /** key = requestId */
  byRequestId: Record<string, ApprovalInboxItem>;
  resolveResultByRequestId: Record<string, ApprovalInboxResolveResultFrame>;
  submissionByRequestId: Record<string, ApprovalSubmissionState>;

  // ---- 协议帧 reducer ----
  applyAddFrame: (frame: ApprovalInboxAddFrame) => void;
  applyRemoveFrame: (frame: ApprovalInboxRemoveFrame) => void;
  applySnapshotFrame: (frame: ApprovalInboxSnapshotFrame) => void;
  applyResolveResultFrame: (frame: ApprovalInboxResolveResultFrame) => void;

  // ---- 用户操作 ----
  /** 发 resolve 帧到后端；卡片保留到 authoritative remove，失败进入可重试 error */
  resolve: (
    threadId: string,
    requestId: string,
    allow: boolean,
    opts?: {
      message?: string;
      remember?: boolean;
      rememberRule?: RememberRule | null;
    },
  ) => boolean | Promise<boolean>;

  // ---- 测试用 ----
  clear: () => void;
}

export interface ApprovalSubmissionState {
  status: "submitting" | "error";
  message?: string;
}

function hasValidIdentity(
  item:
    | Partial<Pick<ApprovalInboxItem, "requestId" | "threadId">>
    | null
    | undefined,
): boolean {
  return (
    typeof item?.requestId === "string" &&
    item.requestId.length > 0 &&
    typeof item?.threadId === "string" &&
    item.threadId.length > 0
  );
}

export const useApprovalInboxStore = create<ApprovalInboxState>((set, get) => ({
  byRequestId: {},
  resolveResultByRequestId: {},
  submissionByRequestId: {},

  applyAddFrame: (frame) => {
    // hardening：入口 guard —— requestId/threadId 必须是非空 string
    if (!hasValidIdentity(frame)) {
      console.warn(
        "[approval-inbox] applyAddFrame: invalid identity, skip",
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
      resolveResultByRequestId: Object.fromEntries(
        Object.entries(s.resolveResultByRequestId).filter(
          ([requestId]) => requestId !== normalized.requestId,
        ),
      ),
      submissionByRequestId: Object.fromEntries(
        Object.entries(s.submissionByRequestId).filter(
          ([requestId]) => requestId !== normalized.requestId,
        ),
      ),
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
      const nextResults = { ...s.resolveResultByRequestId };
      delete nextResults[frame.requestId];
      const nextSubmissions = { ...s.submissionByRequestId };
      delete nextSubmissions[frame.requestId];
      return {
        byRequestId: next,
        resolveResultByRequestId: nextResults,
        submissionByRequestId: nextSubmissions,
      };
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
      if (!hasValidIdentity(item)) continue;
      // v0.5 兼容：snapshot 老条目可能也没有 timeoutMs，统一 normalize 成 null
      next[item.requestId] = {
        ...item,
        timeoutMs:
          typeof item.timeoutMs === "number" && item.timeoutMs > 0
            ? item.timeoutMs
            : null,
      };
    }
    set({
      byRequestId: next,
      resolveResultByRequestId: {},
      submissionByRequestId: {},
    });
  },

  applyResolveResultFrame: (frame) => {
    if (
      typeof frame?.requestId !== "string" ||
      !frame.requestId ||
      typeof frame.accepted !== "boolean"
    ) {
      return;
    }
    set((s) => {
      if (!(frame.requestId in s.byRequestId)) return s;
      return {
        resolveResultByRequestId: {
          ...s.resolveResultByRequestId,
          [frame.requestId]: frame,
        },
        submissionByRequestId: {
          ...s.submissionByRequestId,
          [frame.requestId]: frame.accepted
            ? { status: "submitting" }
            : {
                status: "error",
                message: frame.message ?? "审批提交失败，请重试",
              },
        },
      };
    });
  },

  resolve: (threadId, requestId, allow, opts) => {
    if (
      typeof threadId !== "string" ||
      !threadId ||
      typeof requestId !== "string" ||
      !requestId
    ) {
      console.warn(
        "[approval-inbox] resolve called with invalid identity; frame dropped",
        { threadId, requestId, allow },
      );
      return false;
    }
    const sender = getSender();
    const pendingItem = get().byRequestId[requestId];
    if (get().submissionByRequestId[requestId]?.status === "submitting") {
      return false;
    }
    const remember = opts?.remember === true;
    const frame: ApprovalInboxResolveFrame = {
      frame_type: "approval.inbox.resolve",
      threadId,
      requestId,
      allow,
      remember,
      ...(remember
        ? { rememberRule: opts?.rememberRule ?? pendingItem?.rememberRule ?? null }
        : {}),
      ...(opts?.message !== undefined ? { message: opts.message } : {}),
    };
    if (sender === null) {
      console.warn(
        "[approval-inbox] resolve called but sender not set; frame dropped",
        frame,
      );
      set((state) => ({
        submissionByRequestId: {
          ...state.submissionByRequestId,
          [requestId]: {
            status: "error",
            message: "审批连接尚未就绪，请重试",
          },
        },
      }));
      return false;
    }
    const markError = (message: string): void =>
      set((state) => {
        if (!(requestId in state.byRequestId)) return state;
        return {
          submissionByRequestId: {
            ...state.submissionByRequestId,
            [requestId]: { status: "error", message },
          },
        };
      });
    set((state) => ({
      submissionByRequestId: {
        ...state.submissionByRequestId,
        [requestId]: { status: "submitting" },
      },
    }));
    try {
      const sent = sender(frame);
      if (sent instanceof Promise) {
        return sent
          .then((result) => {
            if (result === false) {
              markError("审批发送失败，请重试");
              return false;
            }
            return true;
          })
          .catch((err: unknown) => {
            console.warn("[approval-inbox] async sender threw:", err);
            markError(err instanceof Error ? err.message : String(err));
            return false;
          });
      }
      if (sent === false) {
        markError("审批发送失败，请重试");
        return false;
      }
      return true;
    } catch (err) {
      console.warn("[approval-inbox] sender threw:", err);
      markError(err instanceof Error ? err.message : String(err));
      return false;
    }
  },

  clear: () =>
    set({
      byRequestId: {},
      resolveResultByRequestId: {},
      submissionByRequestId: {},
    }),
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
 * - danger 卡置顶
 * - 每组内部按 ``arrivedAtMs`` 递增（旧 → 新）
 *
 * 用法：``useApprovalInboxStore(selectSorted)``
 */
export function selectSorted(state: ApprovalInboxState): ApprovalInboxItem[] {
  const all = Object.values(state.byRequestId);
  const danger = all
    .filter((i) => i.danger)
    .sort((a, b) => a.arrivedAtMs - b.arrivedAtMs);
  const safe = all
    .filter((i) => !i.danger)
    .sort((a, b) => a.arrivedAtMs - b.arrivedAtMs);
  return [...danger, ...safe];
}
