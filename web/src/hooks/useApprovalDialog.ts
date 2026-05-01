import { create } from "zustand";
import type { ApprovalRequestFrame } from "@/protocol";

/**
 * 审批 modal 队列。
 * - WS 收到 `approval.request` → push 进 pending
 * - 用户决策 → caller 取队头第一个 + 发 `approval.ack` + shift
 * - 多个并发 approval：保留 FIFO 顺序，逐个弹窗
 */
interface ApprovalDialogState {
  pending: ApprovalRequestFrame[];
  push: (frame: ApprovalRequestFrame) => void;
  /** 取队头并移除（消费一个 approval） */
  shift: () => ApprovalRequestFrame | undefined;
  clear: () => void;
}

export const useApprovalDialogStore = create<ApprovalDialogState>(
  (set, get) => ({
    pending: [],
    push: (frame) => {
      // 去重防御：同 call_id 的 request 不重复入队
      const exists = get().pending.some(
        (f) => f.call_id === frame.call_id,
      );
      if (exists) return;
      set((s) => ({ pending: [...s.pending, frame] }));
    },
    shift: () => {
      const head = get().pending[0];
      if (!head) return undefined;
      set((s) => ({ pending: s.pending.slice(1) }));
      return head;
    },
    clear: () => set({ pending: [] }),
  }),
);
