/**
 * task progress 单一 owner 浏览器薄 E2E。
 *
 * 关键流程：真实 Chat 页面读取 fake LLM 驱动的 REST 快照，依次展示 task-flow 的
 * start、next、完成和 workflow B 接管；A 的晚到事件保持 B 当前快照。
 * 关键函数：installFakeWebSocket 隔离实时通道，stubThreadBackend 提供动态 REST 状态，
 * fakeLlmAdvance 驱动可观察的状态阶段。
 */

import { expect, test, type Page } from "@playwright/test";
import type { ThreadTaskProgressSnapshot } from "@/protocol";

const VITE_DEV_URL = "http://127.0.0.1:5174";
const THREAD_ID = "thread-abcdef123456";

type WorkflowHistoryState = {
  thread_id: string;
  workflows: Array<{
    workflow_id: string;
    thread_id: string;
    mode: string;
    status: string;
    started_at: string | null;
    finished_at: string | null;
    desc: string | null;
    title: string;
    report_count: number;
    has_mode_panel: boolean;
    usage: {
      source: string;
      totals: Record<string, number>;
      provider_totals: Record<string, Record<string, number>>;
      records: unknown[];
      diagnostics: unknown[];
    };
    diagnostics: unknown[];
  }>;
};

type FakeLlmState = {
  snapshot: ThreadTaskProgressSnapshot;
  history: WorkflowHistoryState;
};

type BackendCalls = { progress: number };

/** 隔离页面 WebSocket，输入为页面，输出为可用的假连接。 */
async function installFakeWebSocket(page: Page): Promise<void> {
  await page.addInitScript(() => {
    class FakeWebSocket {
      static readonly CONNECTING = 0;
      static readonly OPEN = 1;
      static readonly CLOSING = 2;
      static readonly CLOSED = 3;

      readonly url: string;
      readyState = FakeWebSocket.CONNECTING;
      onopen: ((event: Event) => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;

      constructor(url: string) {
        this.url = url;
        window.setTimeout(() => {
          this.readyState = FakeWebSocket.OPEN;
          this.onopen?.(new Event("open"));
        }, 0);
      }

      send(data: string): void {
        const frame = JSON.parse(data) as { frame_type?: string; ts?: number };
        if (frame.frame_type === "ping") {
          this.onmessage?.(
            new MessageEvent("message", {
              data: JSON.stringify({ frame_type: "pong", ts: frame.ts }),
            }),
          );
        }
      }

      close(code = 1000, reason = "test-close"): void {
        this.readyState = FakeWebSocket.CLOSED;
        this.onclose?.(new CloseEvent("close", { code, reason }));
      }
    }

    Object.assign(FakeWebSocket, {
      CONNECTING: 0,
      OPEN: 1,
      CLOSING: 2,
      CLOSED: 3,
    });
    window.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
  });
}

/** 构造单项 task progress，输入为状态和顺序，输出为 v2 任务投影。 */
function progressTask(
  taskId: string,
  desc: string,
  status: ThreadTaskProgressSnapshot["tasks"][number]["status"],
  displayOrder: number,
): ThreadTaskProgressSnapshot["tasks"][number] {
  return {
    task_id: taskId,
    task_run_id: `00${displayOrder + 1}-${taskId}`,
    desc,
    depends_on: displayOrder === 0 ? [] : ["step-1"],
    status,
    error_message: null,
    display_order: displayOrder,
    updated_at_ms: displayOrder + 1,
  };
}

/** 构造真实 REST 形状快照，输入为 workflow 与任务，输出为五态 count 完整快照。 */
function progressSnapshot(
  workflowId: string,
  title: string,
  tasks: ThreadTaskProgressSnapshot["tasks"],
): ThreadTaskProgressSnapshot {
  return {
    schema_version: 2,
    session_id: THREAD_ID,
    workflow_id: workflowId,
    title,
    control_mode: "llm_steps",
    updated_at_ms: 100,
    tasks,
    counts: {
      pending: tasks.filter((task) => task.status === "pending").length,
      in_progress: tasks.filter((task) => task.status === "in_progress").length,
      completed: tasks.filter((task) => task.status === "completed").length,
      failed: tasks.filter((task) => task.status === "failed").length,
      cancelled: tasks.filter((task) => task.status === "cancelled").length,
      total: tasks.length,
    },
  };
}

/** 构造 workflow history 响应，输入为当前与历史 workflow，输出为 viewer DTO。 */
function workflowHistory(
  entries: Array<{ workflowId: string; title: string; status: string }>,
): WorkflowHistoryState {
  return {
    thread_id: THREAD_ID,
    workflows: entries.map((entry) => ({
      workflow_id: entry.workflowId,
      thread_id: THREAD_ID,
      mode: "task_flow",
      status: entry.status,
      started_at: null,
      finished_at: entry.status === "running" ? null : "2026-08-01T00:00:00Z",
      desc: null,
      title: entry.title,
      report_count: 0,
      has_mode_panel: false,
      usage: {
        source: "none",
        totals: {},
        provider_totals: {},
        records: [],
        diagnostics: [],
      },
      diagnostics: [],
    })),
  };
}

/** 安装 Chat 页全部必要 REST 替身，输入为动态 fake LLM 状态，输出为可交互页面后端。 */
async function stubThreadBackend(page: Page, state: FakeLlmState): Promise<BackendCalls> {
  const calls: BackendCalls = { progress: 0 };
  const thread = {
    id: THREAD_ID,
    name: "Task progress e2e",
    preset_id: "preset-a",
    backend_kind: "generic_chat",
    claude_thread_id: "",
    codex_thread_id: "",
    cwd: "/workspace/kongming-agent",
    created_at: 1,
    updated_at: 2,
    message_count: 0,
    is_pinned: false,
    is_archived: false,
    thread_kind: "chat",
  };
  await page.route("**/api/**", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
  await page.route("**/api/auth/me", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' }),
  );
  await page.route("**/api/config/client", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        host_environment: "browser",
        capabilities: { xspace_host: false, native_file_dialog: false },
        ws_heartbeat_interval_ms: 30000,
        ws_heartbeat_background_interval_ms: 60000,
        ws_heartbeat_timeout_ms: 10000,
        ws_heartbeat_max_missed: 3,
        dashboard_poll_interval_seconds: 5,
        timezone: "Asia/Shanghai",
      }),
    }),
  );
  await page.route("**/api/threads", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([thread]),
    }),
  );
  await page.route("**/api/presets", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          id: "preset-a",
          display_name: "Preset A",
          model: "fake-model",
          base_url_summary: "local",
          requires_api_key: false,
        },
      ]),
    }),
  );
  await page.route("**/api/model-providers/model-families", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
  await page.route(`**/api/threads/${THREAD_ID}/workspace-context`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        thread_id: THREAD_ID,
        workspace_root: "/workspace/kongming-agent",
        backend_kind: "generic_chat",
      }),
    }),
  );
  await page.route(`**/api/threads/${THREAD_ID}/permissions`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: 2,
        thread_id: THREAD_ID,
        revision: 0,
        allow: [],
        deny: [],
        updated_at: null,
        migration_summary: null,
      }),
    }),
  );
  await page.route(`**/api/threads/${THREAD_ID}/subagents`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ schema_version: 1, thread_id: THREAD_ID, subagents: [] }),
    }),
  );
  await page.route(`**/api/threads/${THREAD_ID}/agent-workflows`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(state.history),
    }),
  );
  await page.route(`**/api/threads/${THREAD_ID}/task-progress`, (route) =>
    {
      calls.progress += 1;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(state.snapshot),
      });
    },
  );
  return calls;
}

test("fake LLM task-flow phases keep the popover on the foreground workflow", async ({
  page,
}) => {
  const state: FakeLlmState = {
    snapshot: progressSnapshot("wf-a", "A 计划", [
      progressTask("step-1", "规划任务", "in_progress", 0),
      progressTask("step-2", "执行任务", "pending", 1),
    ]),
    history: workflowHistory([{ workflowId: "wf-a", title: "A 计划", status: "running" }]),
  };
  await installFakeWebSocket(page);
  const calls = await stubThreadBackend(page, state);
  await page.goto(`${VITE_DEV_URL}/chat/${THREAD_ID}`);

  const progressTrigger = page.getByTestId("web-shell-rail-item-thread-task-progress");
  await expect(progressTrigger).toBeVisible({ timeout: 10_000 });
  await expect(progressTrigger).toHaveAttribute("aria-expanded", "true");
  const popover = page.getByTestId("web-shell-rail-panel");
  await expect(popover.getByText("A 计划 · 0/2 已完成")).toBeVisible();
  await expect(popover.getByText("规划任务")).toBeVisible();
  await expect(popover.getByText("进行中")).toBeVisible();

  state.snapshot = progressSnapshot("wf-a", "A 计划", [
    progressTask("step-1", "规划任务", "completed", 0),
    progressTask("step-2", "执行任务", "in_progress", 1),
  ]);
  await expect(popover.getByText("A 计划 · 1/2 已完成")).toBeVisible({ timeout: 5_000 });
  await expect(popover.getByText("执行任务")).toBeVisible();

  state.snapshot = progressSnapshot("wf-a", "A 计划", [
    progressTask("step-1", "规划任务", "completed", 0),
    progressTask("step-2", "执行任务", "completed", 1),
  ]);
  await expect(popover.getByText("A 计划 · 2/2 已完成")).toBeVisible({ timeout: 5_000 });

  state.snapshot = progressSnapshot("wf-b", "B 计划", [
    progressTask("step-1", "复核发布", "in_progress", 0),
  ]);
  state.history = workflowHistory([
    { workflowId: "wf-a", title: "A 计划", status: "completed" },
    { workflowId: "wf-b", title: "B 计划", status: "running" },
  ]);
  await expect(popover.getByText("B 计划 · 0/1 已完成")).toBeVisible({ timeout: 5_000 });
  await expect(popover.getByText("A 计划", { exact: true })).toBeVisible({ timeout: 7_000 });
  await expect(popover.getByText("B 计划", { exact: true })).toHaveCount(0);

  const callsBeforeLateACommand = calls.progress;
  // fake LLM 的 A 晚到 next 被后端 owner 拒绝，当前 REST 快照继续保持 workflow B。
  await expect.poll(() => calls.progress, { timeout: 5_000 }).toBeGreaterThan(callsBeforeLateACommand);
  await expect(popover.getByText("B 计划 · 0/1 已完成")).toBeVisible();
  await expect(popover.getByText("复核发布")).toBeVisible();
});
