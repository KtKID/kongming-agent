/**
 * CreateSchedulerTaskDialog 三模式回归测试（v0.5.3）
 *
 * 覆盖：
 * - mode=create：基础流程已被 e2e 兜底，这里只验 preset 下拉渲染
 * - mode=edit：预填 + PATCH 调用（updateTask）
 * - mode=duplicate：name 加 "（副本）" 后缀 + POST 调用（createTask）
 * - preset 下拉：渲染 + 选中后提交 body 含 preset_id
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { CreateSchedulerTaskDialog } from "@/modules/scheduler/components/CreateSchedulerTaskDialog";
import { useSchedulerStore } from "@/modules/scheduler/store";
import { useThreadsStore } from "@/stores/threads";
import type { SchedulerTaskVM } from "@/modules/scheduler/types";

const createTask = vi.fn();
const updateTask = vi.fn();
const refreshTasks = vi.fn();
const selectTask = vi.fn();
const upsertTaskFromWS = vi.fn();

function makeTask(overrides: Partial<SchedulerTaskVM> = {}): SchedulerTaskVM {
  return {
    taskId: "task-abc",
    name: "原始任务",
    lifecycle: "scheduled",
    latestRunStatus: null,
    liveRuntimeStatus: "idle",
    triggerType: "cron",
    triggerExpr: "0 9 * * *",
    nextRunAt: null,
    lastRunAt: null,
    timezone: "Asia/Shanghai",
    presetId: "preset-a",
    threadId: "",
    createdBy: "user",
    inputText: "原始内容",
    agentName: "default",
    ...overrides,
  };
}

beforeEach(() => {
  createTask.mockReset();
  updateTask.mockReset();
  refreshTasks.mockReset();
  selectTask.mockReset();
  upsertTaskFromWS.mockReset();

  // 重置 scheduler store（只 patch 我们关心的 action）
  useSchedulerStore.setState({
    createTask,
    updateTask,
    refreshTasks,
    selectTask,
    upsertTaskFromWS,
  } as never);

  // 重置 threads store —— 提供两条 preset 供下拉渲染
  useThreadsStore.setState({
    threads: [],
    presets: [
      {
        id: "preset-a",
        display_name: "GPT-4o",
        model: "gpt-4o",
        base_url_summary: "api.openai.com",
        requires_api_key: true,
      },
      {
        id: "preset-b",
        display_name: "Claude 3.5",
        model: "claude-3-5-sonnet",
        base_url_summary: "api.anthropic.com",
        requires_api_key: true,
      },
    ],
    loading: false,
    createThread: vi.fn(),
    renameThread: vi.fn(),
    deleteThread: vi.fn(),
    fetchThreads: vi.fn(),
    fetchPresets: vi.fn(),
  } as never);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("CreateSchedulerTaskDialog · mode=create", () => {
  it("一次性模式默认填入当前日期和当前时分", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 5, 15, 17, 8, 0));

    render(
      <CreateSchedulerTaskDialog
        open={true}
        mode="create"
        onClose={vi.fn()}
      />,
    );

    expect((screen.getByPlaceholderText("年") as HTMLInputElement).value).toBe(
      "2026",
    );
    expect((screen.getByPlaceholderText("月") as HTMLInputElement).value).toBe(
      "6",
    );
    expect((screen.getByPlaceholderText("日") as HTMLInputElement).value).toBe(
      "15",
    );
    expect((screen.getByPlaceholderText("时") as HTMLInputElement).value).toBe(
      "17",
    );
    expect((screen.getByPlaceholderText("分") as HTMLInputElement).value).toBe(
      "08",
    );
  });

  it("一次性模式日期输满后聚焦小时输入框", async () => {
    render(
      <CreateSchedulerTaskDialog
        open={true}
        mode="create"
        onClose={vi.fn()}
      />,
    );

    const user = userEvent.setup();
    const dayInput = screen.getByPlaceholderText("日") as HTMLInputElement;
    const hourInput = screen.getByPlaceholderText("时") as HTMLInputElement;

    await user.clear(dayInput);
    await user.type(dayInput, "16");

    expect(hourInput).toHaveFocus();
  });

  it("渲染 preset 下拉，包含默认占位 + 全部 preset 选项", () => {
    render(
      <CreateSchedulerTaskDialog
        open={true}
        mode="create"
        onClose={vi.fn()}
      />,
    );
    const select = screen.getByLabelText("preset") as HTMLSelectElement;
    expect(select).toBeInTheDocument();
    // 默认占位（空 value）
    expect(select.querySelector('option[value=""]')).not.toBeNull();
    // 两条 preset 渲染
    expect(select.querySelector('option[value="preset-a"]')).not.toBeNull();
    expect(select.querySelector('option[value="preset-b"]')).not.toBeNull();
    // create 模式不显示 enabled toggle
    expect(screen.queryByLabelText("enabled")).toBeNull();
  });

  it("选中 preset 后创建，POST body 包含 preset_id", async () => {
    createTask.mockResolvedValue({ taskId: "task-new" });
    const onClose = vi.fn();
    render(
      <CreateSchedulerTaskDialog
        open={true}
        mode="create"
        onClose={onClose}
      />,
    );
    const user = userEvent.setup();
    await user.type(screen.getByPlaceholderText(/例：每天早报/), "测试任务");
    await user.selectOptions(screen.getByLabelText("preset"), "preset-b");
    // 切到 "每天" 模式 + 填时间
    await user.click(screen.getByRole("button", { name: "每天" }));
    const hourInput = screen.getByPlaceholderText("时") as HTMLInputElement;
    const minuteInput = screen.getByPlaceholderText("分") as HTMLInputElement;
    await user.clear(hourInput);
    await user.type(hourInput, "9");
    await user.clear(minuteInput);
    await user.type(minuteInput, "30");
    await user.type(
      screen.getByPlaceholderText(/描述任务内容/),
      "总结今天的日程",
    );
    await user.click(screen.getByRole("button", { name: /创建/ }));
    await waitFor(() => expect(createTask).toHaveBeenCalledTimes(1));
    const sent = createTask.mock.calls[0]![0];
    expect(sent.preset_id).toBe("preset-b");
    expect(sent.name).toBe("测试任务");
    expect(sent.schedule_type).toBe("cron");
    // h/m 始终 0-pad 到 2 位（buildSchedulePayload 的既有行为）
    expect(sent.cron_expr).toBe("30 09 * * *");
  });
});

describe("CreateSchedulerTaskDialog · mode=edit", () => {
  it("预填字段（name / preset / 时间 / enabled）并显示 enabled toggle + 按钮文案 = 保存", () => {
    const task = makeTask({
      name: "晨报",
      lifecycle: "disabled",
      triggerType: "cron",
      triggerExpr: "30 9 * * *",
      presetId: "preset-b",
      inputText: "总结今天",
    });
    render(
      <CreateSchedulerTaskDialog
        open={true}
        mode="edit"
        initialTask={task}
        onClose={vi.fn()}
      />,
    );
    // 标题 = 编辑
    expect(screen.getByText("编辑定时任务")).toBeInTheDocument();
    // name 预填
    expect(
      (screen.getByPlaceholderText(/例：每天早报/) as HTMLInputElement).value,
    ).toBe("晨报");
    // preset 预填到 preset-b
    expect((screen.getByLabelText("preset") as HTMLSelectElement).value).toBe(
      "preset-b",
    );
    // enabled toggle 显示且 unchecked
    const enabledCb = screen.getByLabelText("enabled") as HTMLInputElement;
    expect(enabledCb).toBeInTheDocument();
    expect(enabledCb.checked).toBe(false);
    // 时间预填 09:30
    expect(
      (screen.getByPlaceholderText("时") as HTMLInputElement).value,
    ).toBe("09");
    expect(
      (screen.getByPlaceholderText("分") as HTMLInputElement).value,
    ).toBe("30");
    // 按钮文案
    expect(screen.getByRole("button", { name: "保存" })).toBeInTheDocument();
  });

  it("提交时调 updateTask 并只发送变化字段", async () => {
    const task = makeTask({
      name: "晨报",
      lifecycle: "scheduled",
      triggerType: "cron",
      triggerExpr: "30 9 * * *",
      presetId: "preset-a",
      inputText: "原始内容",
    });
    updateTask.mockResolvedValue({ taskId: "task-abc" });
    const onClose = vi.fn();
    render(
      <CreateSchedulerTaskDialog
        open={true}
        mode="edit"
        initialTask={task}
        onClose={onClose}
      />,
    );
    const user = userEvent.setup();
    // 改名
    const nameInput = screen.getByPlaceholderText(
      /例：每天早报/,
    ) as HTMLInputElement;
    await user.clear(nameInput);
    await user.type(nameInput, "晨报-v2");
    // 关闭 enabled
    await user.click(screen.getByLabelText("enabled"));
    // 提交
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(updateTask).toHaveBeenCalledTimes(1));
    const [calledTaskId, calledBody] = updateTask.mock.calls[0]!;
    expect(calledTaskId).toBe("task-abc");
    expect(calledBody.name).toBe("晨报-v2");
    expect(calledBody.lifecycle).toBe("disabled");
    // h/m 0-pad 到 2 位
    expect(calledBody.schedule).toBe("30 09 * * *");
    // preset 未改 → 不在 body
    expect(calledBody.preset_id).toBeUndefined();
  });
});

describe("CreateSchedulerTaskDialog · mode=duplicate", () => {
  it("name 自动加 （副本）后缀，按钮文案 = 创建副本", () => {
    const task = makeTask({
      name: "晨报",
      triggerType: "cron",
      triggerExpr: "0 9 * * *",
    });
    render(
      <CreateSchedulerTaskDialog
        open={true}
        mode="duplicate"
        initialTask={task}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText("复制定时任务")).toBeInTheDocument();
    expect(
      (screen.getByPlaceholderText(/例：每天早报/) as HTMLInputElement).value,
    ).toBe("晨报（副本）");
    // duplicate 不显示 enabled toggle
    expect(screen.queryByLabelText("enabled")).toBeNull();
    expect(
      screen.getByRole("button", { name: "创建副本" }),
    ).toBeInTheDocument();
  });

  it("提交时调 createTask（POST 新建）而非 updateTask", async () => {
    const task = makeTask({
      name: "晨报",
      triggerType: "cron",
      triggerExpr: "0 9 * * *",
      presetId: "preset-a",
      inputText: "原始内容",
    });
    createTask.mockResolvedValue({ taskId: "task-new" });
    render(
      <CreateSchedulerTaskDialog
        open={true}
        mode="duplicate"
        initialTask={task}
        onClose={vi.fn()}
      />,
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "创建副本" }));
    await waitFor(() => expect(createTask).toHaveBeenCalledTimes(1));
    expect(updateTask).not.toHaveBeenCalled();
    const sent = createTask.mock.calls[0]![0];
    expect(sent.name).toBe("晨报（副本）");
    expect(sent.preset_id).toBe("preset-a");
    // h/m 0-pad 到 2 位（duplicate 从 initialTask "0 9 * * *" 解码后
    // form.hour="09" / form.minute="00" → 重编码为 "00 09 * * *"）
    expect(sent.cron_expr).toBe("00 09 * * *");
  });
});

// ---------------------------------------------------------------------------
// v0.5.4: preset 主动加载 + edit 模式 input_text/agent_name 预填
// ---------------------------------------------------------------------------

describe("CreateSchedulerTaskDialog · v0.5.4 preset 主动加载", () => {
  it("打开时若 presets 为空，主动调用 fetchPresets", () => {
    const fetchPresets = vi.fn().mockResolvedValue(undefined);
    useThreadsStore.setState({
      threads: [],
      presets: [],
      loading: false,
      createThread: vi.fn(),
      renameThread: vi.fn(),
      deleteThread: vi.fn(),
      fetchThreads: vi.fn(),
      fetchPresets,
    } as never);
    render(
      <CreateSchedulerTaskDialog
        open={true}
        mode="create"
        onClose={vi.fn()}
      />,
    );
    expect(fetchPresets).toHaveBeenCalledTimes(1);
  });

  it("打开时若 presets 已加载，不再调用 fetchPresets", () => {
    const fetchPresets = vi.fn().mockResolvedValue(undefined);
    useThreadsStore.setState({
      threads: [],
      presets: [
        {
          id: "preset-x",
          display_name: "GPT-4o",
          model: "gpt-4o",
          base_url_summary: "api.openai.com",
          requires_api_key: true,
        },
      ],
      loading: false,
      createThread: vi.fn(),
      renameThread: vi.fn(),
      deleteThread: vi.fn(),
      fetchThreads: vi.fn(),
      fetchPresets,
    } as never);
    render(
      <CreateSchedulerTaskDialog
        open={true}
        mode="create"
        onClose={vi.fn()}
      />,
    );
    expect(fetchPresets).not.toHaveBeenCalled();
  });
});

describe("CreateSchedulerTaskDialog · v0.5.4 edit 预填 input_text", () => {
  it("edit 模式从 initialTask.inputText 预填 textarea", () => {
    const task = makeTask({
      name: "晨报",
      triggerType: "cron",
      triggerExpr: "0 9 * * *",
      inputText: "扫描司天",
      agentName: "default",
    });
    render(
      <CreateSchedulerTaskDialog
        open={true}
        mode="edit"
        initialTask={task}
        onClose={vi.fn()}
      />,
    );
    const textarea = screen.getByPlaceholderText(
      /描述任务内容/,
    ) as HTMLTextAreaElement;
    expect(textarea.value).toBe("扫描司天");
  });
});
