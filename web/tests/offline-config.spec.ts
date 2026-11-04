import { test, expect } from '@playwright/test'

async function login(page: any) {
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 10000 })
}

/**
 * 离线下载的**配置区**。
 *
 * 背景（2026-11-04）：三项配置原先只能改 `.env.prod` 重启容器。现在
 * 它们落库（`app_env_settings`），在页面上改**立即生效**——文件服务的
 * 路由常驻，每次请求时读当前配置决定放行与否（FastAPI 不支持运行时
 * 增删路由）。
 *
 * 本 spec 只做**只读断言 + 非法输入被拒**，不改真实配置——e2e 连的是
 * 真实容器，写坏配置会让离线下载立刻不可用。
 */
test('离线下载 - 配置区有三个字段 + 状态标签', async ({ page }) => {
  await login(page)
  await page.goto('/offline')
  await page.waitForLoadState('networkidle')

  // 配置卡始终可见（未启用时它是唯一入口）
  await expect(page.getByText('① 密钥')).toBeVisible()
  await expect(page.getByText('② 公网入口')).toBeVisible()
  await expect(page.getByText('③ 源根目录')).toBeVisible()

  // 状态标签与后端一致
  const resp = await page.request.get('/api/offline-downloads/capabilities')
  const caps = await resp.json()
  const tag = page.locator('.cfg-tag')
  await expect(tag).toBeVisible()
  await expect(tag).toHaveText(caps.enabled ? '已启用' : '未启用')

  // 保存按钮存在
  await expect(page.getByRole('button', { name: '保存配置' })).toBeVisible()

  await page.screenshot({ path: 'test-results/offline-config.png', fullPage: true })
})

test('离线下载 - 公网入口校验（非法值 → 400，不改动现有配置）', async ({ page }) => {
  await login(page)

  const before = await (
    await page.request.get('/api/offline-downloads/capabilities')
  ).json()

  // 非法公网入口必须被拒（不落库）
  const r = await page.request.put('/api/offline-downloads/config', {
    data: { public_base: 'not-a-url' },
  })
  expect(r.status()).toBe(400)
  expect((await r.json()).detail).toContain('http')

  // 配置没被写坏
  const after = await (
    await page.request.get('/api/offline-downloads/capabilities')
  ).json()
  expect(after).toEqual(before)
})

test('离线下载 - 源根目录的目录浏览器能打开（不限权）', async ({ page }) => {
  await login(page)
  await page.goto('/offline')
  await page.waitForLoadState('networkidle')

  await page.getByRole('button', { name: '浏览…' }).click()
  const modal = page.locator('.ant-modal').filter({ hasText: '选择源根目录' })
  await expect(modal).toBeVisible()

  // 从 `/` 开始列目录（与「数据同步」一致：服务能看到的目录都能选）
  const resp = await page.request.get('/api/offline-downloads/local-dirs?path=/')
  expect(resp.status()).toBe(200)
  const dirs = (await resp.json()).dirs as { id: string; name: string }[]
  expect(dirs.length).toBeGreaterThan(0)
  // id 是完整路径（前端拿它当下一层直接请求）
  expect(dirs[0].id.startsWith('/')).toBe(true)

  await expect(modal.getByRole('button', { name: '使用此目录' })).toBeVisible()
  await page.screenshot({
    path: 'test-results/offline-root-browser.png',
    fullPage: true,
  })
})
