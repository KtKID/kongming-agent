/**
 * 真实 LLM 通用 thread 完整分叉 E2E。
 *
 * 关键流程：
 * 1. 登录真实 Kongming Web；
 * 2. 从空白页首发消息创建 generic thread，并等待真实模型回复；
 * 3. 在 assistant 最终回复气泡下方执行回复级分叉；
 * 4. 在目标 thread 发送第二条消息，要求模型引用源回复标记；
 * 5. 输出源/目标 thread ID，供 FileSession、trace 与 server log 交叉核验。
 */
import { expect, test, type Page } from "@playwright/test";

const PASSWORD = process.env.KONGMING_WEB_PASSWORD ?? "";
const RUN_ENABLED = process.env.RUN_REAL_LLM_E2E === "1";
const RUN_MARKER =
  process.env.REAL_LLM_E2E_MARKER ?? `REAL_LLM_E2E_${Date.now()}`;

type ThreadPayload = {
  id: string;
  name: string;
  preset_id: string;
  forked_from_id: string | null;
  message_count: number;
};

/** 登录真实 Web；密码只从当前进程环境读取。 */
async function login(page: Page): Promise<void> {
  await page.goto("/login");
  if (!page.url().endsWith("/login")) return;
  await page.getByRole("textbox", { name: "密码", exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/chat(?:\/.*)?$/);
}

test.skip(!RUN_ENABLED, "设置 RUN_REAL_LLM_E2E=1 后才调用真实模型");
test.skip(!PASSWORD, "KONGMING_WEB_PASSWORD 未提供");

test("真实 generic thread 首发、assistant 回复分叉与目标续聊", async ({
  page,
}, testInfo) => {
  const sourceReplyMarker = `${RUN_MARKER}_SOURCE_OK`;
  const forkReplyMarker = `${RUN_MARKER}_FORK_OK`;
  const forkExpectedReply =
    `${forkReplyMarker} SEES_SOURCE=${sourceReplyMarker}`;
  const sourcePrompt = [
    `这是通用 thread 的真实 LLM E2E，运行标记 ${RUN_MARKER}。`,
    `请直接回复唯一字符串：${sourceReplyMarker}`,
  ].join("\n");

  await login(page);
  await expect(page.getByRole("button", { name: "新建对话" })).toBeVisible();
  await page.getByRole("button", { name: "新建对话" }).click();
  const composer = page.getByRole("textbox", { name: "消息输入" });
  await expect(composer).toBeEnabled();
  await composer.fill(sourcePrompt);

  const createResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith("/api/threads/generic/first-message"),
  );
  await page.getByRole("button", { name: "发送", exact: true }).click();
  const createResponse = await createResponsePromise;
  expect(createResponse.status()).toBe(200);
  const createPayload = (await createResponse.json()) as {
    thread: ThreadPayload;
  };
  const source = createPayload.thread;
  await expect(page).toHaveURL(new RegExp(`/chat/${source.id}$`));
  await expect(page.getByText(sourceReplyMarker, { exact: true })).toBeVisible({
    timeout: 240_000,
  });
  await page.screenshot({
    path: testInfo.outputPath("real-llm-source-complete.png"),
    fullPage: true,
  });

  const sourceReply = page.getByText(sourceReplyMarker, { exact: true });
  await sourceReply.hover();
  const forkButton = page.getByRole("button", { name: "从此回复分叉" });
  await expect(forkButton).toBeEnabled({ timeout: 60_000 });
  const forkResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith(`/api/threads/${source.id}/fork`),
  );
  await forkButton.click();
  const forkResponse = await forkResponsePromise;
  expect(forkResponse.status()).toBe(201);
  expect(forkResponse.request().postDataJSON()).toEqual({ history_index: 1 });
  const forked = (await forkResponse.json()) as ThreadPayload;
  expect(forked.forked_from_id).toBe(source.id);
  await expect(page).toHaveURL(new RegExp(`/chat/${forked.id}$`));
  await expect(page.getByText(sourceReplyMarker, { exact: true })).toBeVisible();

  const forkPrompt = [
    `这是分支 thread 的真实 LLM 续聊，运行标记 ${RUN_MARKER}。`,
    `请确认上文包含 ${sourceReplyMarker}，然后直接回复：`,
    forkExpectedReply,
  ].join("\n");
  await expect(composer).toBeEnabled({ timeout: 30_000 });
  await composer.fill(forkPrompt);
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByText(forkExpectedReply, { exact: true })).toBeVisible({
    timeout: 240_000,
  });
  await page.screenshot({
    path: testInfo.outputPath("real-llm-fork-complete.png"),
    fullPage: true,
  });

  console.log(
    JSON.stringify({
      marker: RUN_MARKER,
      sourceThreadId: source.id,
      sourcePresetId: source.preset_id,
      sourceReplyMarker,
      forkThreadId: forked.id,
      forkedFromId: forked.forked_from_id,
      forkReplyMarker,
      finalUrl: page.url(),
    }),
  );
});
