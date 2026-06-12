import { expect, test } from "@playwright/test";

/**
 * 图片粘贴端到端 e2e（联调阶段跑）。
 *
 * 覆盖 claude-image-paste-e2e task 的 #26 + #27：
 *   #26 主链路：粘贴 PNG → 缩略图 → 上传 endpoint 被调 → 发送 → 用户消息含图 → WS user.input 帧含 attachments
 *   #27 历史回放：刷新 → 历史缩略图回显 → 点击放大 lightbox → ESC 关闭
 *
 * 设计原则（与 chat.spec.ts 一致）：
 * - 联调期/mock 期不断言 Claude assistant 真实文本回复（注释保留，联调期取消注释）
 * - 但 **必须** 断言协议层：上传 endpoint 真的被调、WS user.input 帧 payload 含 attachments
 * - 不允许 .skip，跳过段直接注释
 */

const PASSWORD = process.env.E2E_PASSWORD ?? "test-pwd";

/** 最小合法 1x1 透明 PNG（67 字节，base64 用于 evaluate 注入） */
const PNG_1x1_BYTES_ARRAY = [
  0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d, 0x49,
  0x48, 0x44, 0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06,
  0x00, 0x00, 0x00, 0x1f, 0x15, 0xc4, 0x89, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x44,
  0x41, 0x54, 0x78, 0x9c, 0x63, 0x00, 0x01, 0x00, 0x00, 0x05, 0x00, 0x01, 0x0d,
  0x0a, 0x2d, 0xb4, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4e, 0x44, 0xae, 0x42,
  0x60, 0x82,
];

async function login(page: import("@playwright/test").Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("密码").fill(PASSWORD);
  await page.getByRole("button", { name: /登录/ }).click();
  await expect(page).toHaveURL(/\/chat/);
}

/**
 * 把一张 1x1 PNG 作为 ClipboardEvent 注入到 textarea 上。
 *
 * 注意：使用 page.evaluate 在浏览器上下文里构造 File + DataTransfer + ClipboardEvent，
 * 因为 playwright 没有原生 paste-image API（只能 paste 文本）。
 */
async function pastePngIntoComposer(
  page: import("@playwright/test").Page,
  pngBytes: number[],
): Promise<void> {
  await page.evaluate((bytes) => {
    const u8 = Uint8Array.from(bytes);
    const file = new File([u8], "paste.png", { type: "image/png" });
    const dt = new DataTransfer();
    dt.items.add(file);
    const event = new ClipboardEvent("paste", {
      clipboardData: dt,
      bubbles: true,
      cancelable: true,
    });
    const ta = document.querySelector('textarea[aria-label="消息输入"]');
    if (ta) ta.dispatchEvent(event);
  }, pngBytes);
}

test.describe("image-paste-claude", () => {
  test("#26 粘贴图片 → 缩略图 → 上传 → 发送 → 用户消息含图 → WS 帧带 attachments", async ({
    page,
  }) => {
    await login(page);

    // 拦截上传 endpoint
    const uploadCalls: { method: string; url: string }[] = [];
    page.on("request", (req) => {
      if (req.url().includes("/api/uploads/images")) {
        uploadCalls.push({ method: req.method(), url: req.url() });
      }
    });

    // 拦截 WS framesent
    const wsFrames: string[] = [];
    page.on("websocket", (ws) => {
      ws.on("framesent", (data) => {
        if (typeof data.payload === "string") {
          wsFrames.push(data.payload);
        }
      });
    });

    // 1. 新建 thread
    await page.getByLabel("新建会话").click();
    await page.getByPlaceholder(/会话名/).fill("e2e-image-paste");
    await page.getByRole("button", { name: /创建/ }).click();
    await expect(page).toHaveURL(/\/chat\/thread-/);

    // 2. focus textarea + 粘贴 PNG
    const textarea = page.getByLabel("消息输入");
    await textarea.focus();
    await pastePngIntoComposer(page, PNG_1x1_BYTES_ARRAY);

    // 3. 缩略图条出现
    const strip = page.getByTestId("attachment-thumbnail-strip");
    await expect(strip).toBeVisible({ timeout: 5000 });

    // 4. 等上传完成（networkidle 后 POST 已发出）
    await page.waitForLoadState("networkidle");
    expect(uploadCalls.length).toBeGreaterThanOrEqual(1);
    expect(uploadCalls[0]?.method).toBe("POST");

    // 5. 输入文字 + 发送
    await textarea.fill("看图");
    await page.getByTestId("composer-send").click();

    // 6. 用户消息出现 + 消息附件缩略图渲染
    await expect(page.getByText("看图")).toBeVisible();
    await expect(
      page.getByTestId("message-attachment-thumb").first(),
    ).toBeVisible({ timeout: 5000 });

    // 7. WS user.input 帧含 attachments + kind=image
    // 给 WS 一小段时间把帧吐出（page.on 是异步收集的）
    await page
      .waitForFunction(() => true, undefined, { timeout: 500 })
      .catch(() => {
        /* 仅用作时序屏障 */
      });

    const userInputFrame = wsFrames.find((f) =>
      f.includes('"kind":"user.input"'),
    );
    expect(userInputFrame, "WS user.input 帧应已发出").toBeDefined();
    expect(userInputFrame).toContain('"attachments"');
    expect(userInputFrame).toContain('"kind":"image"');

    // 8. mock 期：不断言 Claude assistant 真实回复
    // 联调期取消下行注释：
    // await expect(page.locator('[data-role="assistant"]').last()).toBeVisible({
    //   timeout: 30_000,
    // });
  });

  test("#27 刷新 → 历史缩略图回显 → 点击放大 lightbox → ESC 关闭", async ({
    page,
  }) => {
    await login(page);

    // 1. 选第一个 thread（依赖 #26 已留下一张图，或环境里已有带附件的历史）
    const firstThread = page.locator("aside a").first();
    await firstThread.click();

    // 2. 刷新页面让前端走 thread.history 帧回放路径
    await page.reload();
    await page.waitForLoadState("networkidle");

    // 3. 历史消息缩略图回显
    const historyThumb = page.getByTestId("message-attachment-thumb").first();
    await expect(historyThumb).toBeVisible({ timeout: 5000 });

    // 4. 点击缩略图 → lightbox 出现
    await historyThumb.click();
    const lightbox = page.getByTestId("image-lightbox");
    await expect(lightbox).toBeVisible({ timeout: 3000 });

    // 5. 按 ESC → lightbox 关闭
    await page.keyboard.press("Escape");
    await expect(lightbox).not.toBeVisible({ timeout: 3000 });
  });
});
