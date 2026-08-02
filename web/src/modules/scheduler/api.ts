/**
 * Scheduler 模块 REST API 封装
 *
 * 只封装 /api/cron/* 端点，对外暴露纯函数。
 * 底层走 @/lib/api 的 request 封装（自带 CSRF / 401 / 429 处理）。
 */

import { apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api";
import type {
  CronRunMessagesResponse,
  CronRunDTO,
  CronTaskDTO,
  RunNowResponse,
} from "@/protocol";
import type {
  CreateSchedulerTaskRequest,
  SchedulerRunVM,
  SchedulerTaskVM,
  UpdateSchedulerTaskRequest,
} from "./types";

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

export async function runTaskNow(taskId: string): Promise<RunNowResponse> {
  return apiPost<RunNowResponse>(`/api/cron/tasks/${taskId}/run_now`);
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

export async function loadRunMessages(
  taskId: string,
  runId: string,
): Promise<CronRunMessagesResponse> {
  return apiGet<CronRunMessagesResponse>(
    `/api/cron/tasks/${taskId}/runs/${runId}/messages`,
  );
}

// ---------------------------------------------------------------------------
// DTO → VM 映射
// ---------------------------------------------------------------------------

function taskFromDTO(dto: CronTaskDTO): SchedulerTaskVM {
  return {
    taskId: dto.task_id,
    name: dto.name,
    lifecycle: dto.lifecycle,
    latestRunStatus: dto.latest_run_status,
    liveRuntimeStatus: dto.live_runtime_status,
    triggerType: dto.trigger_type,
    triggerExpr: dto.trigger_expr,
    nextRunAt: dto.next_run_at,
    lastRunAt: dto.last_run_at,
    timezone: dto.timezone,
    presetId: dto.preset_id,
    threadId: dto.thread_id,
    createdBy: dto.created_by,
    inputText: dto.input_text,
    agentName: dto.agent_name,
  };
}

export function runFromDTO(dto: CronRunDTO): SchedulerRunVM {
  return {
    runId: dto.run_id,
    taskId: dto.task_id,
    taskName: dto.task_name,
    sessionId: dto.session_id,
    threadId: dto.thread_id,
    scheduledFor: dto.scheduled_for,
    startedAt: dto.started_at,
    finishedAt: dto.finished_at,
    status: dto.status,
    failureReason: dto.failure_reason,
    finalMessageExcerpt: dto.final_message_excerpt,
    deliveryStatus: dto.delivery_status,
    deliveryError: dto.delivery_error,
  };
}
