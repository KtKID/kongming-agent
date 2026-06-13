import { create } from "zustand";

import { apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api";
import type {
  AddProjectRequest,
  BackendKind,
  CreateGenericThreadFromFirstMessageRequest,
  CreateGenericThreadFromFirstMessageResponse,
  CreateThreadRequest,
  LLMPresetDTO,
  ProjectRegistryEntry,
  RenameThreadRequest,
  ThreadMetadataDTO,
  UpdateThreadPresetRequest,
} from "@/protocol";

const THREADS_CACHE_KEY = "kongming.sidebar.threads";

let threadsInFlight: Promise<void> | null = null;
let presetsInFlight: Promise<void> | null = null;

function sortThreads(list: ThreadMetadataDTO[]): ThreadMetadataDTO[] {
  return [...list].sort((a, b) => {
    if (a.is_pinned !== b.is_pinned) return a.is_pinned ? -1 : 1;
    return b.updated_at - a.updated_at;
  });
}

function loadCachedThreads(): ThreadMetadataDTO[] {
  if (
    typeof window === "undefined" ||
    typeof window.localStorage === "undefined" ||
    typeof window.localStorage.getItem !== "function"
  ) {
    return [];
  }
  try {
    const raw = window.localStorage.getItem(THREADS_CACHE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as ThreadMetadataDTO[];
    return Array.isArray(parsed) ? sortThreads(parsed) : [];
  } catch {
    return [];
  }
}

function saveCachedThreads(threads: ThreadMetadataDTO[]): void {
  if (
    typeof window === "undefined" ||
    typeof window.localStorage === "undefined" ||
    typeof window.localStorage.setItem !== "function"
  ) {
    return;
  }
  try {
    window.localStorage.setItem(THREADS_CACHE_KEY, JSON.stringify(threads));
  } catch {
    // Ignore quota or privacy-mode failures.
  }
}

export interface PendingNewSession {
  cwd: string;
  projectName: string;
  backendKind: "generic_chat" | "claude_code" | "codex";
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
  createGenericThreadFromFirstMessage: (
    body: CreateGenericThreadFromFirstMessageRequest,
  ) => Promise<ThreadMetadataDTO>;
  updateThreadPreset: (
    id: string,
    presetId: string,
  ) => Promise<ThreadMetadataDTO>;
  renameThread: (id: string, name: string) => Promise<void>;
  pinThread: (id: string, isPinned: boolean) => Promise<void>;
  deleteThread: (id: string) => Promise<void>;
  setPendingNewSession: (p: PendingNewSession | null) => void;
  startPendingGenericThread: () => void;
  setInitialMessage: (msg: string | null) => void;
  triggerClaudeProjectsRefresh: () => void;
  triggerCodexProjectsRefresh: () => void;
  addClaudeProject: (cwd: string, alias?: string) => Promise<ProjectRegistryEntry>;
  removeClaudeProject: (cwd: string) => Promise<void>;
  addCodexProject: (cwd: string, alias?: string) => Promise<ProjectRegistryEntry>;
  removeCodexProject: (cwd: string) => Promise<void>;
}

export const useThreadsStore = create<ThreadsState>((set, get) => ({
  threads: loadCachedThreads(),
  presets: [],
  loading: false,
  pendingNewSession: null,
  initialMessage: null,
  claudeProjectsRefreshKey: 0,
  codexProjectsRefreshKey: 0,

  fetchThreads: async () => {
    if (threadsInFlight) return threadsInFlight;
    set({ loading: true });
    threadsInFlight = (async () => {
      try {
        const list = sortThreads(await apiGet<ThreadMetadataDTO[]>("/api/threads"));
        saveCachedThreads(list);
        set({ threads: list });
      } finally {
        set({ loading: false });
        threadsInFlight = null;
      }
    })();
    return threadsInFlight;
  },

  fetchPresets: async () => {
    if (presetsInFlight) return presetsInFlight;
    presetsInFlight = (async () => {
      try {
        const list = await apiGet<LLMPresetDTO[]>("/api/presets");
        set({ presets: list });
      } finally {
        presetsInFlight = null;
      }
    })();
    return presetsInFlight;
  },

  createThread: async (name, presetId, backendKind = "generic_chat", cwd = "") => {
    const body: CreateThreadRequest = {
      name,
      preset_id: presetId,
      backend_kind: backendKind,
      cwd,
    };
    const thread = await apiPost<ThreadMetadataDTO>("/api/threads", body);
    const threads = sortThreads([thread, ...get().threads]);
    saveCachedThreads(threads);
    set({ threads });
    return thread;
  },

  createGenericThreadFromFirstMessage: async (body) => {
    const response = await apiPost<CreateGenericThreadFromFirstMessageResponse>(
      "/api/threads/generic/first-message",
      body,
    );
    const thread = response.thread;
    const threads = sortThreads([
      thread,
      ...get().threads.filter((item) => item.id !== thread.id),
    ]);
    saveCachedThreads(threads);
    set({
      threads,
      pendingNewSession: null,
      initialMessage: null,
    });
    try {
      await get().fetchThreads();
    } catch {
      // 保留首发接口返回的真实 thread；下一次列表刷新会重新对齐。
    }
    return thread;
  },

  updateThreadPreset: async (id, presetId) => {
    const body: UpdateThreadPresetRequest = { preset_id: presetId };
    const updated = await apiPatch<ThreadMetadataDTO>(
      `/api/threads/${id}/preset`,
      body,
    );
    const threads = sortThreads(
      get().threads.map((thread) => (thread.id === id ? updated : thread)),
    );
    saveCachedThreads(threads);
    set({ threads });
    return updated;
  },

  renameThread: async (id, name) => {
    const body: RenameThreadRequest = { name };
    const updated = await apiPatch<ThreadMetadataDTO>(`/api/threads/${id}`, body);
    const threads = sortThreads(
      get().threads.map((thread) => (thread.id === id ? updated : thread)),
    );
    saveCachedThreads(threads);
    set({ threads });
  },

  pinThread: async (id, isPinned) => {
    const updated = await apiPatch<ThreadMetadataDTO>(`/api/threads/${id}`, {
      is_pinned: isPinned,
    });
    const threads = sortThreads(
      get().threads.map((thread) => (thread.id === id ? updated : thread)),
    );
    saveCachedThreads(threads);
    set({ threads });
  },

  deleteThread: async (id) => {
    await apiDelete(`/api/threads/${id}`);
    const threads = get().threads.filter((thread) => thread.id !== id);
    saveCachedThreads(threads);
    set({ threads });
  },

  setPendingNewSession: (pendingNewSession) => set({ pendingNewSession }),
  startPendingGenericThread: () =>
    set({
      pendingNewSession: {
        backendKind: "generic_chat",
        cwd: "",
        projectName: "",
      },
      initialMessage: null,
    }),
  setInitialMessage: (initialMessage) => set({ initialMessage }),
  triggerClaudeProjectsRefresh: () =>
    set((state) => ({ claudeProjectsRefreshKey: state.claudeProjectsRefreshKey + 1 })),
  triggerCodexProjectsRefresh: () =>
    set((state) => ({ codexProjectsRefreshKey: state.codexProjectsRefreshKey + 1 })),

  addClaudeProject: async (cwd, alias = "") => {
    const body: AddProjectRequest = { cwd, alias };
    const entry = await apiPost<ProjectRegistryEntry>("/api/claude/projects", body);
    set((state) => ({
      claudeProjectsRefreshKey: state.claudeProjectsRefreshKey + 1,
    }));
    return entry;
  },

  removeClaudeProject: async (cwd) => {
    await apiDelete(`/api/claude/projects?cwd=${encodeURIComponent(cwd)}`);
    set((state) => ({
      claudeProjectsRefreshKey: state.claudeProjectsRefreshKey + 1,
    }));
  },

  addCodexProject: async (cwd, alias = "") => {
    const body: AddProjectRequest = { cwd, alias };
    const entry = await apiPost<ProjectRegistryEntry>("/api/codex/projects", body);
    set((state) => ({
      codexProjectsRefreshKey: state.codexProjectsRefreshKey + 1,
    }));
    return entry;
  },

  removeCodexProject: async (cwd) => {
    await apiDelete(`/api/codex/projects?cwd=${encodeURIComponent(cwd)}`);
    set((state) => ({
      codexProjectsRefreshKey: state.codexProjectsRefreshKey + 1,
    }));
  },
}));
