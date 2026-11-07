import { test, expect } from '@playwright/test'

async function login(page: any) {
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 10000 })
}

/**
 * 点第一行的「测连」。
 *
 * 站点页有轮询刷新 → 表格持续重绘 → Playwright 的元素定位/稳定性检查会
 * 超时（既有 flaky，与本特性无关）→ 直接用 JS 点 DOM，Vue 处理器照常触发。
 */
async function clickTestButton(page: any) {
  await page.evaluate(() => {
    const btns = Array.from(
      document.querySelectorAll('.ant-table-tbody tr button'),
    ) as HTMLElement[]
    const target = btns.find((b) => b.textContent?.includes('测连'))
    target?.click()
  })
}

/**
 * 测连三态：**不能只看 reachable**。
 *
 * 2026-11-08 prod：ourbits 凭据过期 10 天，面板测连一直显示绿点——
 * 前端只按 reachable 判绿，把 logged_in=false 丢了，用户完全看不出
 * cookie 已过期（种子拉取同时静默零产出，故障彻底不可见）。
 */
test('站点 - 测连三态：可达但未登录显示黄点', async ({ page }) => {
  await login(page)

  // 拦截测连响应：可达但**未登录**（凭据失效）
  await page.route('**/api/v1/sites/*/test', (route: any) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        reachable: true,
        logged_in: false,
        detail: 'cookie 已过期，需重新获取',
        self_test: {},
      }),
    }),
  )

  await page.goto('/sites')
  await page.waitForLoadState('networkidle')
  await clickTestButton(page)

  const dot = page.locator('.test-result-dot').first()
  await expect(dot).toBeVisible({ timeout: 30000 })
  const cls = await dot.getAttribute('class')
  expect(cls).toContain('test-result-dot--warn')
  expect(cls).not.toContain('test-result-dot--ok')
})

test('站点 - 测连三态：可达且已登录显示绿点', async ({ page }) => {
  await login(page)

  await page.route('**/api/v1/sites/*/test', (route: any) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        reachable: true,
        logged_in: true,
        detail: 'ok',
        self_test: {},
      }),
    }),
  )

  await page.goto('/sites')
  await page.waitForLoadState('networkidle')
  await clickTestButton(page)

  const dot = page.locator('.test-result-dot').first()
  await expect(dot).toBeVisible({ timeout: 30000 })
  expect(await dot.getAttribute('class')).toContain('test-result-dot--ok')
})
