import { expect, test } from "@playwright/test";

const PASSWORD = "fork-e2e-pwd";
const SOURCE_THREAD_ID = "thread-aaaaaaaaaaaa";
const SOURCE_THREAD_NAME = "Fork E2E Source";
const ASSET_ID = "b".repeat(32);

async function login(page: import("@playwright/test").Page): Promise<void> {
  await page.goto("/login");
  await page.getByRole("textbox", { name: "密码", exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/chat/);
}

test("UI fork 经 REST 提交并从新 FileSession 恢复完整历史与附件", async ({
  page,
}) => {
  const forkRequests: string[] = [];
  const forkRequestBodies: unknown[] = [];
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      request.url().endsWith(`/api/threads/${SOURCE_THREAD_ID}/fork`)
    ) {
      forkRequests.push(request.url());
      forkRequestBodies.push(request.postDataJSON());
    }
  });
  await login(page);
  await page.getByTitle(SOURCE_THREAD_NAME).click();
  const finalReply = page.getByText("fork browser final marker");
  await expect(finalReply).toBeVisible();
  await finalReply.hover();

  const forkResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith(`/api/threads/${SOURCE_THREAD_ID}/fork`),
  );
  await page.getByRole("button", { name: "从此回复分叉" }).click();
  const forkResponse = await forkResponsePromise;
  expect(forkResponse.status()).toBe(201);
  const forked = (await forkResponse.json()) as {
    id: string;
    forked_from_id: string | null;
    forked_from_history_index: number | null;
    message_count: number;
  };
  expect(forked.forked_from_id).toBe(SOURCE_THREAD_ID);
  expect(forked.forked_from_history_index).toBe(3);
  expect(forked.message_count).toBe(4);
  await expect(page).toHaveURL(new RegExp(`/chat/${forked.id}$`));

  await expect(page.getByText("fork browser source marker")).toBeVisible();
  await expect(page.getByText("fork browser final marker")).toBeVisible();
  await page.getByRole("button", { name: "read_file" }).click();
  await expect(page.getByText("fork e2e tool output")).toBeVisible();
  expect(forkRequests).toHaveLength(1);
  expect(forkRequestBodies).toEqual([{ history_index: 3 }]);
  const attachment = page.locator(
    `[data-testid="message-attachment-thumb"][data-asset-id="${ASSET_ID}"] img`,
  );
  await expect(attachment).toBeVisible();
  await expect(attachment).not.toHaveAttribute("data-error", "true");

  await page.reload();
  await expect(page.getByText("fork browser source marker")).toBeVisible();
  await expect(page.getByText("fork browser final marker")).toBeVisible();
  await expect(attachment).not.toHaveAttribute("data-error", "true");

  const lineageLink = page.getByRole("link", { name: "续接自任务" });
  await expect(page.getByTestId("fork-lineage-navigation")).toHaveAttribute(
    "data-history-index",
    "3",
  );
  await expect(lineageLink).toBeVisible();
  await expect(lineageLink).toHaveAttribute("href", `/chat/${SOURCE_THREAD_ID}`);
  await lineageLink.click();
  await expect(page).toHaveURL(new RegExp(`/chat/${SOURCE_THREAD_ID}$`));
  await expect(page.getByText("fork browser source marker")).toBeVisible();
  await expect(page.getByText("fork browser final marker")).toBeVisible();

  const sourceDeleteAndAssetRead = await page.evaluate(
    async ({ sourceThreadId, assetId }) => {
      const deleted = await fetch(`/api/threads/${sourceThreadId}`, {
        method: "DELETE",
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      const asset = await fetch(`/api/uploads/${assetId}`);
      return {
        deleteStatus: deleted.status,
        assetStatus: asset.status,
        assetBytes: (await asset.arrayBuffer()).byteLength,
      };
    },
    { sourceThreadId: SOURCE_THREAD_ID, assetId: ASSET_ID },
  );
  expect(sourceDeleteAndAssetRead.deleteStatus).toBe(204);
  expect(sourceDeleteAndAssetRead.assetStatus).toBe(200);
  expect(sourceDeleteAndAssetRead.assetBytes).toBeGreaterThan(0);
});
