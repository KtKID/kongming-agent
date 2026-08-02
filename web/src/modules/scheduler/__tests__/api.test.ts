import { describe, expect, it, vi, beforeEach } from "vitest";

import { apiGet, apiPost } from "@/lib/api";
import {
  createTask,
  listTaskRuns,
  listTasks,
  loadRunMessages,
  runFromDTO,
} from "@/modules/scheduler/api";
import type {
  CreateCronTaskRequest,
  CronRunMessagesResponse,
  CronRunsPage,
  CronTaskDTO,
  RunNowResponse,
  UpdateCronTaskRequest,
} from "@/protocol";

vi.mock("@/lib/api", () => ({
  apiDelete: vi.fn(),
  apiGet: vi.fn(),
  apiPatch: vi.fn(),
  apiPost: vi.fn(),
}));

const baseTaskDTO = {
  task_id: "task-1",
  name: "每天早报",
  lifecycle: "scheduled",
  latest_run_status: "failed",
  live_runtime_status: "idle",
  trigger_type: "cron",
  trigger_expr: "0 9 * * *",
  timezone: "Asia/Shanghai",
  next_run_at: null,
  last_run_at: null,
  preset_id: "preset-a",
  thread_id: "",
  created_by: "user",
  input_text: "总结今天",
  agent_name: "default",
} satisfies CronTaskDTO;

const defaultedCreateRequest = {
  name: "每天早报",
  agent_name: "default",
  input_text: "总结今天",
  schedule_type: "cron",
} satisfies CreateCronTaskRequest;

const nullableUpdateRequest = {
  name: null,
  preset_id: null,
  lifecycle: null,
} satisfies UpdateCronTaskRequest;

const emptyMessages = { messages: [] } satisfies CronRunMessagesResponse;
const emptyRunsPage = {
  runs: [],
  next_cursor: null,
} satisfies CronRunsPage;
const pendingRun = {
  run_id: "pending-1",
  status: "PENDING",
} satisfies RunNowResponse;

void defaultedCreateRequest;
void nullableUpdateRequest;
void emptyMessages;
void emptyRunsPage;
void pendingRun;

beforeEach(() => {
  vi.mocked(apiGet).mockReset();
  vi.mocked(apiPost).mockReset();
});

describe("scheduler api", () => {
  it("maps CronTaskDTO.thread_id to SchedulerTaskVM.threadId", async () => {
    vi.mocked(apiGet).mockResolvedValue([
      {
        ...baseTaskDTO,
        thread_id: "thread-bbbbbbbbbbbb",
      },
    ]);

    const tasks = await listTasks();

    expect(tasks[0]!.threadId).toBe("thread-bbbbbbbbbbbb");
    expect(tasks[0]!.inputText).toBe("总结今天");
    expect(tasks[0]!.agentName).toBe("default");
    expect(tasks[0]!.lifecycle).toBe("scheduled");
    expect(tasks[0]!.latestRunStatus).toBe("failed");
    expect(tasks[0]!.liveRuntimeStatus).toBe("idle");
  });

  it("maps the required empty thread_id exactly", async () => {
    vi.mocked(apiPost).mockResolvedValue(baseTaskDTO);

    const task = await createTask({
      name: "每天早报",
      agent_name: "default",
      input_text: "总结今天",
      schedule_type: "cron",
      cron_expr: "0 9 * * *",
      timezone: "Asia/Shanghai",
    });

    expect(task.threadId).toBe("");
  });

  it("maps CronRunDTO session_id and thread_id to SchedulerRunVM", async () => {
    const run = runFromDTO({
      run_id: "run-1",
      task_id: "task-1",
      task_name: "daily",
      session_id: "session-run-1",
      thread_id: "thread-1",
      scheduled_for: "2026-06-15T10:00:00+08:00",
      started_at: "2026-06-15T10:00:01+08:00",
      finished_at: null,
      status: "running",
      failure_reason: "needs_approval",
      final_message_excerpt: null,
      delivery_status: "pending",
      delivery_error: null,
    });

    expect(run.sessionId).toBe("session-run-1");
    expect(run.threadId).toBe("thread-1");
    expect(run.failureReason).toBe("needs_approval");
  });

  it("lists task runs with session and thread ids", async () => {
    vi.mocked(apiGet).mockResolvedValue([
      {
        run_id: "run-1",
        task_id: "task-1",
        task_name: "daily",
        session_id: "session-run-1",
        thread_id: "thread-1",
        scheduled_for: "2026-06-15T10:00:00+08:00",
        started_at: null,
        finished_at: null,
        status: "completed",
        failure_reason: null,
        final_message_excerpt: "done",
        delivery_status: "delivered",
        delivery_error: null,
      },
    ]);

    const runs = await listTaskRuns("task-1");

    expect(apiGet).toHaveBeenCalledWith("/api/cron/tasks/task-1/runs?limit=20");
    expect(runs[0]).toMatchObject({
      runId: "run-1",
      sessionId: "session-run-1",
      threadId: "thread-1",
      status: "completed",
      failureReason: null,
    });
  });

  it("loads cron run messages from the backend contract endpoint", async () => {
    const messages = [
      {
        frame_type: "text",
        role: "assistant",
        content: "done",
        id: "msg-1",
      },
    ];
    vi.mocked(apiGet).mockResolvedValue({ messages });

    const result = await loadRunMessages("task-1", "run-1");

    expect(apiGet).toHaveBeenCalledWith(
      "/api/cron/tasks/task-1/runs/run-1/messages",
    );
    expect(result.messages).toBe(messages);
  });
});
