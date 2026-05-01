import { create } from "zustand";
import { apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api";
import type {
  CreateThreadRequest,
  LLMPresetDTO,
  RenameThreadRequest,
  ThreadMetadataDTO,
} from "@/protocol";

interface ThreadsState {
  threads: ThreadMetadataDTO[];
  presets: LLMPresetDTO[];
  loading: boolean;
  fetchThreads: () => Promise<void>;
  fetchPresets: () => Promise<void>;
  createThread: (
    name: string,
    presetId: string,
  ) => Promise<ThreadMetadataDTO>;
  renameThread: (id: string, name: string) => Promise<void>;
  deleteThread: (id: string) => Promise<void>;
}

/**
 * thread + preset 状态。所有 mutating action 都先打 REST 再回填 store。
 * 失败由 caller 用 try/catch 处理（toast 提示）。
 */
export const useThreadsStore = create<ThreadsState>((set, get) => ({
  threads: [],
  presets: [],
  loading: false,

  fetchThreads: async () => {
    set({ loading: true });
    try {
      const list = await apiGet<ThreadMetadataDTO[]>("/api/threads");
      // updated_at 倒序
      list.sort((a, b) => b.updated_at - a.updated_at);
      set({ threads: list });
    } finally {
      set({ loading: false });
    }
  },

  fetchPresets: async () => {
    const list = await apiGet<LLMPresetDTO[]>("/api/presets");
    set({ presets: list });
  },

  createThread: async (name, presetId) => {
    const body: CreateThreadRequest = { name, preset_id: presetId };
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
        .sort((a, b) => b.updated_at - a.updated_at),
    });
  },

  deleteThread: async (id) => {
    await apiDelete(`/api/threads/${id}`);
    set({ threads: get().threads.filter((t) => t.id !== id) });
  },
}));
