import { defineConfig, devices } from "@playwright/test";

/**
 * kongming-agent v0.1.5 e2e 配置
 *
 * - chromium 单浏览器（v0.1.5 桌面优先）
 * - baseURL :8080：联调阶段假定后端 web-app-shell 在 8080 起，serve dist + REST + WS
 * - 本任务**不跑** e2e；联调阶段（web-app-shell + web-thread-manager 就绪）后统一跑
 */
export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:8080",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  // smart-approval-manager-stage1 task #8：本 webServer 仅服务
  // approval-inbox-generic-chat.spec.ts —— 该 spec 显式 navigate 到
  // http://127.0.0.1:5174 走 vite dev（store 暴露依赖 `import.meta.env.DEV`）。
  // 其他 e2e 仍走全局 baseURL :8080 真后端，互不影响。reuseExistingServer
  // 让本地开发已开的 vite dev 不被重复拉起。
  webServer: {
    command: "pnpm exec vite --host 127.0.0.1 --port 5174 --strictPort",
    url: "http://127.0.0.1:5174",
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
    stdout: "pipe",
    stderr: "pipe",
  },
});
