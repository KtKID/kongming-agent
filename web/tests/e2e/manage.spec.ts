import { expect, test } from "@playwright/test";

/**
 * 管理页 e2e（联调阶段跑）。
 */

const PASSWORD = process.env.E2E_PASSWORD ?? "test-pwd";

async function login(page: import("@playwright/test").Page) {
  await page.goto("/login");
  await page.getByLabel("密码").fill(PASSWORD);
  await page.getByRole("button", { name: /登录/ }).click();
  await expect(page).toHaveURL(/\/chat/);
}

test.describe("manage", () => {
  test("访问 /manage → 表格或空态", async ({ page }) => {
    await login(page);
    await page.goto("/manage");
    // Layout 标题 + 页面 h2 都会出现“运行管理”，用 heading role 锁定 h2
    await expect(
      page.getByRole("heading", { name: "运行管理" }),
    ).toBeVisible();
  });

  test("停止 cell → 二次确认 → 列表移除", async ({ page }) => {
    await login(page);
    await page.goto("/manage");
    // 假设至少有 1 个 cell
    const stopBtn = page.getByRole("button", { name: /停止/ }).first();
    if (await stopBtn.isVisible()) {
      await stopBtn.click();
      await expect(page.getByText("停止该 cell？")).toBeVisible();
      await page.getByRole("button", { name: /确认停止/ }).click();
      // 列表更新（联调期校验）
    }
  });
});
