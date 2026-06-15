/**
 * Scheduler 模块 Zustand Store
 *
 * 单独持有 scheduler 模块状态，不读取其他业务 store。
 */

import { create } from "zustand";
import { useThreadsStore } from "@/stores/threads";
import type { SchedulerStoreState, SchedulerTaskVM } from "./types";
import * as api from "./api";

type SchedulerActions = {
  openDrawer: () => void;
  closeDrawer: () => void;
  bootstrap: () => Promise<void>;
  refreshTasks: () => Promise<void>;
  selectTask: (taskId: string | null) => void;
  loadRuns: (taskId: string) => Promise<void>;
  setFilter: (filter: SchedulerStoreState["filter"]) => void;
  createTask: (
    req: Parameters<typeof api.createTask>[0],
  ) => Promise<SchedulerTaskVM | null>;
  updateTask: (
    taskId: string,
    body: Parameters<typeof api.updateTask>[1],
  ) => Promise<SchedulerTaskVM | null>;
  pauseTask: (taskId: string) => Promise<void>;
  resumeTask: (taskId: string) => Promise<void>;
  runNow: (taskId: string) => Promise<void>;
  deleteTask: (taskId: string) => Promise<void>;
  upsertTaskFromWS: (task: SchedulerTaskVM) => void;
};

export const useSchedulerStore = create<SchedulerStoreState & SchedulerActions>(
  (set, get) => ({
    // --- state ---
    isDrawerOpen: false,
    isBootstrapped: false,
    isLoadingTasks: false,
    isLoadingRuns: false,
    tasks: [],
    taskMap: {},
    runsByTaskId: {},
    selectedTaskId: null,
    filter: "all",
    errorMessage: null,

    // --- actions ---
    openDrawer: () => {
      set({ isDrawerOpen: true });
      const { isBootstrapped } = get();
      if (!isBootstrapped) void get().bootstrap();
    },
    closeDrawer: () => set({ isDrawerOpen: false }),

    bootstrap: async () => {
      set({ isLoadingTasks: true, errorMessage: null });
      try {
        const tasks = await api.listTasks();
        const taskMap: Record<string, SchedulerTaskVM> = {};
        for (const t of tasks) taskMap[t.taskId] = t;
        set({ tasks, taskMap, isBootstrapped: true });
      } catch (err) {
        set({ errorMessage: String(err) });
      } finally {
        set({ isLoadingTasks: false });
      }
    },

    refreshTasks: async () => {
      set({ isLoadingTasks: true, errorMessage: null });
      try {
        const tasks = await api.listTasks();
        const taskMap: Record<string, SchedulerTaskVM> = {};
        for (const t of tasks) taskMap[t.taskId] = t;
        set({ tasks, taskMap });
      } catch (err) {
        set({ errorMessage: String(err) });
      } finally {
        set({ isLoadingTasks: false });
      }
    },

    selectTask: (taskId) => {
      set({ selectedTaskId: taskId });
      if (taskId) void get().loadRuns(taskId);
    },

    loadRuns: async (taskId) => {
      set({ isLoadingRuns: true });
      try {
        const runs = await api.listTaskRuns(taskId);
        set((s) => ({
          runsByTaskId: { ...s.runsByTaskId, [taskId]: runs },
        }));
      } catch {
        // 静默
      } finally {
        set({ isLoadingRuns: false });
      }
    },

    setFilter: (filter) => set({ filter }),

    createTask: async (req) => {
      try {
        const created = await api.createTask(req);
        try {
          await useThreadsStore.getState().fetchThreads();
        } catch (err) {
          set({ errorMessage: String(err) });
        }
        return created;
      } catch (err) {
        set({ errorMessage: String(err) });
        return null;
      }
    },

    updateTask: async (taskId, body) => {
      try {
        const updated = await api.updateTask(taskId, body);
        // 同步本地 tasks/taskMap，避免抽屉/详情列出旧数据。
        // 与 createTask 不同的是：编辑 task_id 不变，本地直接 upsert 即可，
        // 不需要等 dialog 关闭再 refresh（无新增项要选中）。
        get().upsertTaskFromWS(updated);
        return updated;
      } catch (err) {
        set({ errorMessage: String(err) });
        return null;
      }
    },

    pauseTask: async (taskId) => {
      try {
        const updated = await api.pauseTask(taskId);
        get().upsertTaskFromWS(updated);
      } catch (err) {
        set({ errorMessage: String(err) });
      }
    },

    resumeTask: async (taskId) => {
      try {
        const updated = await api.resumeTask(taskId);
        get().upsertTaskFromWS(updated);
      } catch (err) {
        set({ errorMessage: String(err) });
      }
    },

    runNow: async (taskId) => {
      try {
        await api.runTaskNow(taskId);
      } catch (err) {
        set({ errorMessage: String(err) });
      }
    },

    deleteTask: async (taskId) => {
      try {
        await api.deleteTask(taskId);
        set((s) => {
          const tasks = s.tasks.filter((t) => t.taskId !== taskId);
          const taskMap = { ...s.taskMap };
          delete taskMap[taskId];
          const runsByTaskId = { ...s.runsByTaskId };
          delete runsByTaskId[taskId];
          return {
            tasks,
            taskMap,
            runsByTaskId,
            selectedTaskId: s.selectedTaskId === taskId ? null : s.selectedTaskId,
          };
        });
      } catch (err) {
        set({ errorMessage: String(err) });
      }
    },

    upsertTaskFromWS: (task) => {
      set((s) => {
        const taskMap = { ...s.taskMap, [task.taskId]: task };
        const tasks = Object.values(taskMap);
        return { tasks, taskMap };
      });
    },
  }),
);
