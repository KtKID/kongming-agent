import { defineConfig, devices } from "@playwright/test";

/**
 * task progress 单一 owner 真实浏览器 E2E 配置。
 *
 * Python 服务装配真实 FastAPI、ThreadManager 与 SessionTaskProgressManager；
 * Vite 提供前端资源和同源 API 代理。测试控制路由只模拟外部 LLM 的状态指令。
 */
export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: "task-progress-single-owner-real.spec.ts",
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
      command: ".venv/bin/python tests/support/task_progress_single_owner_e2e_server.py",
      cwd: "..",
      url: "http://127.0.0.1:8080/api/auth/status",
      timeout: 120_000,
      reuseExistingServer: false,
      env: {
        TASK_PROGRESS_E2E_PROGRESS_PATH_LOCATOR:
          "/tmp/kongming-task-progress-single-owner-e2e-path.json",
      },
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
