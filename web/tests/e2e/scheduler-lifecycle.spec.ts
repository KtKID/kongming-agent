import { expect, test } from "@playwright/test";

const PASSWORD = "scheduler-e2e-pwd";

async function login(page: import("@playwright/test").Page): Promise<void> {
  await page.goto("/login");
  await page.getByRole("textbox", { name: "密码", exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/chat/);
}

async function openScheduler(page: import("@playwright/test").Page): Promise<void> {
  const entry = page.getByRole("button", { name: "定时任务" }).last();
  await expect(entry).toBeVisible();
  await entry.click();
  await expect(page.getByRole("heading", { name: "定时任务" })).toBeVisible();
}

test("scheduler lifecycle、手动执行、ticker、WS 与刷新投影保持一致", async ({ page }) => {
  const taskResponses: unknown[] = [];
  page.on("response", async (response) => {
    if (response.url().endsWith("/api/cron/tasks") && response.status() === 200) {
      taskResponses.push(await response.json());
    }
  });
  await login(page);
  await openScheduler(page);

  const recurring = page.getByText("Recurring failed").locator("..");
  await expect(recurring.getByText("已调度")).toBeVisible();
  await expect(recurring.getByText("失败")).toBeVisible();

  const oneShot = page.getByText("One-shot running").locator("..");
  await expect(oneShot.getByText("已耗尽")).toBeVisible();
  await expect(oneShot.getByText("已完成")).toBeVisible();

  await page.getByRole("button", { name: "已耗尽" }).click();
  await expect(page.getByText("One-shot running")).toBeVisible();
  await expect(page.getByText("Recurring failed")).toHaveCount(0);

  await page.reload();
  await openScheduler(page);
  await expect(page.getByText("Recurring failed")).toBeVisible();
  await expect(page.getByText("One-shot running")).toBeVisible();
  await expect(page.locator("span", { hasText: "已调度" })).toBeVisible();
  await expect(page.getByText("失败")).toBeVisible();
  await expect(page.locator("span", { hasText: "已耗尽" })).toBeVisible();
  await expect(page.getByText("已完成")).toBeVisible();
  expect(taskResponses.length).toBeGreaterThanOrEqual(2);
  const firstProjection = taskResponses[0] as Array<{
    task_id: string;
    lifecycle: string;
    latest_run_status: string | null;
    live_runtime_status: string;
  }>;
  expect(
    firstProjection.find((task) => task.task_id === "recurring-failed"),
  ).toMatchObject({
    lifecycle: "scheduled",
    latest_run_status: "failed",
    live_runtime_status: "idle",
  });
  expect(
    firstProjection.find((task) => task.task_id === "oneshot-running"),
  ).toMatchObject({
    lifecycle: "exhausted",
    latest_run_status: "completed",
    live_runtime_status: "idle",
  });

  const recurringRow = page
    .getByText("Recurring failed")
    .locator("xpath=ancestor::div[contains(@class,'rounded-lg')][1]");
  await recurringRow.click();
  const runNowResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/cron/tasks/recurring-failed/run_now") &&
      response.request().method() === "POST",
  );
  await recurringRow.getByTitle("立即执行").click();
  expect((await runNowResponse).status()).toBe(202);
  await expect(recurringRow.getByText("运行中")).toBeVisible();
  await expect(recurringRow.getByText("已完成")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText("scheduler e2e")).toBeVisible();

  await page.reload();
  await openScheduler(page);
  const refreshedRecurring = page
    .getByText("Recurring failed")
    .locator("xpath=ancestor::div[contains(@class,'rounded-lg')][1]");
  await expect(refreshedRecurring.getByText("已调度")).toBeVisible();
  await expect(refreshedRecurring.getByText("已完成")).toBeVisible();
});
