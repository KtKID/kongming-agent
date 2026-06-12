import { create } from "zustand";

import { ApiError } from "@/lib/api";

import {
  connectProvider,
  getConnectedModelFamilies,
  getProviderCatalog,
  getProviderConnections,
  testCurrentProvider,
  testProvider,
} from "./api";
import type {
  ProviderActionResponse,
  ConnectedModelFamily,
  ProviderCatalogItem,
  ProviderConnection,
  ProviderListItem,
} from "./types";

type LoadStatus = "idle" | "loading" | "loaded" | "error";
type ActionStatus = "idle" | "running" | "success" | "error";

interface DialogState {
  providerId: string | null;
  draftApiKey: string;
  testedApiKey: string | null;
  testStatus: ActionStatus;
  saveStatus: ActionStatus;
  message: string | null;
}

interface ProviderUiState {
  currentTestStatus: Record<string, ActionStatus>;
  feedback: Record<string, string | null>;
}

interface ModelProvidersState {
  items: ProviderListItem[];
  modelFamilies: ConnectedModelFamily[];
  loadStatus: LoadStatus;
  familiesLoadStatus: LoadStatus;
  loadError: string | null;
  dialog: DialogState;
  ui: ProviderUiState;
  load: () => Promise<void>;
  loadModelFamilies: () => Promise<void>;
  openDialog: (providerId: string) => void;
  closeDialog: () => void;
  setDraftApiKey: (value: string) => void;
  testDraft: () => Promise<void>;
  saveConnection: () => Promise<void>;
  testCurrent: (providerId: string) => Promise<void>;
  reset: () => void;
}

const EMPTY_DIALOG: DialogState = {
  providerId: null,
  draftApiKey: "",
  testedApiKey: null,
  testStatus: "idle",
  saveStatus: "idle",
  message: null,
};

const EMPTY_UI: ProviderUiState = {
  currentTestStatus: {},
  feedback: {},
};

const FALLBACK_PROVIDER_CATALOG: ProviderCatalogItem[] = [
  {
    providerId: "minimax",
    displayName: "Minimax",
    regionLabel: "CN",
    description: "中国区 Minimax API Key，用于启用对应模型预设。",
    logoText: "M",
  },
  {
    providerId: "glm",
    displayName: "GLM",
    regionLabel: "CN",
    description: "智谱 GLM API Key，用于启用 GLM 模型预设。",
    logoText: "G",
  },
  {
    providerId: "deepseek",
    displayName: "DeepSeek",
    regionLabel: "CN",
    description: "DeepSeek API Key，用于启用 DeepSeek 模型预设。",
    logoText: "D",
  },
];

function describeError(err: unknown): string {
  if (err instanceof ApiError) return err.detail || err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}

function mergeProviders(
  catalog: ProviderCatalogItem[],
  connections: ProviderConnection[],
): ProviderListItem[] {
  const connectionById = new Map(connections.map((item) => [item.providerId, item]));
  return catalog.map((item) => {
    const connection = connectionById.get(item.providerId);
    return {
      ...item,
      status: connection?.status ?? "disconnected",
      model: connection?.model ?? null,
      authLabel: connection?.authLabel ?? null,
    };
  });
}

function applyConnection(
  items: ProviderListItem[],
  response: ProviderActionResponse,
): ProviderListItem[] {
  if (!response.connection) return items;
  return items.map((item) =>
    item.providerId === response.providerId
      ? {
          ...item,
          status: response.connection!.status,
          model: response.connection!.model,
          authLabel: response.connection!.authLabel,
        }
      : item,
  );
}

export const useModelProvidersStore = create<ModelProvidersState>((set, get) => ({
  items: [],
  modelFamilies: [],
  loadStatus: "idle",
  familiesLoadStatus: "idle",
  loadError: null,
  dialog: EMPTY_DIALOG,
  ui: EMPTY_UI,

  load: async () => {
    set({ loadStatus: "loading", loadError: null });
    const [catalogResult, connectionsResult] = await Promise.allSettled([
      getProviderCatalog(),
      getProviderConnections(),
    ]);
    const catalog =
      catalogResult.status === "fulfilled" && catalogResult.value.length > 0
        ? catalogResult.value
        : FALLBACK_PROVIDER_CATALOG;
    const connections =
      connectionsResult.status === "fulfilled" ? connectionsResult.value : [];

    set({
      items: mergeProviders(catalog, connections),
      loadStatus: "loaded",
      loadError: null,
    });
  },

  loadModelFamilies: async () => {
    set({ familiesLoadStatus: "loading" });
    try {
      const modelFamilies = await getConnectedModelFamilies();
      set({ modelFamilies, familiesLoadStatus: "loaded" });
    } catch {
      set({ modelFamilies: [], familiesLoadStatus: "error" });
    }
  },

  openDialog: (providerId) => {
    set({
      dialog: {
        ...EMPTY_DIALOG,
        providerId,
      },
    });
  },

  closeDialog: () => {
    set({ dialog: EMPTY_DIALOG });
  },

  setDraftApiKey: (value) => {
    const dialog = get().dialog;
    set({
      dialog: {
        ...dialog,
        draftApiKey: value,
        testedApiKey: null,
        testStatus: "idle",
        saveStatus: "idle",
        message: null,
      },
    });
  },

  testDraft: async () => {
    const dialog = get().dialog;
    const apiKey = dialog.draftApiKey.trim();
    if (!dialog.providerId || apiKey.length < 8) return;
    set({
      dialog: {
        ...dialog,
        testStatus: "running",
        saveStatus: "idle",
        message: "测试中...",
      },
    });
    try {
      const response = await testProvider(dialog.providerId, { apiKey });
      if (!response.ok) {
        set({
          dialog: {
            ...get().dialog,
            testedApiKey: null,
            testStatus: "error",
            saveStatus: "idle",
            message: response.message,
          },
        });
        return;
      }
      set({
        dialog: {
          ...get().dialog,
          testedApiKey: apiKey,
          testStatus: "success",
          message: response.message || "连接测试通过。",
        },
      });
    } catch (err) {
      set({
        dialog: {
          ...get().dialog,
          testedApiKey: null,
          testStatus: "error",
          saveStatus: "idle",
          message: describeError(err),
        },
      });
    }
  },

  saveConnection: async () => {
    const dialog = get().dialog;
    const apiKey = dialog.draftApiKey.trim();
    if (!dialog.providerId || dialog.testedApiKey !== apiKey || apiKey.length < 8) {
      return;
    }
    set({
      dialog: {
        ...dialog,
        saveStatus: "running",
        message: "保存中...",
      },
    });
    try {
      const response = await connectProvider(dialog.providerId, { apiKey });
      set({
        items: applyConnection(get().items, response),
        dialog: EMPTY_DIALOG,
        ui: {
          ...get().ui,
          feedback: {
            ...get().ui.feedback,
            [response.providerId]: response.message || "已保存，刚刚测试通过。",
          },
        },
      });
      void get().loadModelFamilies();
    } catch (err) {
      set({
        dialog: {
          ...get().dialog,
          saveStatus: "error",
          message: describeError(err),
        },
      });
    }
  },

  testCurrent: async (providerId) => {
    const ui = get().ui;
    set({
      ui: {
        currentTestStatus: {
          ...ui.currentTestStatus,
          [providerId]: "running",
        },
        feedback: {
          ...ui.feedback,
          [providerId]: "正在测试已保存连接...",
        },
      },
    });
    try {
      const response = await testCurrentProvider(providerId);
      const currentUi = get().ui;
      if (!response.ok) {
        set({
          ui: {
            currentTestStatus: {
              ...currentUi.currentTestStatus,
              [providerId]: "error",
            },
            feedback: {
              ...currentUi.feedback,
              [providerId]: response.message,
            },
          },
        });
        return;
      }
      set({
        ui: {
          currentTestStatus: {
            ...currentUi.currentTestStatus,
            [providerId]: "success",
          },
          feedback: {
            ...currentUi.feedback,
            [providerId]: response.message || "已保存连接测试通过。",
          },
        },
      });
    } catch (err) {
      const currentUi = get().ui;
      set({
        ui: {
          currentTestStatus: {
            ...currentUi.currentTestStatus,
            [providerId]: "error",
          },
          feedback: {
            ...currentUi.feedback,
            [providerId]: describeError(err),
          },
        },
      });
    }
  },

  reset: () => {
    set({
      items: [],
      modelFamilies: [],
      loadStatus: "idle",
      familiesLoadStatus: "idle",
      loadError: null,
      dialog: EMPTY_DIALOG,
      ui: EMPTY_UI,
    });
  },
}));
