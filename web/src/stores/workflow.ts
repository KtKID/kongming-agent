import { create } from "zustand";
import { apiGet, apiPost } from "@/lib/api";

// ── TS 接口（对应后端 dataclass）──────────────────────

export interface SpecSnapshot {
  spec_id: string;
  name: string;
  path: string;
  has_goals: boolean;
  has_module_breakdown: boolean;
  has_task_map: boolean;
  has_architecture: boolean;
}

export interface TaskSnapshot {
  task_id: string;
  name: string;
  path: string;
  spec_ref: string | null;
  stages: Record<string, "done" | "not_started">;
  pinned: boolean;
}

export interface WorkflowNode {
  id: string;
  template_id: string;
  position: number;
}

export interface WorkflowEdge {
  id: string;
  from_node_id: string;
  to_node_id: string;
  trigger: "on_completed" | "on_failed";
}

export interface WorkflowDefinition {
  id: string;
  name: string;
  description: string;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  is_preset: boolean;
  created_at: string;
}

export interface WorkflowRun {
  id: string;
  workflow_id: string;
  task_id: string;
  spec_id: string | null;
  node_states: Record<string, "done" | "not_started">;
  status: "unbound" | "active" | "completed";
  created_at: string;
  updated_at: string;
}

export interface ScannerStatus {
  last_scan_at: string | null;
  scan_interval_seconds: number;
  specs: SpecSnapshot[];
  tasks: TaskSnapshot[];
}

// ── Store ─────────────────────────────────────────────

interface WorkflowState {
  // data
  definitions: WorkflowDefinition[];
  runs: WorkflowRun[];
  scannerStatus: ScannerStatus | null;
  loading: boolean;
  error: string | null;

  // selected
  selectedTaskId: string | null;

  // polling
  _pollTimer: ReturnType<typeof setInterval> | null;

  // actions
  fetchAll: () => Promise<void>;
  createRun: (taskId: string, workflowId: string, specId?: string | null) => Promise<void>;
  triggerScan: () => Promise<void>;
  selectTask: (taskId: string | null) => void;
  togglePin: (taskId: string) => Promise<void>;
  startPolling: (intervalMs?: number) => void;
  stopPolling: () => void;
}

export const useWorkflowStore = create<WorkflowState>((set, get) => ({
  definitions: [],
  runs: [],
  scannerStatus: null,
  loading: false,
  error: null,
  selectedTaskId: null,
  _pollTimer: null,

  fetchAll: async () => {
    set({ loading: true, error: null });
    try {
      const [defs, runs, scanner] = await Promise.all([
        apiGet<WorkflowDefinition[]>("/api/workflow/definitions"),
        apiGet<WorkflowRun[]>("/api/workflow/runs"),
        apiGet<ScannerStatus>("/api/workflow/scanner/status"),
      ]);
      set({ definitions: defs, runs, scannerStatus: scanner });
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
    } finally {
      set({ loading: false });
    }
  },

  createRun: async (taskId, workflowId, specId) => {
    try {
      await apiPost("/api/workflow/runs", {
        task_id: taskId,
        workflow_id: workflowId,
        spec_id: specId ?? null,
      });
      await get().fetchAll();
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
    }
  },

  triggerScan: async () => {
    set({ loading: true });
    try {
      await apiPost("/api/workflow/scanner/trigger");
      await get().fetchAll();
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
    } finally {
      set({ loading: false });
    }
  },

  selectTask: (taskId) => set({ selectedTaskId: taskId }),

  togglePin: async (taskId) => {
    const scanner = get().scannerStatus;
    if (!scanner) return;
    // 乐观更新
    const prevTasks = scanner.tasks;
    const updated = prevTasks.map((t) =>
      t.task_id === taskId ? { ...t, pinned: !t.pinned } : t,
    );
    set({ scannerStatus: { ...scanner, tasks: updated } });
    try {
      await apiPost(`/api/workflow/tasks/${taskId}/pin`);
    } catch {
      // 失败回退：重新拉取
      await get().fetchAll();
    }
  },

  startPolling: (intervalMs) => {
    const existing = get()._pollTimer;
    if (existing) clearInterval(existing);
    const ms = intervalMs ?? (get().scannerStatus?.scan_interval_seconds ?? 30) * 1000;
    const timer = setInterval(() => {
      get().fetchAll();
    }, ms);
    set({ _pollTimer: timer });
  },

  stopPolling: () => {
    const timer = get()._pollTimer;
    if (timer) {
      clearInterval(timer);
      set({ _pollTimer: null });
    }
  },
}));
