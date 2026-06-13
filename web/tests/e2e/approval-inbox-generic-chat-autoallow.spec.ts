import { expect, test } from "@playwright/test";

/**
 * smart-approval-generic-chat-autoallow task #8 e2e。
 *
 * 覆盖 DoD：
 * - generic_chat 通道 inbox Card 在浏览器内真实 mount + 渲染 + auto-approve 倒计时
 * - autoApproveAtMs 非空 → CountdownBar mode="approve"（绿）+ 文案「Ns 自动通过」
 * - 倒计时数字真实递减（前 1s 看到较大数，后看到 1s）
 * - 倒计时到点 → CountdownBar.onComplete → store.resolve(allow=true) →
 *   sender = null 时 console.warn（e2e 环境无真后端 sender）；
 *   随后注入 remove 帧模拟后端 manager 真 set_result(approved) 后 broadcaster
 *   下发的清理帧 → Card 消失
 *
 * 测试策略（与 stage1 approval-inbox-generic-chat.spec.ts 完全一致的"前端层
 * 注入 fake item"模式）：
 *   不启动真后端 / 真 LLM / 真 SafetyDecisionEngine / 真 ApprovalManager。
 *   理由：generic_chat → LLM → safety → ApprovalManager → WS 端到端真链路在 1h
 *   estimate 内跑通且稳定不现实；后端 task #6 集成测（test_auto_allow_integration.py
 *   8 用例含 race condition 10 次压测）已覆盖 manager → set_result → broadcaster
 *   → ws fan-out 全链路。本 e2e 聚焦"前端 UI 链路在浏览器内真实跑通 auto-approve
 *   倒计时 + 到点回调 + remove 帧清理"，避免"Playwright MCP 手动跑过 ≠ 自动化 e2e"
 *   反例。
 *
 * 具体做法：
 *   1. 复用 stage1 的 vite dev :5174（playwright.config.ts 已配 webServer）
 *   2. page.route stub /api/** 让 AuthGuard 放行进入 Layout → ApprovalToastQueue
 *      在 DOM 内挂载
 *   3. page.evaluate 调 window.__APPROVAL_INBOX_STORE__.applyAddFrame 注入
 *      autoApproveAtMs = now + 3000 的 fake generic_chat add 帧
 *   4. 断言倒计时存在 / mode="approve" / 文案含 "自动通过" / 秒数递减
 *   5. 等到点（~3.5s 余量）→ 注入 remove 帧 → Card 消失
 */

const VITE_DEV_URL = "http://127.0.0.1:5174";

/**
 * 把所有后端 API 调用 stub 掉，让 AuthGuard 直接放行进入 Layout。
 *
 * 与 stage1 spec 同款 stubBackend，注意 Playwright 路由 LIFO 顺序匹配：
 * 先注册兜底（宽）再注册具体（窄），具体覆盖兜底。
 */
async function stubBackend(page: import("@playwright/test").Page): Promise<void> {
  await page.route("**/api/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: "[]",
    }),
  );
  await page.route("**/api/auth/me", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true }),
    }),
  );
  await page.route("**/api/v2/threads*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ threads: [] }),
    }),
  );
}

test.describe("smart-approval-generic-chat-autoallow inbox auto-approve countdown e2e", () => {
  test("generic_chat add 帧带 autoApproveAtMs → 倒计时绿色递减 → 到点 + remove 帧 → 卡片消失", async ({
    page,
  }) => {
    await stubBackend(page);

    // 捕获 console 用于断言到点后 store.resolve 被调（sender 未注入 → warn）
    const consoleWarnings: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "warning") {
        consoleWarnings.push(msg.text());
      }
    });

    // 进入 Layout（AuthGuard 因 /api/auth/me 200 放行）→ ApprovalToastQueue 挂载
    await page.goto(`${VITE_DEV_URL}/chat`);
    await expect(page).toHaveURL(/\/chat/);

    // 等 store 被前端代码注入到 window（vite dev 模式下 useApprovalInbox.ts 末尾
    // 的 `if (import.meta.env.DEV)` 会执行）
    await page.waitForFunction(
      () =>
        (window as unknown as { __APPROVAL_INBOX_STORE__?: unknown })
          .__APPROVAL_INBOX_STORE__ !== undefined,
      undefined,
      { timeout: 15_000 },
    );

    // 注入 fake generic_chat add 帧 —— autoApproveAtMs = now + 3000
    // 模拟后端：用户已开 auto-approve toggle → ApprovalRules.classify 返
    // auto_approve_at_ms → manager.request 注入 _PendingApproval →
    // InboxEventSink.emit_approval_required → broadcaster.emit_add 经 ws 推到前端
    const requestId = "e2e-autoallow-1";
    const threadId = "e2e-thread-autoallow-1";
    const COUNTDOWN_MS = 3000;
    await page.evaluate(
      ({ rid, tid, countdownMs }) => {
        type Frame = {
          kind: "approval.inbox.add";
          requestId: string;
          threadId: string;
          toolName: string;
          toolInput: unknown;
          autoApproveAtMs: number | null;
          autoRejectAtMs: number | null;
          blockedByRule: string | null;
          isElevated: boolean;
          channel: string;
          cwd: string;
          arrivedAtMs: number;
          timeoutMs: number | null;
        };
        const store = (
          window as unknown as {
            __APPROVAL_INBOX_STORE__: {
              getState: () => {
                applyAddFrame: (f: Frame) => void;
              };
            };
          }
        ).__APPROVAL_INBOX_STORE__;
        const nowMs = Date.now();
        store.getState().applyAddFrame({
          kind: "approval.inbox.add",
          requestId: rid,
          threadId: tid,
          toolName: "Shell",
          toolInput: { command: "ls" },
          autoApproveAtMs: nowMs + countdownMs,
          autoRejectAtMs: null,
          blockedByRule: null,
          isElevated: false,
          channel: "generic_chat",
          cwd: "/tmp",
          arrivedAtMs: nowMs,
          timeoutMs: 60000,
        });
      },
      { rid: requestId, tid: threadId, countdownMs: COUNTDOWN_MS },
    );

    // Card 出现
    const card = page.locator(
      `[data-testid="approval-inbox-card"][data-request-id="${requestId}"]`,
    );
    await expect(card).toBeVisible({ timeout: 5_000 });

    // tool name 渲染
    await expect(card.getByTestId("approval-inbox-tool-name")).toHaveText(
      "Shell",
    );

    // 倒计时存在 + mode="approve"（绿色/通过）—— 关键断言：优先级 approve > reject > fallback
    const countdownContainer = card.getByTestId("approval-inbox-countdown");
    await expect(countdownContainer).toBeVisible();

    const countdownBar = countdownContainer.getByTestId("countdown-bar");
    await expect(countdownBar).toHaveAttribute("data-mode", "approve");

    // 文案含 "自动通过"（approve 模式专属；reject 模式是 "自动拒绝"）
    const seconds = countdownContainer.getByTestId("countdown-seconds");
    await expect(seconds).toContainText("自动通过");

    // 进度条颜色（bg-primary = approve 绿；bg-destructive = reject 红）
    const progress = countdownContainer.getByTestId("countdown-progress");
    await expect(progress).toHaveClass(/bg-primary/);
    await expect(progress).not.toHaveClass(/bg-destructive/);

    // 抓初始秒数（应该 ≈ 3）
    const initialText = await seconds.textContent();
    expect(initialText).toMatch(/^[1-3]s 自动通过$/);

    // 等 ~1.5s，断言秒数递减（初始 3 → 后续 ≤ 2）
    await page.waitForTimeout(1500);
    const midText = await seconds.textContent();
    expect(midText).toMatch(/^[1-2]s 自动通过$/);
    // 派生数字断言：mid < initial
    const initialNum = Number(initialText?.match(/^(\d+)/)?.[1] ?? "0");
    const midNum = Number(midText?.match(/^(\d+)/)?.[1] ?? "0");
    expect(midNum).toBeLessThan(initialNum);

    // 等到点（3000ms 倒计时 + 500ms 余量 = 等到 4000ms 总）
    // 已等 1500ms，再等 2500ms
    await page.waitForTimeout(2500);

    // 到点后 CountdownBar.onComplete → ApprovalToastCard.onCountdownComplete →
    // resolve(threadId, requestId, true) → senderRef 未注入 → console.warn
    // 这是 e2e 环境的预期行为（真后端走 ws sender 发 resolve 帧给 manager）
    const resolveWarned = consoleWarnings.some((w) =>
      w.includes("[approval-inbox] resolve called but sender not set"),
    );
    expect(resolveWarned).toBe(true);

    // resolve 不本地删 Card（等后端回 remove 帧）—— 模拟后端 manager 真 set_result
    // 后 InboxEventSink.emit_removed → broadcaster.emit_remove → ws → applyRemoveFrame
    await page.evaluate((rid) => {
      const store = (
        window as unknown as {
          __APPROVAL_INBOX_STORE__: {
            getState: () => {
              applyRemoveFrame: (f: {
                kind: "approval.inbox.remove";
                requestId: string;
                reason: "user_decided" | "timeout" | "cancelled";
              }) => void;
            };
          };
        }
      ).__APPROVAL_INBOX_STORE__;
      store.getState().applyRemoveFrame({
        kind: "approval.inbox.remove",
        requestId: rid,
        reason: "user_decided",
      });
    }, requestId);

    // remove 帧到达 → Card 消失
    await expect(card).toHaveCount(0, { timeout: 5_000 });
  });
});
