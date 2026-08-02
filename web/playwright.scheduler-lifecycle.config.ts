import { defineConfig, devices } from "@playwright/test";

/**
 * Scheduler lifecycle 的真实跨层浏览器 E2E 配置。
 *
 * Python 服务提供真实 FastAPI、SchedulerManager、Store 与 ExecutionBridge；
 * Vite 提供前端资源和同源 API/WS 代理。
 */
export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: "scheduler-lifecycle.spec.ts",
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
      command: ".venv/bin/python tests/support/scheduler_lifecycle_e2e_server.py",
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
