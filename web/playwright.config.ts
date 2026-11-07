import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests',
  timeout: 30000,
  reporter: [['list']],
  use: {
    // dev 容器端口是 8092（8080/8081 已被本机其它容器占用）。
    // 本地起 vite dev 验证未构建的改动时，用 PLAYWRIGHT_BASE_URL 覆盖。
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? 'http://localhost:8092',
    headless: true,
    screenshot: 'only-on-failure',
    video: 'off',
    trace: 'off',
  },
  // 复用已在运行的 dev 容器；没起时用 docker compose 起一个。
  // （原先没有 webServer，容器没起时每个用例都 ECONNREFUSED 失败。）
  webServer: {
    command: 'docker compose up -d medialab-dev',
    url: 'http://localhost:8092/healthz',
    reuseExistingServer: true,
    timeout: 180_000,
    cwd: '..',
  },
})
