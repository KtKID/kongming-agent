import { create } from "zustand";
import {
  fetchAgentWorkflowArtifact,
  fetchAgentWorkflowConversation,
  fetchAgentWorkflowDetail,
  fetchAgentWorkflows,
  fetchThreadUsage,
} from "./api";
import type {
  ConversationDTO,
  WorkflowArtifactContentDTO,
  WorkflowDetailDTO,
  WorkflowListDTO,
} from "./types";

export const conversationKey = (workflowId: string, taskRunId: string) =>
  `${workflowId}::${taskRunId}`;

interface WorkflowViewerState {
  activeThreadId: string | null;
  list: WorkflowListDTO | null;
  detail: WorkflowDetailDTO | null;
  conversations: Record<string, ConversationDTO>;
  artifact: WorkflowArtifactContentDTO | null;
  threadUsage: unknown;
  loadingList: boolean;
  loadingDetail: boolean;
  loadingConversation: boolean;
  loadingArtifact: boolean;
  loadingThreadUsage: boolean;
  error: string | null;
  artifactError: string | null;
  loadList: (threadId: string) => Promise<void>;
  loadThreadUsage: (threadId: string) => Promise<void>;
  loadDetail: (threadId: string, workflowId: string) => Promise<void>;
  loadConversation: (
    threadId: string,
    workflowId: string,
    taskRunId: string,
  ) => Promise<void>;
  loadArtifact: (
    threadId: string,
    workflowId: string,
    artifactId: string,
  ) => Promise<void>;
  clearThread: (threadId: string) => void;
  clearArtifact: () => void;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export const useAgentWorkflowViewerStore = create<WorkflowViewerState>((set) => ({
  activeThreadId: null,
  list: null,
  detail: null,
  conversations: {},
  artifact: null,
  threadUsage: null,
  loadingList: false,
  loadingDetail: false,
  loadingConversation: false,
  loadingArtifact: false,
  loadingThreadUsage: false,
  error: null,
  artifactError: null,

  loadList: async (threadId) => {
    set({
      activeThreadId: threadId,
      loadingList: true,
      error: null,
    });
    try {
      const list = await fetchAgentWorkflows(threadId);
      set({ list, loadingList: false, error: null });
    } catch (error) {
      set({ loadingList: false, error: errorMessage(error) });
    }
  },

  loadThreadUsage: async (threadId) => {
    set({ loadingThreadUsage: true, error: null });
    try {
      const threadUsage = await fetchThreadUsage(threadId);
      set({ threadUsage, loadingThreadUsage: false, error: null });
    } catch (error) {
      set({ loadingThreadUsage: false, error: errorMessage(error) });
    }
  },

  loadDetail: async (threadId, workflowId) => {
    set({
      activeThreadId: threadId,
      loadingDetail: true,
      detail: null,
      artifact: null,
      artifactError: null,
      error: null,
    });
    try {
      const detail = await fetchAgentWorkflowDetail(threadId, workflowId);
      set({ detail, loadingDetail: false, error: null });
    } catch (error) {
      set({ loadingDetail: false, error: errorMessage(error) });
    }
  },

  loadConversation: async (threadId, workflowId, taskRunId) => {
    set({ loadingConversation: true, error: null });
    try {
      const conversation = await fetchAgentWorkflowConversation({
        threadId,
        workflowId,
        taskRunId,
        limit: 300,
      });
      set((state) => ({
        conversations: {
          ...state.conversations,
          [conversationKey(workflowId, taskRunId)]: conversation,
        },
        loadingConversation: false,
        error: null,
      }));
    } catch (error) {
      set({ loadingConversation: false, error: errorMessage(error) });
    }
  },

  loadArtifact: async (threadId, workflowId, artifactId) => {
    set({ loadingArtifact: true, artifactError: null });
    try {
      const artifact = await fetchAgentWorkflowArtifact({
        threadId,
        workflowId,
        artifactId,
      });
      set({ artifact, loadingArtifact: false, artifactError: null });
    } catch (error) {
      set({
        loadingArtifact: false,
        artifactError: errorMessage(error),
      });
    }
  },

  clearThread: (threadId) =>
    set((state) =>
      state.activeThreadId === threadId
        ? {}
        : {
            activeThreadId: threadId,
            list: null,
            detail: null,
            conversations: {},
            artifact: null,
            threadUsage: null,
            error: null,
            artifactError: null,
          },
    ),

  clearArtifact: () => set({ artifact: null, artifactError: null }),
}));
