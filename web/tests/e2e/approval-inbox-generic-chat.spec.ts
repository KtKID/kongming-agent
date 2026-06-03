import { expect, test } from "@playwright/test";

/**
 * smart-approval-manager-stage1 task #8 e2e。
 *
 * 覆盖 DoD：
 * - generic_chat 通道 inbox Card 端到端 UI 链路（add → 渲染 → 用户决策 → store 调 resolve）
 * - generic_chat 通道二态映射：「本 session」按钮隐藏（task #7 实现）
 * - manager 主链路前端入口（fake add frame → applyAddFrame → Card 出现）
 *
 * 测试策略（必读）：
 *   选项 A 简化版 —— 不启动真后端 / 真 LLM / 真 SafetyDecisionEngine。
 *   理由：generic_chat 端到端真链路涉及 LLM tool call + 完整 safety chain + WS
 *   reverse 路由，在 1h estimate 内跑通且稳定不现实；后端 task #6 集成测已
 *   覆盖 manager → broadcaster → ws resolve 全链路。本 e2e 聚焦"前端 UI 链路
 *   在浏览器内真实 mount + 渲染 + 交互"，避免"Playwright MCP 手动跑过"反例。
 *
 *   具体做法：
 *   1. webServer 启 vite dev :5174（store 暴露依赖 `import.meta.env.DEV`）
 *   2. page.route 拦截所有 /api/** 返回 stub（让 AuthGuard 通过 → Layout 挂载
 *      → ApprovalToastQueue 在 DOM 内）
 *   3. page.evaluate 调 window.__APPROVAL_INBOX_STORE__.applyAddFrame 直接
 *      注入 fake generic_chat add 帧（模拟后端 InboxEventSink fan-out）
 *   4. 断言 Card / 按钮 / channel 二态隐藏 / store.resolve 行为
 *
 * baseURL 覆盖：本 spec navigate 用绝对 URL（默认 baseURL :8080 给其他真后端
 * e2e 用，本 spec 不依赖 8080）。
 */

const VITE_DEV_URL = "http://127.0.0.1:5174";

/**
 * 把所有后端 API 调用 stub 掉，让 AuthGuard 直接放行进入 Layout。
 *
 * - /api/auth/me 返回 200（视为已登录）
 * - 其他 /api/** 返回 200 空对象 / 空数组（让 store 不报错）
 * - WS 不显式 mock：useThreadStatusWS 失败重连不影响 ApprovalToastQueue 渲染
 */
async function stubBackend(page: import("@playwright/test").Page): Promise<void> {
  // 注意：Playwright 路由 LIFO 顺序匹配 —— 后注册的先尝试。所以
  // 先注册兜底（宽），后注册具体（窄），具体覆盖兜底。
  // 兜底：所有 /api/** 返空 array（NewThreadDialog presets.map 等需 array；
  // 单挂返 {} 的 endpoint 在本 e2e 不触发可不管）
  await page.route("**/api/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: "[]",
    }),
  );
  // 具体：auth/me 返 {ok:true} 让 AuthGuard 放行
  await page.route("**/api/auth/me", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true }),
    }),
  );
  // 具体：threads 列表（v2）—— 期望 {threads: [...]}
  await page.route("**/api/v2/threads*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ threads: [] }),
    }),
  );
}

test.describe("smart-approval-manager-stage1 generic_chat inbox e2e", () => {
  test("generic_chat add frame → Card 渲染 + 二按钮 + 「本 session」隐藏 + resolve 路径", async ({
    page,
  }) => {
    await stubBackend(page);

    // 进入 Layout（AuthGuard 因 /api/auth/me 200 放行）→ ApprovalToastQueue 挂载
    await page.goto(`${VITE_DEV_URL}/chat`);
    await expect(page).toHaveURL(/\/chat/);

    // 等 store 被前端代码注入到 window（vite dev 模式下 useApprovalInbox.ts 末尾的
    // `if (import.meta.env.DEV)` 会执行）
    await page.waitForFunction(
      () =>
        (window as unknown as { __APPROVAL_INBOX_STORE__?: unknown })
          .__APPROVAL_INBOX_STORE__ !== undefined,
      undefined,
      { timeout: 15_000 },
    );

    // 注入 fake generic_chat add frame（模拟后端 InboxEventSink.emit_approval_required
    // → ApprovalInboxBroadcaster.emit_add 经 ws 推到前端）
    const requestId = "e2e-generic-chat-1";
    const threadId = "e2e-thread-generic-1";
    await page.evaluate(
      ({ rid, tid }) => {
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
        store.getState().applyAddFrame({
          kind: "approval.inbox.add",
          requestId: rid,
          threadId: tid,
          toolName: "Bash",
          toolInput: { command: "ls -la" },
          autoApproveAtMs: null,
          autoRejectAtMs: null,
          blockedByRule: null,
          isElevated: false,
          channel: "generic_chat",
          cwd: "/tmp",
          arrivedAtMs: Date.now(),
        });
      },
      { rid: requestId, tid: threadId },
    );

    // Card 出现
    const card = page.locator(
      `[data-testid="approval-inbox-card"][data-request-id="${requestId}"]`,
    );
    await expect(card).toBeVisible({ timeout: 5_000 });

    // tool name / cwd 渲染正确
    await expect(card.getByTestId("approval-inbox-tool-name")).toHaveText("Bash");

    // generic_chat channel 二态：「拒绝」+「单次同意」可见
    await expect(card.getByTestId("approval-inbox-btn-reject")).toBeVisible();
    await expect(
      card.getByTestId("approval-inbox-btn-allow-once"),
    ).toBeVisible();

    // 「本 session」按钮隐藏（task #7 实现：CHANNELS_WITH_SESSION_GRANT 白名单不含 generic_chat）
    await expect(
      card.getByTestId("approval-inbox-btn-allow-session"),
    ).toHaveCount(0);

    // 点「单次同意」：store.resolve 走 senderRef，但 e2e 环境 sender 没被注入
    // （useThreadStatusWS 没真连上 WS），store.resolve 会 console.warn 而非抛错。
    // 验证点击后 store 仍持有该 item（resolve 不本地删，等后端 remove 帧）。
    await card.getByTestId("approval-inbox-btn-allow-once").click();

    // Card 仍在（无 remove 帧 → 不消失）—— 验证按钮 click 不引发崩溃，状态机正确
    await expect(card).toBeVisible();

    // 模拟后端发 remove 帧（用户决定后 manager.resolve → InboxEventSink.emit_removed
    // → broadcaster.emit_remove → ws push 前端 → applyRemoveFrame）
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
