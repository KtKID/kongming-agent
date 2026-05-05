import { create } from "zustand";
import { apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api";
import type {
  BackendKind,
  CreateThreadRequest,
  LLMPresetDTO,
  RenameThreadRequest,
  ThreadMetadataDTO,
} from "@/protocol";
import { useChatStore } from "@/stores/chat";

interface ThreadsState {
  threads: ThreadMetadataDTO[];
  presets: LLMPresetDTO[];
  loading: boolean;
  fetchThreads: () => Promise<void>;
  fetchPresets: () => Promise<void>;
  /**
   * 创建 thread。
   *
   * v0.1.6：
   * - `backendKind` 缺省 `"generic_chat"`
   * - `backendKind="claude_code"` 时 `presetId` 可传空字符串（后端忽略）
   * - `backendKind="generic_chat"` 时 `presetId` 必须非空
   */
  createThread: (
    name: string,
    presetId: string,
    backendKind?: BackendKind,
    cwd?: string,
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
      useChatStore.getState().hydrateUsageFromThreads(list);
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
        .sort((a, b) => b.updated_at - a.updated_at),
    });
  },

  deleteThread: async (id) => {
    await apiDelete(`/api/threads/${id}`);
    set({ threads: get().threads.filter((t) => t.id !== id) });
  },
}));
