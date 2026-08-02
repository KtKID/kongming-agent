/**
 * Scheduler 模块 Zustand Store
 *
 * 单独持有 scheduler 模块状态，不读取其他业务 store。
 */

import { create } from "zustand";
import { useThreadsStore } from "@/stores/threads";
import type {
  SchedulerStoreState,
  SchedulerTaskRuntimeStatus,
  SchedulerTaskVM,
} from "./types";
import * as api from "./api";

let tasksRequestGeneration = 0;
const runsRequestGenerationByTaskId = new Map<string, number>();
let runsRequestsInFlight = 0;

function mergeTaskSnapshot(
  tasks: SchedulerTaskVM[],
  state: SchedulerStoreState,
): {
  tasks: SchedulerTaskVM[];
  taskMap: Record<string, SchedulerTaskVM>;
  runtimeStatusByTaskId: Record<string, SchedulerTaskRuntimeStatus>;
} {
  const mergedTasks = tasks.map((task) => {
    const hasLiveRun = (state.liveRunIdsByTaskId[task.taskId]?.length ?? 0) > 0;
    const hasPendingManualRun = state.pendingManualRunTaskId === task.taskId;
    return hasLiveRun || hasPendingManualRun
      ? { ...task, liveRuntimeStatus: "running" as const }
      : task;
  });
  const taskMap: Record<string, SchedulerTaskVM> = {};
  for (const task of mergedTasks) taskMap[task.taskId] = task;
  const runtimeStatusByTaskId = Object.fromEntries(
    mergedTasks.map((task) => [task.taskId, task.liveRuntimeStatus]),
  );
  return { tasks: mergedTasks, taskMap, runtimeStatusByTaskId };
}

type SchedulerActions = {
  openDrawer: () => void;
  closeDrawer: () => void;
  bootstrap: () => Promise<void>;
  refreshTasks: () => Promise<void>;
  selectTask: (taskId: string | null) => void;
  selectRun: (taskId: string, runId: string | null) => void;
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
  setTaskRuntimeStatus: (
    taskId: string,
    status: SchedulerTaskRuntimeStatus,
  ) => void;
  markRunStarted: (taskId: string, runId: string) => void;
  markRunFinished: (taskId: string, runId: string) => void;
  markPendingManualRun: (taskId: string | null) => void;
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
    runtimeStatusByTaskId: {},
    liveRunIdsByTaskId: {},
    selectedRunIdByTaskId: {},
    pendingManualRunTaskId: null,
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
      const generation = ++tasksRequestGeneration;
      set({ isLoadingTasks: true, errorMessage: null });
      try {
        const tasks = await api.listTasks();
        if (generation !== tasksRequestGeneration) return;
        const snapshot = mergeTaskSnapshot(tasks, get());
        set({
          ...snapshot,
          isBootstrapped: true,
        });
      } catch (err) {
        if (generation === tasksRequestGeneration) {
          set({ errorMessage: String(err) });
        }
      } finally {
        if (generation === tasksRequestGeneration) {
          set({ isLoadingTasks: false });
        }
      }
    },

    refreshTasks: async () => {
      const generation = ++tasksRequestGeneration;
      set({ isLoadingTasks: true, errorMessage: null });
      try {
        const tasks = await api.listTasks();
        if (generation !== tasksRequestGeneration) return;
        set(mergeTaskSnapshot(tasks, get()));
      } catch (err) {
        if (generation === tasksRequestGeneration) {
          set({ errorMessage: String(err) });
        }
      } finally {
        if (generation === tasksRequestGeneration) {
          set({ isLoadingTasks: false });
        }
      }
    },

    selectTask: (taskId) => {
      set({ selectedTaskId: taskId });
      if (taskId) void get().loadRuns(taskId);
    },

    selectRun: (taskId, runId) => {
      set((s) => ({
        selectedRunIdByTaskId: {
          ...s.selectedRunIdByTaskId,
          [taskId]: runId,
        },
      }));
    },

    loadRuns: async (taskId) => {
      const generation = (runsRequestGenerationByTaskId.get(taskId) ?? 0) + 1;
      runsRequestGenerationByTaskId.set(taskId, generation);
      runsRequestsInFlight += 1;
      set({ isLoadingRuns: true });
      try {
        const runs = await api.listTaskRuns(taskId);
        if (runsRequestGenerationByTaskId.get(taskId) !== generation) return;
        set((s) => ({
          runsByTaskId: { ...s.runsByTaskId, [taskId]: runs },
          selectedRunIdByTaskId: {
            ...s.selectedRunIdByTaskId,
            [taskId]:
              runs.find((run) => run.runId === s.selectedRunIdByTaskId[taskId])
                ?.runId ??
              runs[0]?.runId ??
              null,
          },
        }));
      } catch {
        // 静默
      } finally {
        runsRequestsInFlight -= 1;
        set({ isLoadingRuns: runsRequestsInFlight > 0 });
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
        set((s) => ({
          pendingManualRunTaskId: taskId,
          runtimeStatusByTaskId: {
            ...s.runtimeStatusByTaskId,
            [taskId]: "running",
          },
        }));
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
          const runtimeStatusByTaskId = { ...s.runtimeStatusByTaskId };
          delete runtimeStatusByTaskId[taskId];
          const liveRunIdsByTaskId = { ...s.liveRunIdsByTaskId };
          delete liveRunIdsByTaskId[taskId];
          const selectedRunIdByTaskId = { ...s.selectedRunIdByTaskId };
          delete selectedRunIdByTaskId[taskId];
          return {
            tasks,
            taskMap,
            runsByTaskId,
            runtimeStatusByTaskId,
            liveRunIdsByTaskId,
            selectedRunIdByTaskId,
            pendingManualRunTaskId:
              s.pendingManualRunTaskId === taskId ? null : s.pendingManualRunTaskId,
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

    setTaskRuntimeStatus: (taskId, status) => {
      set((s) => ({
        runtimeStatusByTaskId: {
          ...s.runtimeStatusByTaskId,
          [taskId]: status,
        },
      }));
    },

    markRunStarted: (taskId, runId) => {
      set((s) => {
        const current = s.liveRunIdsByTaskId[taskId] ?? [];
        const liveRunIds = current.includes(runId)
          ? current
          : [...current, runId];
        return {
          liveRunIdsByTaskId: {
            ...s.liveRunIdsByTaskId,
            [taskId]: liveRunIds,
          },
          runtimeStatusByTaskId: {
            ...s.runtimeStatusByTaskId,
            [taskId]: "running",
          },
        };
      });
    },

    markRunFinished: (taskId, runId) => {
      set((s) => {
        const knownLiveRuns = s.liveRunIdsByTaskId[taskId] ?? [];
        const remaining = knownLiveRuns.filter(
          (candidate) => candidate !== runId,
        );
        const nextRuntimeStatus =
          remaining.length > 0
            ? "running"
            : s.runtimeStatusByTaskId[taskId] ?? "idle";
        return {
          liveRunIdsByTaskId: {
            ...s.liveRunIdsByTaskId,
            [taskId]: remaining,
          },
          runtimeStatusByTaskId: {
            ...s.runtimeStatusByTaskId,
            [taskId]: nextRuntimeStatus,
          },
        };
      });
    },

    markPendingManualRun: (taskId) => {
      set({ pendingManualRunTaskId: taskId });
    },
  }),
);
