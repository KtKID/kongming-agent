import { create } from "zustand";
import type { ThreadStatusPhase } from "@/protocol";

interface ThreadStatusEntry {
  phase: ThreadStatusPhase;
  toolName?: string;
}

interface ThreadStatusState {
  statuses: Record<string, ThreadStatusEntry>;
  setStatus: (threadId: string, phase: ThreadStatusPhase, toolName?: string) => void;
  clearStatus: (threadId: string) => void;
}

export const useThreadStatusStore = create<ThreadStatusState>((set) => ({
  statuses: {},
  setStatus: (threadId, phase, toolName) =>
    set((s) => ({
      statuses: {
        ...s.statuses,
        [threadId]: { phase, toolName },
      },
    })),
  clearStatus: (threadId) =>
    set((s) => {
      const { [threadId]: _, ...rest } = s.statuses;
      return { statuses: rest };
    }),
}));
