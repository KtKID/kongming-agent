import { create } from "zustand";
import { apiGet } from "@/lib/api";
import type { WorkspaceContextDTO } from "@/protocol";

export type WorkspaceTab = "chat" | "files" | "git" | "shell";

export interface WorkspaceFileOpenRequest {
  path: string;
  nonce: number;
}

export interface FileDrawerState {
  threadId: string;
  relativePath: string;   // workspace 相对路径（传给 API）
  absolutePath: string;    // 原始绝对路径（展示用）
}

interface WorkspaceState {
  contextsByThread: Record<string, WorkspaceContextDTO | undefined>;
  loadingByThread: Record<string, boolean | undefined>;
  activeTabByThread: Record<string, WorkspaceTab | undefined>;
  fileOpenRequestByThread: Record<string, WorkspaceFileOpenRequest | undefined>;
  drawerFile: FileDrawerState | null;
  fetchContext: (threadId: string) => Promise<WorkspaceContextDTO>;
  setActiveTab: (threadId: string, tab: WorkspaceTab) => void;
  requestOpenFile: (threadId: string, path: string) => void;
  openFileDrawer: (threadId: string, absolutePath: string, workspaceRoot: string) => void;
  closeFileDrawer: () => void;
  resetThreadState: (threadId: string) => void;
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  contextsByThread: {},
  loadingByThread: {},
  activeTabByThread: {},
  fileOpenRequestByThread: {},
  drawerFile: null,

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

  openFileDrawer: (threadId, absolutePath, workspaceRoot) => {
    let relativePath = absolutePath;
    if (workspaceRoot) {
      const prefix = workspaceRoot.endsWith("/") ? workspaceRoot : workspaceRoot + "/";
      if (absolutePath.startsWith(prefix)) {
        relativePath = absolutePath.slice(prefix.length);
      }
    }
    set({ drawerFile: { threadId, relativePath, absolutePath } });
  },

  closeFileDrawer: () => {
    set({ drawerFile: null });
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
    const clearDrawer = get().drawerFile?.threadId === threadId;
    set({
      contextsByThread: nextContexts,
      loadingByThread: nextLoading,
      activeTabByThread: nextTabs,
      fileOpenRequestByThread: nextFileOpenRequests,
      ...(clearDrawer ? { drawerFile: null } : {}),
    });
  },
}));
