/**
 * EvolutionStore — 前端 evolution 事件管理。
 *
 * 专职接收后端 EvolutionManager 通过 ws 发来的 `system.notice` 帧，
 * 更新 reviewer 状态，并提供 buildSystemItem 给调用方追加到 items。
 */

import { create } from "zustand";
import { toast } from "sonner";
import type { SystemNoticeFrame } from "@/protocol";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ReviewerStatus = "idle" | "running" | "completed" | "failed";

interface ThreadEvolutionState {
  reviewerStatus: ReviewerStatus;
}

interface EvolutionState {
  byThread: Record<string, ThreadEvolutionState>;
  register: (threadId: string) => void;
  unregister: (threadId: string) => void;
  /** 更新本地 reviewer 状态 */
  handleFrame: (threadId: string, frame: SystemNoticeFrame) => void;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const useEvolutionStore = create<EvolutionState>((set) => ({
  byThread: {},

  register: (threadId) =>
    set((state) => {
      if (state.byThread[threadId]) return state;
      return {
        byThread: {
          ...state.byThread,
          [threadId]: { reviewerStatus: "idle" },
        },
      };
    }),

  unregister: (threadId) =>
    set((state) => {
      if (!state.byThread[threadId]) return state;
      const { [threadId]: _, ...rest } = state.byThread;
      return { byThread: rest };
    }),

  handleFrame: (threadId, frame) => {
    const status = frame.status;
    const reviewerStatus: ReviewerStatus =
      status === "started" ? "running" :
      status === "completed" ? "completed" :
      status === "failed" ? "failed" : "idle";

    set((state) => {
      const existing = state.byThread[threadId];
      if (!existing) return state;
      return {
        byThread: {
          ...state.byThread,
          [threadId]: { ...existing, reviewerStatus },
        },
      };
    });

    // 独立 toast 通知——不侵入 claude 频道对话流
    if (status === "started") {
      toast.info(frame.title || "进化复盘", {
        description: frame.message || "后台复盘中...",
        duration: 5000,
      });
    } else if (status === "completed") {
      toast.success(frame.title || "进化复盘", {
        description: frame.message || "复盘完成",
        duration: 8000,
      });
    } else if (status === "failed") {
      toast.error(frame.title || "进化复盘", {
        description: frame.message || "复盘失败",
        duration: 8000,
      });
    }
  },
}));
