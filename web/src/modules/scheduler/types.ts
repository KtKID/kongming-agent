/**
 * Scheduler 模块类型定义（数据真源）
 *
 * 不放 fetch / WS / UI 逻辑，只定义前端视图模型和事件模型。
 */

// ---------------------------------------------------------------------------
// 任务视图模型
// ---------------------------------------------------------------------------

export type SchedulerTaskVM = {
  taskId: string;
  name: string;
  enabled: boolean;
  state: "scheduled" | "paused" | "running" | "completed" | "failed";
  triggerType: "once" | "cron" | "interval" | "seconds";
  triggerExpr: string;
  nextRunAt: string | null;
  lastRunAt: string | null;
  timezone: string | null;
  presetId: string;
  threadId: string;
  createdBy: string;
  // v0.5.4: 后端 GET /api/cron/tasks 现已返回这两个字段，
  // 用于 edit / duplicate 模式时直接预填表单（不再依赖外部 detail 接口）。
  inputText: string;
  agentName: string;
};

// ---------------------------------------------------------------------------
// 运行记录视图模型
// ---------------------------------------------------------------------------

export type SchedulerRunVM = {
  runId: string;
  taskId: string;
  taskName: string;
  scheduledFor: string;
  startedAt: string | null;
  finishedAt: string | null;
  status: "running" | "success" | "failed" | "silent" | "cancelled";
  finalMessageExcerpt: string | null;
  deliveryStatus: string;
  deliveryError: string | null;
};

// ---------------------------------------------------------------------------
// 创建表单模型
// ---------------------------------------------------------------------------

export type SchedulerFormModel = {
  name: string;
  agentName: string;
  inputText: string;
  presetId: string;
  scheduleType: "once" | "cron";
  onceAt: string;
  cronExpr: string;
  timezone: string;
  concurrencyPolicy: "forbid" | "allow" | "replace";
  enabled: boolean;
};

// ---------------------------------------------------------------------------
// 创建请求 DTO
// ---------------------------------------------------------------------------

export type CreateSchedulerTaskRequest = {
  name: string;
  agent_name: string;
  input_text: string;
  preset_id?: string;
  schedule_type: "once" | "cron";
  once_at?: string;
  cron_expr?: string;
  timezone: string;
  concurrency_policy?: "forbid" | "allow" | "replace";
  enabled?: boolean;
};

// ---------------------------------------------------------------------------
// 编辑请求 DTO（PATCH /api/cron/tasks/{task_id}，v0.5.3）
// ---------------------------------------------------------------------------
//
// 所有字段 optional，None/undefined = 不改。后端字段命名注意：
// - `schedule` 是自由文本（ISO 时间戳 / cron 表达式 / "every 30s"），与
//   create 时的 schedule_type + once_at/cron_expr 不同——PATCH 由后端
//   `parse_schedule` 统一解析。
// - `agent` 对应 create 时的 `agent_name`（这里精简掉 _name 后缀）。

export type UpdateSchedulerTaskRequest = {
  name?: string;
  schedule?: string;
  agent?: string;
  input_text?: string;
  preset_id?: string;
  enabled?: boolean;
  concurrency_policy?: "forbid" | "allow" | "replace";
};

// ---------------------------------------------------------------------------
// Store 状态
// ---------------------------------------------------------------------------

export type SchedulerStoreState = {
  isDrawerOpen: boolean;
  isBootstrapped: boolean;
  isLoadingTasks: boolean;
  isLoadingRuns: boolean;
  tasks: SchedulerTaskVM[];
  taskMap: Record<string, SchedulerTaskVM>;
  runsByTaskId: Record<string, SchedulerRunVM[]>;
  selectedTaskId: string | null;
  filter: "all" | "running" | "paused" | "completed" | "failed";
  errorMessage: string | null;
};
