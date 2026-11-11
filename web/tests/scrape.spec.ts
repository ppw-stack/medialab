import { test, expect } from '@playwright/test'

async function login(page: any) {
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 10000 })
}

/**
 * 整理与刮削页（/scrape）。
 *
 * 验证端到端流程：页面可达 → 配置加载（TMDB Key 不回显）→ 单选按钮组
 * 渲染（枚举参数的既有契约：runtime_params 枚举必须用 radio 而非输入框）
 * → 保存配置 → Key 留空不修改（has_key 保持）。
 *
 * 注意：Playwright e2e 连的是真实容器（localhost:8080），CI 不跑。
 * 需在本机起 dev 容器后跑。
 */
test('整理与刮削 - 配置加载与 Key 不回显', async ({ page }) => {
  await login(page)
  await page.goto('/scrape')
  await page.waitForLoadState('networkidle')

  // 页面标题
  await expect(page.locator('.page-header')).toContainText('整理与刮削')

  // 配置卡渲染
  const configCard = page.locator('.ant-card').filter({ hasText: '配置' }).first()
  await expect(configCard).toBeVisible()

  // 后端是唯一事实源：拿 API 当前值断言 UI 状态一致
  const resp = await page.request.get('/api/scrape/config')
  const config = await resp.json()

  // TMDB Key 输入框：placeholder 依 has_key 变化，**输入框永远为空**（不回显）
  const keyInput = page.locator('input[type="password"]').first()
  await expect(keyInput).toBeVisible()
  const keyInputValue = await keyInput.inputValue()
  expect(keyInputValue).toBe('')
  if (config.has_key) {
    await expect(keyInput).toHaveAttribute('placeholder', /已配置/)
  }

  // 媒体库目录输入框初值与 API 一致
  const dirInput = page.locator('input[placeholder="/media/library"]')
  await expect(dirInput).toHaveValue(config.media_library_dir)

  // 整理源目录输入框初值与 API 一致
  const sourceInput = page.locator('input[placeholder="/downloads/media,/data/incoming"]')
  await expect(sourceInput).toHaveValue(config.organize_source_dirs)

  // 枚举参数渲染成单选按钮组（不是数字输入框）
  await expect(configCard.locator('.ant-radio-group')).toHaveCount(4)

  // 就绪标签
  const readyTag = configCard.locator('.cfg-tag')
  await expect(readyTag).toContainText(config.ready ? '已就绪' : '未就绪')

  // 截图
  await page.screenshot({ path: 'test-results/scrape-config.png', fullPage: true })
})

test('整理与刮削 - 保存配置后 Key 留空不修改', async ({ page }) => {
  await login(page)
  await page.goto('/scrape')
  await page.waitForLoadState('networkidle')

  const before = await (await page.request.get('/api/scrape/config')).json()

  // 不填 Key（留空 = 不修改），改目录并保存
  const dirInput = page.locator('input[placeholder="/media/library"]')
  await dirInput.fill(before.media_library_dir || '/media/library')
  const sourceInput = page.locator(
    'input[placeholder="/downloads/media,/data/incoming"]'
  )
  await sourceInput.fill(before.organize_source_dirs || '/downloads/media')
  const saveButton = page.getByRole('button', { name: /保存配置/ })
  await saveButton.click()
  await page.waitForTimeout(500)

  const after = await (await page.request.get('/api/scrape/config')).json()

  // has_key 与保存前一致（Key 留空 ≠ 清除）
  expect(after.has_key).toBe(before.has_key)
  // 目录与源目录都写进去了
  expect(after.media_library_dir).toBe(before.media_library_dir || '/media/library')
  expect(after.organize_source_dirs).toBe(
    before.organize_source_dirs || '/downloads/media'
  )
  // 整个响应体不含明文 Key
  expect(JSON.stringify(after)).not.toContain('sk-')
})
