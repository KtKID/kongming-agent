/**
 * 真实 LLM 通用 thread E2E 配置。
 *
 * 关键流程：复用已启动的 Kongming Web，使用独立 Chromium 上下文登录，
 * 创建 generic thread、等待真实模型回复、从 assistant 气泡分叉并在目标 thread 继续对话。
 * 本配置不启动替身服务，也不修改 provider 配置。
 */
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: "real-llm-thread.spec.ts",
  fullyParallel: false,
  workers: 1,
  timeout: 360_000,
  reporter: "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:60000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
