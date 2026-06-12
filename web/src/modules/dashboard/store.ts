import { create } from "zustand";
import type { RuntimeStatusSnapshotDTO } from "@/protocol";
import { fetchRuntimeStatus } from "@/modules/dashboard/api";

interface RuntimeStatusState {
  snapshot: RuntimeStatusSnapshotDTO | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

export const useRuntimeStatusStore = create<RuntimeStatusState>((set) => ({
  snapshot: null,
  loading: false,
  error: null,

  refresh: async () => {
    set({ loading: true, error: null });
    try {
      const snapshot = await fetchRuntimeStatus();
      set({ snapshot, loading: false, error: null });
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      set({ loading: false, error: detail });
    }
  },
}));
