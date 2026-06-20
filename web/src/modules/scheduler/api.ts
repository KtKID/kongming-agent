/**
 * Scheduler 模块 REST API 封装
 *
 * 只封装 /api/cron/* 端点，对外暴露纯函数。
 * 底层走 @/lib/api 的 request 封装（自带 CSRF / 401 / 429 处理）。
 */

import { apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api";
import type {
  CreateSchedulerTaskRequest,
  SchedulerRunVM,
  SchedulerTaskVM,
  UpdateSchedulerTaskRequest,
} from "./types";

// ---------------------------------------------------------------------------
// 后端原始 DTO（与 cron.py 的 CronTaskDTO / CronRunDTO 对齐）
// ---------------------------------------------------------------------------

type CronTaskDTO = {
  task_id: string;
  name: string;
  enabled: boolean;
  state: string;
  trigger_type: string;
  trigger_expr: string;
  next_run_at: string | null;
  last_run_at: string | null;
  preset_id: string;
  thread_id?: string;
  created_by: string;
  // v0.5.4: 后端把任务内容与 agent 名直接随 GET /tasks 返回，
  // 供 edit / duplicate 模式预填表单（旧字段缺失时映射为空串兜底）。
  input_text?: string;
  agent_name?: string;
};

type CronRunDTO = {
  run_id: string;
  task_id: string;
  task_name: string;
  scheduled_for: string;
  started_at: string | null;
  finished_at: string | null;
  status: string;
  final_message_excerpt: string | null;
  delivery_status: string;
  delivery_error: string | null;
};

// ---------------------------------------------------------------------------
// 公开 API 函数
// ---------------------------------------------------------------------------

export async function listTasks(): Promise<SchedulerTaskVM[]> {
  const dtos = await apiGet<CronTaskDTO[]>("/api/cron/tasks");
  return dtos.map(taskFromDTO);
}

export async function getTask(taskId: string): Promise<SchedulerTaskVM> {
  const dto = await apiGet<CronTaskDTO>(`/api/cron/tasks/${taskId}`);
  return taskFromDTO(dto);
}

export async function createTask(
  req: CreateSchedulerTaskRequest,
): Promise<SchedulerTaskVM> {
  const dto = await apiPost<CronTaskDTO>("/api/cron/tasks", req);
  return taskFromDTO(dto);
}

export async function updateTask(
  taskId: string,
  body: UpdateSchedulerTaskRequest,
): Promise<SchedulerTaskVM> {
  const dto = await apiPatch<CronTaskDTO>(
    `/api/cron/tasks/${taskId}`,
    body,
  );
  return taskFromDTO(dto);
}

export async function pauseTask(taskId: string): Promise<SchedulerTaskVM> {
  const dto = await apiPost<CronTaskDTO>(
    `/api/cron/tasks/${taskId}/pause`,
  );
  return taskFromDTO(dto);
}

export async function resumeTask(taskId: string): Promise<SchedulerTaskVM> {
  const dto = await apiPost<CronTaskDTO>(
    `/api/cron/tasks/${taskId}/resume`,
  );
  return taskFromDTO(dto);
}

export async function runTaskNow(taskId: string): Promise<void> {
  await apiPost(`/api/cron/tasks/${taskId}/run_now`);
}

export async function deleteTask(taskId: string): Promise<void> {
  await apiDelete(`/api/cron/tasks/${taskId}`);
}

export async function listTaskRuns(
  taskId: string,
  limit = 20,
): Promise<SchedulerRunVM[]> {
  const dtos = await apiGet<CronRunDTO[]>(
    `/api/cron/tasks/${taskId}/runs?limit=${limit}`,
  );
  return dtos.map(runFromDTO);
}

// ---------------------------------------------------------------------------
// DTO → VM 映射
// ---------------------------------------------------------------------------

function taskFromDTO(dto: CronTaskDTO): SchedulerTaskVM {
  return {
    taskId: dto.task_id,
    name: dto.name,
    enabled: dto.enabled,
    state: dto.state as SchedulerTaskVM["state"],
    triggerType: dto.trigger_type as SchedulerTaskVM["triggerType"],
    triggerExpr: dto.trigger_expr,
    nextRunAt: dto.next_run_at,
    lastRunAt: dto.last_run_at,
    timezone: null,
    presetId: dto.preset_id,
    threadId: dto.thread_id ?? "",
    createdBy: dto.created_by,
    inputText: dto.input_text ?? "",
    agentName: dto.agent_name ?? "",
  };
}

function runFromDTO(dto: CronRunDTO): SchedulerRunVM {
  return {
    runId: dto.run_id,
    taskId: dto.task_id,
    taskName: dto.task_name,
    scheduledFor: dto.scheduled_for,
    startedAt: dto.started_at,
    finishedAt: dto.finished_at,
    status: dto.status as SchedulerRunVM["status"],
    finalMessageExcerpt: dto.final_message_excerpt,
    deliveryStatus: dto.delivery_status,
    deliveryError: dto.delivery_error,
  };
}
