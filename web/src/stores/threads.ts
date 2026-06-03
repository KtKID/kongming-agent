import { create } from "zustand";
import { apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api";
import type {
  AddProjectRequest,
  BackendKind,
  CreateThreadRequest,
  LLMPresetDTO,
  ProjectRegistryEntry,
  RenameThreadRequest,
  ThreadMetadataDTO,
} from "@/protocol";

export interface PendingNewSession {
  cwd: string;
  projectName: string;
  backendKind: "claude_code" | "codex";
}

interface ThreadsState {
  threads: ThreadMetadataDTO[];
  presets: LLMPresetDTO[];
  loading: boolean;
  pendingNewSession: PendingNewSession | null;
  initialMessage: string | null;
  claudeProjectsRefreshKey: number;
  codexProjectsRefreshKey: number;
  fetchThreads: () => Promise<void>;
  fetchPresets: () => Promise<void>;
  createThread: (
    name: string,
    presetId: string,
    backendKind?: BackendKind,
    cwd?: string,
  ) => Promise<ThreadMetadataDTO>;
  renameThread: (id: string, name: string) => Promise<void>;
  pinThread: (id: string, isPinned: boolean) => Promise<void>;
  deleteThread: (id: string) => Promise<void>;
  setPendingNewSession: (p: PendingNewSession | null) => void;
  setInitialMessage: (msg: string | null) => void;
  triggerClaudeProjectsRefresh: () => void;
  triggerCodexProjectsRefresh: () => void;
  addClaudeProject: (cwd: string, alias?: string) => Promise<ProjectRegistryEntry>;
  removeClaudeProject: (cwd: string) => Promise<void>;
  addCodexProject: (cwd: string, alias?: string) => Promise<ProjectRegistryEntry>;
  removeCodexProject: (cwd: string) => Promise<void>;
}

/**
 * thread + preset 状态。所有 mutating action 都先打 REST 再回填 store。
 * 失败由 caller 用 try/catch 处理（toast 提示）。
 */
export const useThreadsStore = create<ThreadsState>((set, get) => ({
  threads: [],
  presets: [],
  loading: false,
  pendingNewSession: null,
  initialMessage: null,
  claudeProjectsRefreshKey: 0,
  codexProjectsRefreshKey: 0,

  fetchThreads: async () => {
    set({ loading: true });
    try {
      const list = await apiGet<ThreadMetadataDTO[]>("/api/threads");
      // 先按 is_pinned 降序（置顶排前），再按 updated_at 降序
      list.sort((a, b) => {
        if (a.is_pinned !== b.is_pinned) return a.is_pinned ? -1 : 1;
        return b.updated_at - a.updated_at;
      });
      set({ threads: list });
    } finally {
      set({ loading: false });
    }
  },

  fetchPresets: async () => {
    const list = await apiGet<LLMPresetDTO[]>("/api/presets");
    set({ presets: list });
  },

  createThread: async (name, presetId, backendKind = "generic_chat", cwd = "") => {
    const body: CreateThreadRequest = {
      name,
      preset_id: presetId,
      backend_kind: backendKind,
      cwd,
    };
    const t = await apiPost<ThreadMetadataDTO>("/api/threads", body);
    set({ threads: [t, ...get().threads] });
    return t;
  },

  renameThread: async (id, name) => {
    const body: RenameThreadRequest = { name };
    const updated = await apiPatch<ThreadMetadataDTO>(
      `/api/threads/${id}`,
      body,
    );
    set({
      threads: get()
        .threads.map((t) => (t.id === id ? updated : t))
        .sort((a, b) => {
          if (a.is_pinned !== b.is_pinned) return a.is_pinned ? -1 : 1;
          return b.updated_at - a.updated_at;
        }),
    });
  },

  pinThread: async (id, isPinned) => {
    const updated = await apiPatch<ThreadMetadataDTO>(
      `/api/threads/${id}`,
      { is_pinned: isPinned },
    );
    set({
      threads: get()
        .threads.map((t) => (t.id === id ? updated : t))
        .sort((a, b) => {
          if (a.is_pinned !== b.is_pinned) return a.is_pinned ? -1 : 1;
          return b.updated_at - a.updated_at;
        }),
    });
  },

  deleteThread: async (id) => {
    await apiDelete(`/api/threads/${id}`);
    set({ threads: get().threads.filter((t) => t.id !== id) });
  },

  setPendingNewSession: (p) => set({ pendingNewSession: p }),
  setInitialMessage: (msg) => set({ initialMessage: msg }),
  triggerClaudeProjectsRefresh: () =>
    set((s) => ({ claudeProjectsRefreshKey: s.claudeProjectsRefreshKey + 1 })),
  triggerCodexProjectsRefresh: () =>
    set((s) => ({ codexProjectsRefreshKey: s.codexProjectsRefreshKey + 1 })),

  // -------------------------------------------------------------------------
  // 项目登记 actions（v0.1，web-projects-registry-v0.1）
  //
  // 4 个 action 都先打 REST 再 bump 对应 channel 的 refresh key，让
  // {Claude,Codex}ProjectsTree 通过 externalRefreshKey 重新拉数据。
  // 失败由 caller 用 try/catch 处理（toast 提示）。
  //
  // DELETE 端点用 query string 传 cwd（绝对路径含 `/`），必须 encodeURIComponent。
  // -------------------------------------------------------------------------

  addClaudeProject: async (cwd, alias = "") => {
    const body: AddProjectRequest = { cwd, alias };
    const entry = await apiPost<ProjectRegistryEntry>(
      "/api/claude/projects",
      body,
    );
    set((s) => ({ claudeProjectsRefreshKey: s.claudeProjectsRefreshKey + 1 }));
    return entry;
  },

  removeClaudeProject: async (cwd) => {
    await apiDelete(
      `/api/claude/projects?cwd=${encodeURIComponent(cwd)}`,
    );
    set((s) => ({ claudeProjectsRefreshKey: s.claudeProjectsRefreshKey + 1 }));
  },

  addCodexProject: async (cwd, alias = "") => {
    const body: AddProjectRequest = { cwd, alias };
    const entry = await apiPost<ProjectRegistryEntry>(
      "/api/codex/projects",
      body,
    );
    set((s) => ({ codexProjectsRefreshKey: s.codexProjectsRefreshKey + 1 }));
    return entry;
  },

  removeCodexProject: async (cwd) => {
    await apiDelete(
      `/api/codex/projects?cwd=${encodeURIComponent(cwd)}`,
    );
    set((s) => ({ codexProjectsRefreshKey: s.codexProjectsRefreshKey + 1 }));
  },
}));
