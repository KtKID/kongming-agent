import { defineConfig, devices } from "@playwright/test";

/**
 * 完整对话 fork 的隔离浏览器 E2E 配置。
 *
 * Python 服务提供真实 FastAPI、ThreadManager、FileSession 与 AssetStorage；
 * Vite 只负责前端资源和同源代理。两个服务都由 Playwright 管理生命周期。
 */
export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: "thread-fork.spec.ts",
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
      command: "uv run python tests/support/web_thread_fork_e2e_server.py",
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
