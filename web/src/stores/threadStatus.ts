import { create } from "zustand";
import type {
  ThreadStatusFrame,
  ThreadStatusSnapshotFrame,
} from "@/protocol";

const ACTIVE_PHASES = new Set<ThreadStatusFrame["phase"]>([
  "responding",
  "thinking",
  "tool_calling",
  "waiting_approval",
]);

interface ThreadStatusState {
  statuses: Record<string, ThreadStatusFrame>;
  connectionGeneration: number;
  lastSequence: number;
  beginConnection: (generation: number) => void;
  applySnapshot: (
    frame: ThreadStatusSnapshotFrame,
    generation: number,
  ) => void;
  applyStatus: (frame: ThreadStatusFrame, generation: number) => void;
}

export const useThreadStatusStore = create<ThreadStatusState>((set) => ({
  statuses: {},
  connectionGeneration: 0,
  lastSequence: 0,
  beginConnection: (generation) =>
    set((state) =>
      generation > state.connectionGeneration
        ? { connectionGeneration: generation }
        : state,
    ),
  applySnapshot: (frame, generation) =>
    set((state) => {
      if (generation !== state.connectionGeneration) return state;
      const statuses: Record<string, ThreadStatusFrame> = {};
      for (const item of frame.items) {
        if (ACTIVE_PHASES.has(item.phase) && item.sequence <= frame.watermark) {
          statuses[item.threadId] = item;
        }
      }
      return {
        statuses,
        lastSequence: frame.watermark,
      };
    }),
  applyStatus: (frame, generation) =>
    set((state) => {
      if (
        generation !== state.connectionGeneration
        || frame.sequence <= state.lastSequence
      ) {
        return state;
      }
      if (ACTIVE_PHASES.has(frame.phase)) {
        return {
          statuses: {
            ...state.statuses,
            [frame.threadId]: frame,
          },
          lastSequence: frame.sequence,
        };
      }
      const { [frame.threadId]: _removed, ...rest } = state.statuses;
      void _removed;
      return {
        statuses: rest,
        lastSequence: frame.sequence,
      };
    }),
}));
