import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";

const PASSWORD = "task-progress-e2e-pwd";
const THREAD_ID = "thread-dddddddddddd";
const CONTROL_ORIGIN = "http://127.0.0.1:8080";
const PROGRESS_PATH_LOCATOR = "/tmp/kongming-task-progress-single-owner-e2e-path.json";

test.use({
  extraHTTPHeaders: { "X-Requested-With": "XMLHttpRequest" },
});

async function loginAndOpenThread(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByRole("textbox", { name: "密码", exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/chat/);
  await page.goto(`/chat/${THREAD_ID}`);
  await expect(page.getByLabel("消息输入")).toBeVisible();
}

test("fake LLM 指令经真实 Manager 与 FastAPI 回显任务进度，B 接管拒绝 A 晚到命令", async ({
  page,
}) => {
  await loginAndOpenThread(page);
  const panel = page.getByTestId("web-shell-rail-panel");

  const initialResponse = await page.request.get(
    `/api/threads/${THREAD_ID}/task-progress`,
  );
  expect(initialResponse.ok()).toBeTruthy();
  expect(await initialResponse.json()).toMatchObject({
    workflow_id: "wf-task-progress-a",
    tasks: [
      { task_id: "a-step-1", status: "pending" },
      { task_id: "a-step-2", status: "pending" },
    ],
    counts: { pending: 2, in_progress: 0, completed: 0, failed: 0, cancelled: 0 },
  });
  await expect(
    panel.getByRole("listitem", { name: "未完成：规划任务", exact: true }),
  ).toBeVisible();

  const started = await page.request.post(
    `${CONTROL_ORIGIN}/__e2e/task-progress/start-a`,
  );
  expect(started.ok()).toBeTruthy();
  expect(await started.json()).toMatchObject({
    workflow_id: "wf-task-progress-a",
    tasks: [
      { task_id: "a-step-1", status: "in_progress" },
      { task_id: "a-step-2", status: "pending" },
    ],
    counts: { pending: 1, in_progress: 1, completed: 0, failed: 0, cancelled: 0 },
  });
  await expect(panel.getByText("A 计划 · 0/2 已完成", { exact: true })).toBeVisible({
    timeout: 7_000,
  });
  await expect(
    panel.getByRole("listitem", { name: "进行中：规划任务", exact: true }),
  ).toBeVisible();

  const advanced = await page.request.post(
    `${CONTROL_ORIGIN}/__e2e/task-progress/next-a`,
  );
  expect(advanced.ok()).toBeTruthy();
  expect(await advanced.json()).toMatchObject({
    workflow_id: "wf-task-progress-a",
    tasks: [
      { task_id: "a-step-1", status: "completed" },
      { task_id: "a-step-2", status: "in_progress" },
    ],
    counts: { pending: 0, in_progress: 1, completed: 1, failed: 0, cancelled: 0 },
  });
  await expect(panel.getByText("A 计划 · 1/2 已完成", { exact: true })).toBeVisible({
    timeout: 7_000,
  });
  await expect(
    panel.getByRole("listitem", { name: "已完成：规划任务", exact: true }),
  ).toBeVisible();
  await expect(
    panel.getByRole("listitem", { name: "进行中：执行任务", exact: true }),
  ).toBeVisible();

  const takeover = await page.request.post(
    `${CONTROL_ORIGIN}/__e2e/task-progress/open-b-and-reject-late-a`,
  );
  expect(takeover.ok()).toBeTruthy();
  expect(await takeover.json()).toMatchObject({
    a_before_takeover: {
      workflow_id: "wf-task-progress-a",
      tasks: [
        { task_id: "a-step-1", status: "completed" },
        { task_id: "a-step-2", status: "in_progress" },
      ],
    },
    late_a_action: "next",
    late_a_rejected: true,
    snapshot: {
      workflow_id: "wf-task-progress-b",
      tasks: [
        { task_id: "b-step-1", status: "in_progress" },
        { task_id: "b-step-2", status: "pending" },
      ],
    },
  });
  await expect(panel.getByText("B 计划 · 0/2 已完成", { exact: true })).toBeVisible({
    timeout: 7_000,
  });
  await expect(panel.getByText("复核发布", { exact: true })).toBeVisible();

  const advancedB = await page.request.post(
    `${CONTROL_ORIGIN}/__e2e/task-progress/next-b`,
  );
  expect(advancedB.ok()).toBeTruthy();
  expect(await advancedB.json()).toMatchObject({
    workflow_id: "wf-task-progress-b",
    tasks: [
      { task_id: "b-step-1", status: "completed" },
      { task_id: "b-step-2", status: "in_progress" },
    ],
  });
  await expect(panel.getByText("B 计划 · 1/2 已完成", { exact: true })).toBeVisible({
    timeout: 7_000,
  });

  const completedB = await page.request.post(
    `${CONTROL_ORIGIN}/__e2e/task-progress/complete-b`,
  );
  expect(completedB.ok()).toBeTruthy();
  expect(await completedB.json()).toMatchObject({
    workflow_id: "wf-task-progress-b",
    counts: { pending: 0, in_progress: 0, completed: 2, failed: 0, cancelled: 0 },
  });
  await expect(panel.getByText("B 计划 · 2/2 已完成", { exact: true })).toBeVisible({
    timeout: 7_000,
  });

  const productSnapshotResponse = await page.request.get(
    `/api/threads/${THREAD_ID}/task-progress`,
  );
  expect(productSnapshotResponse.ok()).toBeTruthy();
  const productSnapshot = await productSnapshotResponse.json();
  const locator = JSON.parse(
    await readFile(PROGRESS_PATH_LOCATOR, "utf-8"),
  ) as { progress_path: string };
  const persistedSnapshot = JSON.parse(
    await readFile(locator.progress_path, "utf-8"),
  );

  expect(productSnapshot).toEqual(persistedSnapshot);
  expect(productSnapshot).toMatchObject({
    workflow_id: "wf-task-progress-b",
    counts: { pending: 0, in_progress: 0, completed: 2, failed: 0, cancelled: 0 },
  });
  expect(productSnapshot.tasks).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ task_id: "b-step-1", status: "completed" }),
      expect.objectContaining({ task_id: "b-step-2", status: "completed" }),
    ]),
  );
});
