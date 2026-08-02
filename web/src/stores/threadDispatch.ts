import { create } from "zustand";

export type ThreadDispatchPhase = "dispatching" | "error";

export interface ThreadDispatchState {
  phase: ThreadDispatchPhase;
  message?: string;
}

interface ThreadDispatchStore {
  byThreadId: Record<string, ThreadDispatchState>;
  begin: (threadId: string) => void;
  succeed: (threadId: string) => void;
  fail: (threadId: string, message: string) => void;
  clear: () => void;
}

export const useThreadDispatchStore = create<ThreadDispatchStore>((set) => ({
  byThreadId: {},
  begin: (threadId) =>
    set((state) => ({
      byThreadId: {
        ...state.byThreadId,
        [threadId]: { phase: "dispatching" },
      },
    })),
  succeed: (threadId) =>
    set((state) => {
      const { [threadId]: _removed, ...rest } = state.byThreadId;
      void _removed;
      return { byThreadId: rest };
    }),
  fail: (threadId, message) =>
    set((state) => ({
      byThreadId: {
        ...state.byThreadId,
        [threadId]: { phase: "error", message },
      },
    })),
  clear: () => set({ byThreadId: {} }),
}));
