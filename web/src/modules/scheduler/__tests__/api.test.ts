import { describe, expect, it, vi, beforeEach } from "vitest";

import { apiGet, apiPost } from "@/lib/api";
import {
  createTask,
  listTasks,
} from "@/modules/scheduler/api";

vi.mock("@/lib/api", () => ({
  apiDelete: vi.fn(),
  apiGet: vi.fn(),
  apiPatch: vi.fn(),
  apiPost: vi.fn(),
}));

const baseTaskDTO = {
  task_id: "task-1",
  name: "每天早报",
  enabled: true,
  state: "scheduled",
  trigger_type: "cron",
  trigger_expr: "0 9 * * *",
  next_run_at: null,
  last_run_at: null,
  preset_id: "preset-a",
  created_by: "user",
  input_text: "总结今天",
  agent_name: "default",
};

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
  });

  it("uses an empty threadId for legacy task payloads", async () => {
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
});
