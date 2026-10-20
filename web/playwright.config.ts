import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests',
  timeout: 30000,
  reporter: [['list']],
  use: {
    baseURL: 'http://localhost:8080',
    headless: true,
    screenshot: 'only-on-failure',
    video: 'off',
    trace: 'off',
  },
  // 复用已在运行的 dev 容器；没起时用 docker compose 起一个。
  // （原先没有 webServer，容器没起时每个用例都 ECONNREFUSED 失败。）
  webServer: {
    command: 'docker compose up -d seedhelper-dev',
    url: 'http://localhost:8080/healthz',
    reuseExistingServer: true,
    timeout: 180_000,
    cwd: '..',
  },
})
