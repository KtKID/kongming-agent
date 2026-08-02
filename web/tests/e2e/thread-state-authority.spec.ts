import {
  expect,
  test,
  type Browser,
  type BrowserContext,
  type Page,
} from "@playwright/test";

const PASSWORD = "thread-state-e2e-pwd";
const THREAD_ID = "thread-cccccccccccc";
const CONTROL_ORIGIN = "http://127.0.0.1:8080";

async function loginAndOpenThread(
  browser: Browser,
  minimumConnections = 1,
): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({
    extraHTTPHeaders: {
      "X-Requested-With": "XMLHttpRequest",
    },
  });
  const page = await context.newPage();
  await page.goto("/login");
  await page.getByRole("textbox", { name: "密码", exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/chat/);
  await page.goto(`/chat/${THREAD_ID}`);
  await expect(page.getByLabel("消息输入")).toBeVisible();
  await expect
    .poll(async () => {
      const response = await page.request.get(
        `${CONTROL_ORIGIN}/__e2e/thread-state`,
      );
      const state = (await response.json()) as { connections: number };
      return state.connections;
    })
    .toBeGreaterThanOrEqual(minimumConnections);
  return { context, page };
}

test("后打开与重连浏览器通过 snapshot 收敛，旧 run 终态无法清除新 run", async ({
  browser,
}) => {
  const a = await loginAndOpenThread(browser);
  const started = await a.page.request.post(
    `${CONTROL_ORIGIN}/__e2e/thread-state/start`,
  );
  expect(started.ok()).toBeTruthy();
  await expect(a.page.getByTestId("composer-stop")).toBeVisible();

  const b = await loginAndOpenThread(browser, 2);
  await expect(b.page.getByTestId("composer-stop")).toBeVisible();

  const replaced = await a.page.request.post(
    `${CONTROL_ORIGIN}/__e2e/thread-state/replace`,
  );
  expect(replaced.ok()).toBeTruthy();
  expect(await replaced.json()).toMatchObject({ stale_accepted: false });
  await expect(a.page.getByTestId("composer-stop")).toBeVisible();
  await expect(b.page.getByTestId("composer-stop")).toBeVisible();

  const active = await (
    await a.page.request.get(`${CONTROL_ORIGIN}/__e2e/thread-state`)
  ).json();
  expect(active.active[THREAD_ID]).toMatchObject({
    runId: "run-2",
    runGeneration: 2,
    phase: "responding",
  });

  const completed = await a.page.request.post(
    `${CONTROL_ORIGIN}/__e2e/thread-state/complete`,
  );
  expect(completed.ok()).toBeTruthy();
  await expect(a.page.getByTestId("composer-stop")).toHaveCount(0);
  await expect(b.page.getByTestId("composer-stop")).toHaveCount(0);
  await b.context.close();

  const reconnected = await loginAndOpenThread(browser, 2);
  await expect(reconnected.page.getByTestId("composer-stop")).toHaveCount(0);
  const terminal = await (
    await reconnected.page.request.get(`${CONTROL_ORIGIN}/__e2e/thread-state`)
  ).json();
  expect(terminal.active).toEqual({});

  await a.context.close();
  await reconnected.context.close();
});

test("两个浏览器同时审批只接受一次，authoritative remove 清除两端卡片", async ({
  browser,
}) => {
  const a = await loginAndOpenThread(browser);
  const b = await loginAndOpenThread(browser, 2);

  const added = await a.page.request.post(
    `${CONTROL_ORIGIN}/__e2e/approval/add`,
  );
  expect(added.ok()).toBeTruthy();
  const cardA = a.page.getByTestId("approval-inbox-card");
  const cardB = b.page.getByTestId("approval-inbox-card");
  await expect(cardA).toBeVisible();
  await expect(cardB).toBeVisible();
  // 两端都完成 240ms 入场动画后再同时触发，避免 Playwright 的稳定性等待
  // 与首个 authoritative remove 竞争，测试目标只保留服务端双提交仲裁。
  await Promise.all([
    a.page.waitForTimeout(300),
    b.page.waitForTimeout(300),
  ]);

  await Promise.all([
    cardA.getByTestId("approval-inbox-btn-allow-once").dispatchEvent("click"),
    cardB.getByTestId("approval-inbox-btn-reject").dispatchEvent("click"),
  ]);
  await expect(cardA).toHaveCount(0);
  await expect(cardB).toHaveCount(0);

  await expect
    .poll(async () => {
      const response = await a.page.request.get(
        `${CONTROL_ORIGIN}/__e2e/approval`,
      );
      return response.json();
    })
    .toMatchObject({
      pending: 0,
      inbox_pending: 0,
      resolution_attempts: 2,
      accepted_resolutions: 1,
    });
  const finalState = await (
    await a.page.request.get(`${CONTROL_ORIGIN}/__e2e/approval`)
  ).json();
  expect(["approved", "rejected"]).toContain(finalState.outcome);
  expect(finalState.tool_continuations).toBe(
    finalState.outcome === "approved" ? 1 : 0,
  );

  await a.context.close();
  await b.context.close();
});
