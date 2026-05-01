import { create } from "zustand";
import { apiGet, apiPost } from "@/lib/api";
import type { CellSummaryDTO } from "@/protocol";

interface CellsState {
  cells: CellSummaryDTO[];
  loading: boolean;
  refresh: () => Promise<void>;
  stop: (threadId: string) => Promise<void>;
}

/**
 * 管理页 cell 列表（5s polling）。
 *
 * 端点约定：
 * - GET /api/manage/cells              → CellSummaryDTO[]
 * - POST /api/manage/cells/:id/stop    → 204（手动停止 cell）
 */
export const useCellsStore = create<CellsState>((set, get) => ({
  cells: [],
  loading: false,

  refresh: async () => {
    set({ loading: true });
    try {
      const list = await apiGet<CellSummaryDTO[]>("/api/manage/cells");
      list.sort((a, b) => b.last_active_at - a.last_active_at);
      set({ cells: list });
    } finally {
      set({ loading: false });
    }
  },

  stop: async (threadId) => {
    await apiPost<void>(`/api/manage/cells/${threadId}/stop`);
    set({ cells: get().cells.filter((c) => c.thread_id !== threadId) });
  },
}));
