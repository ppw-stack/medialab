import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright E2E 配置（web/ 目录）
 *
 * 运行方式（在仓库根）：
 *   cd web && npx playwright test
 *
 * 测试会自动：
 *   1. 启动后端（uvicorn，端口 18180）
 *   2. 提供服务（vite preview，端口 18190）
 *   3. 启动 Chromium 跑用例
 *   4. teardown
 */

const BACKEND_PORT = 18180;
const FRONTEND_PORT = 18190;
const BACKEND_URL = `http://127.0.0.1:${BACKEND_PORT}`;
const FRONTEND_URL = `http://127.0.0.1:${FRONTEND_PORT}`;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,            // 顺序跑：避免多实例写 DB 互踩
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? "list" : "line",
  timeout: 30_000,
  expect: { timeout: 5_000 },
  use: {
    baseURL: FRONTEND_URL,
    headless: true,
    screenshot: "only-on-failure",
    trace: "on-first-retry",
    video: "retain-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
  globalSetup: "./e2e/global-setup.ts",
  globalTeardown: "./e2e/global-teardown.ts",
});

export { BACKEND_URL, FRONTEND_URL, BACKEND_PORT, FRONTEND_PORT };
