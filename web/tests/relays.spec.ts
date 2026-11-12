import { test, expect } from '@playwright/test'

/**
 * 转存页（光鸭 → 光鸭）的页面级契约。
 *
 * ⚠️ e2e 连的是**真实环境**（dev 容器 + 真实存储源），CI 不跑。
 * 这里只断言**不依赖具体账号数据**的结构性契约：
 * 页面能打开、表单控件齐、两个目录浏览器都在、开关存在。
 */

async function login(page: any) {
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url: any) => !url.pathname.startsWith('/login'), { timeout: 10000 })
}

test('转存页 - 打开与表单结构', async ({ page }) => {
  await login(page)
  await page.goto('/relays')
  await page.waitForLoadState('networkidle')

  // 标题
  await expect(page.getByText('转存（光鸭 → 光鸭）')).toBeVisible()

  // 两个目录浏览器
  await expect(page.getByText('① 源光鸭目录')).toBeVisible()
  await expect(page.getByText('② 目标光鸭目录')).toBeVisible()

  // 存储源下拉 ×2（源 / 目标）
  await expect(page.locator('.ant-select')).toHaveCount(2)

  // 关键开关：**搬完清理源**（默认关——删数据不可逆）
  const switches = page.locator('.ant-switch')
  const count = await switches.count()
  expect(count).toBeGreaterThanOrEqual(2)

  // 按钮
  await expect(page.getByRole('button', { name: /开始转存/ })).toBeVisible()
})

test('转存页 - 侧栏入口存在', async ({ page }) => {
  await login(page)
  await page.goto('/relays')
  await page.waitForLoadState('networkidle')

  const menu = page.locator('.ant-menu')
  await expect(menu.getByText('转存')).toBeVisible()
})

test('转存页 - 目录浏览接口返回形状', async ({ page }) => {
  await login(page)
  await page.goto('/relays')
  await page.waitForLoadState('networkidle')

  // 页面加载时会调 /api/relays/dirs（若有光鸭存储源）
  const resp = await page.request.get('/api/relays/dirs?driver_id=1')
  // 存储源不存在 → 404；存在 → 200。两者都要是**结构化 JSON**，不能 500。
  if (resp.status() !== 404) {
    expect(resp.status()).toBe(200)
    const body = await resp.json()
    expect(Array.isArray(body.dirs)).toBeTruthy()
  }
})
