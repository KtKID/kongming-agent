import { expect, test } from "@playwright/test";

/**
 * Claude 频道 keep-alive e2e（network-layer v0.1 步骤 9.x）。
 *
 * 治痛点：Claude 频道 `/ws/claude-code` 空闲 ~1 分钟沉默断开。
 * 这套 spec 覆盖 4 个真实浏览器场景：
 *
 * 1. 空闲 60s 后连接仍活（顶栏 Claude 球不变 closed/failed）
 * 2. 后台 tab 切回前台立即探测（visibilitychange → probe）
 * 3. 顶栏 latency 显示数字（不再恒为 null / "?"）
 * 4. 拔网线 → reconnecting → 恢复（page.context().setOffline）
 *
 * **运行需求**：本机 web server 在 `http://localhost:8210`（或 env `E2E_BASE_URL`）
 * 跑起来 + 至少 1 条 backend_kind=claude_code 的 thread。
 *
 * 联调期单跑：`npx playwright test web/tests/e2e/claude-keepalive.spec.ts`
 */

const PASSWORD = process.env.E2E_PASSWORD ?? "test-pwd";

async function login(page: import("@playwright/test").Page) {
  await page.goto("/login");
  await page.getByLabel("密码").fill(PASSWORD);
  await page.getByRole("button", { name: /登录/ }).click();
  await expect(page).toHaveURL(/\/chat/);
}

/**
 * 进入第一个 claude_code thread（左栏第一项）。
 * 如果没有 claude_code thread，跳过用例（联调环境补 fixture）。
 */
async function openFirstClaudeThread(page: import("@playwright/test").Page) {
  // 假设左栏有"Claude"分组，点进第一条
  const claudeTab = page.getByRole("tab", { name: /Claude/ });
  if (await claudeTab.count()) {
    await claudeTab.click();
  }
  const firstItem = page.locator("aside a").first();
  await firstItem.click();
  await expect(page).toHaveURL(/\/chat\/thread-/);
}

test.describe("claude keep-alive (network-layer v0.1)", () => {
  test("场景 1：60s 空闲后 Claude 频道连接仍活", async ({ page }) => {
    await login(page);
    await openFirstClaudeThread(page);

    // 等连接 open
    const latencyBadge = page.getByTestId("claude-ws-indicator");
    await expect(latencyBadge).toBeVisible({ timeout: 10_000 });

    // 等 65s（超出 nginx 默认 60s idle，但低于本端 100s 判死上限）
    // playwright 默认 test timeout 30s，需要手动放宽
    test.setTimeout(120_000);
    await page.waitForTimeout(65_000);

    // 连接应仍 open；顶栏 Claude 球不应是 closed / failed
    const cls = await latencyBadge.getAttribute("data-state");
    expect(cls).toBe("open");
  });

  test("场景 2：后台 tab 切回前台立即探测", async ({ page, context }) => {
    await login(page);
    await openFirstClaudeThread(page);

    const latencyBadge = page.getByTestId("claude-ws-indicator");
    await expect(latencyBadge).toBeVisible({ timeout: 10_000 });

    // 新开第二个 tab 抢焦点 → 原 tab 进 hidden 状态
    const otherTab = await context.newPage();
    await otherTab.goto("about:blank");
    // 用 evaluate 模拟 visibilitychange（playwright 不直接支持切前后台）
    await page.evaluate(() => {
      Object.defineProperty(document, "hidden", {
        value: true,
        configurable: true,
      });
      Object.defineProperty(document, "visibilityState", {
        value: "hidden",
        configurable: true,
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });

    // 等一会儿再切回
    await page.waitForTimeout(5_000);

    await page.evaluate(() => {
      Object.defineProperty(document, "hidden", {
        value: false,
        configurable: true,
      });
      Object.defineProperty(document, "visibilityState", {
        value: "visible",
        configurable: true,
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });

    // probe 应立即让 latency 数字更新（30s 内有一次新的 pong）
    // 通过 latency 文本变化间接验证
    const initialText = await latencyBadge.textContent();
    await page.waitForFunction(
      (init) => {
        const el = document.querySelector('[data-testid="claude-ws-indicator"]');
        return el && el.textContent && el.textContent !== init;
      },
      initialText,
      { timeout: 15_000 },
    );

    await otherTab.close();
  });

  test("场景 3：顶栏 latency 显示数字而非 null", async ({ page }) => {
    await login(page);
    await openFirstClaudeThread(page);

    const latencyBadge = page.getByTestId("claude-ws-indicator");
    await expect(latencyBadge).toBeVisible({ timeout: 10_000 });

    // 等首次 ping/pong 完成（intervalMs=30s + RTT）
    test.setTimeout(60_000);
    await page.waitForTimeout(35_000);

    // latency 数字应是正整数 ms
    const text = await latencyBadge.textContent();
    // text 形如 "12 ms" / "120 ms"；不应是 "?" / 空 / "null"
    expect(text).toMatch(/\d+\s*ms/);
  });

  test("场景 4：拔网线 → reconnecting → 恢复", async ({ page, context }) => {
    await login(page);
    await openFirstClaudeThread(page);

    const latencyBadge = page.getByTestId("claude-ws-indicator");
    await expect(latencyBadge).toBeVisible({ timeout: 10_000 });
    await expect(latencyBadge).toHaveAttribute("data-state", "open");

    // 拔网线
    await context.setOffline(true);

    // 等 maxMissed × intervalMs + timeoutMs ≈ 100s 后判死（视配置）
    // 联调期可以把心跳调小加速；这里保守等 120s
    test.setTimeout(180_000);
    await page.waitForFunction(
      () => {
        const el = document.querySelector('[data-testid="claude-ws-indicator"]');
        return el?.getAttribute("data-state") === "reconnecting";
      },
      undefined,
      { timeout: 110_000 },
    );

    // 网线恢复
    await context.setOffline(false);

    // 应能重连回 open
    await page.waitForFunction(
      () => {
        const el = document.querySelector('[data-testid="claude-ws-indicator"]');
        return el?.getAttribute("data-state") === "open";
      },
      undefined,
      { timeout: 60_000 },
    );
  });
});
