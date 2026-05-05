import { create } from "zustand";
import { apiGet } from "@/lib/api";
import type { WorkspaceContextDTO } from "@/protocol";

export type WorkspaceTab = "chat" | "files" | "git" | "shell";

export interface WorkspaceFileOpenRequest {
  path: string;
  nonce: number;
}

interface WorkspaceState {
  contextsByThread: Record<string, WorkspaceContextDTO | undefined>;
  loadingByThread: Record<string, boolean | undefined>;
  activeTabByThread: Record<string, WorkspaceTab | undefined>;
  fileOpenRequestByThread: Record<string, WorkspaceFileOpenRequest | undefined>;
  fetchContext: (threadId: string) => Promise<WorkspaceContextDTO>;
  setActiveTab: (threadId: string, tab: WorkspaceTab) => void;
  requestOpenFile: (threadId: string, path: string) => void;
  resetThreadState: (threadId: string) => void;
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  contextsByThread: {},
  loadingByThread: {},
  activeTabByThread: {},
  fileOpenRequestByThread: {},

  fetchContext: async (threadId) => {
    set((state) => ({
      loadingByThread: {
        ...state.loadingByThread,
        [threadId]: true,
      },
    }));
    try {
      const context = await apiGet<WorkspaceContextDTO>(
        `/api/threads/${threadId}/workspace-context`,
      );
      set((state) => ({
        contextsByThread: {
          ...state.contextsByThread,
          [threadId]: context,
        },
      }));
      return context;
    } finally {
      set((state) => ({
        loadingByThread: {
          ...state.loadingByThread,
          [threadId]: false,
        },
      }));
    }
  },

  setActiveTab: (threadId, tab) => {
    set((state) => ({
      activeTabByThread: {
        ...state.activeTabByThread,
        [threadId]: tab,
      },
    }));
  },

  requestOpenFile: (threadId, path) => {
    set((state) => ({
      fileOpenRequestByThread: {
        ...state.fileOpenRequestByThread,
        [threadId]: {
          path,
          nonce: (state.fileOpenRequestByThread[threadId]?.nonce ?? 0) + 1,
        },
      },
    }));
  },

  resetThreadState: (threadId) => {
    const nextContexts = { ...get().contextsByThread };
    const nextLoading = { ...get().loadingByThread };
    const nextTabs = { ...get().activeTabByThread };
    const nextFileOpenRequests = { ...get().fileOpenRequestByThread };
    delete nextContexts[threadId];
    delete nextLoading[threadId];
    delete nextTabs[threadId];
    delete nextFileOpenRequests[threadId];
    set({
      contextsByThread: nextContexts,
      loadingByThread: nextLoading,
      activeTabByThread: nextTabs,
      fileOpenRequestByThread: nextFileOpenRequests,
    });
  },
}));
