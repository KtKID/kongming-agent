import { defineConfig, devices } from "@playwright/test";

/**
 * Web thread 状态与审批双浏览器 E2E 配置。
 *
 * Python 服务提供真实 FastAPI、ThreadManager、ThreadStatusManager 与
 * ApprovalInboxBroadcaster；Vite 提供前端资源和同源 API/WS 代理。
 */
export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: "thread-state-authority.spec.ts",
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:5174",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      command: ".venv/bin/python tests/support/thread_state_authority_e2e_server.py",
      cwd: "..",
      url: "http://127.0.0.1:8080/api/auth/status",
      timeout: 120_000,
      reuseExistingServer: false,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      command: "pnpm exec vite --host 127.0.0.1 --port 5174 --strictPort",
      cwd: ".",
      url: "http://127.0.0.1:5174",
      timeout: 120_000,
      reuseExistingServer: false,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
});
