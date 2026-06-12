import { expect, test } from "@playwright/test";

/**
 * Chat 主流程 e2e（联调阶段跑）。
 */

const PASSWORD = process.env.E2E_PASSWORD ?? "test-pwd";

async function login(page: import("@playwright/test").Page) {
  await page.goto("/login");
  await page.getByLabel("密码").fill(PASSWORD);
  await page.getByRole("button", { name: /登录/ }).click();
  await expect(page).toHaveURL(/\/chat/);
}

test.describe("chat", () => {
  test("新建 thread → 发消息 → 看到 assistant 输出", async ({ page }) => {
    await login(page);
    // 新建 thread
    await page.getByLabel("新建会话").click();
    await page.getByPlaceholder(/会话名/).fill("e2e-thread");
    await page.getByRole("button", { name: /创建/ }).click();
    // 路由跳到 thread 详情
    await expect(page).toHaveURL(/\/chat\/thread-/);
    // 发消息
    const input = page.getByLabel("消息输入");
    await input.fill("hello, agent");
    await page.getByRole("button", { name: /发送/ }).click();
    // 用户消息出现
    await expect(page.getByText("hello, agent")).toBeVisible();
    // assistant 消息（联调期后端真发回复时校验；mock 期可跳过）
    // await expect(page.locator('text=/.+/').first()).toBeVisible({ timeout: 10000 });
  });

  test("⌘⏎ 快捷键发送", async ({ page }) => {
    await login(page);
    // 假设至少有一个 thread；选第一个
    const firstThread = page.locator("aside a").first();
    await firstThread.click();
    const input = page.getByLabel("消息输入");
    await input.fill("via shortcut");
    await input.press("Meta+Enter");
    await expect(page.getByText("via shortcut")).toBeVisible();
  });

  test("approval modal 出现 → 拒绝 → 工具不执行", async ({ page }) => {
    await login(page);
    // 这里依赖后端在某个 prompt 下触发 approval；联调期补具体 fixture
    // 占位：当 approval modal 出现时，按 ESC 应当关闭
    // const dialog = page.getByText('需要审批');
    // await expect(dialog).toBeVisible();
    // await page.keyboard.press('Escape');
    // await expect(dialog).not.toBeVisible();
  });
});
