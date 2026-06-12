import { expect, test } from "@playwright/test";

/**
 * 登录 / 退出 flow（联调阶段跑）。
 *
 * 假设：
 * - E2E_PASSWORD 环境变量提供测试密码（默认 'test-pwd'）
 * - 后端 web-app-shell 已就绪并在 baseURL（默认 :8080）提供 SPA + REST
 */

const PASSWORD = process.env.E2E_PASSWORD ?? "test-pwd";

test.describe("auth", () => {
  test("登录成功 → 跳 /chat", async ({ page }) => {
    await page.goto("/login");
    await page.getByLabel("密码").fill(PASSWORD);
    await page.getByRole("button", { name: /登录/ }).click();
    await expect(page).toHaveURL(/\/chat/);
  });

  test("密码错误 → 显示提示", async ({ page }) => {
    await page.goto("/login");
    await page.getByLabel("密码").fill("definitely-wrong-password");
    await page.getByRole("button", { name: /登录/ }).click();
    await expect(page.getByRole("alert")).toContainText("密码错误");
    await expect(page).toHaveURL(/\/login/);
  });

  test("退出 → 回 /login", async ({ page }) => {
    // 先登录
    await page.goto("/login");
    await page.getByLabel("密码").fill(PASSWORD);
    await page.getByRole("button", { name: /登录/ }).click();
    await expect(page).toHaveURL(/\/chat/);
    // 点退出
    await page.getByLabel("退出登录").click();
    await expect(page).toHaveURL(/\/login/);
  });

  test("未登录访问 /chat → redirect /login", async ({ page, context }) => {
    await context.clearCookies();
    await page.goto("/chat");
    await expect(page).toHaveURL(/\/login/);
  });
});
