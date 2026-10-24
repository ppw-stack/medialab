import { test, expect } from '@playwright/test'

async function login(page: any) {
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 10000 })
}

/**
 * 辅种下载器统计页面（2026-10-24 改为「定时抓取 + 读快照」）。
 *
 * 关键变化：页面**不再实时打下载器接口**——辅种站种子量大（3000+），
 * 实时拉一次要 20s+，每次打开页面都拉既慢又给下载器加压。故：
 *
 * - 页面读 `cross_seed_snapshot` 表（DB 读，毫秒级）
 * - 没有快照 → 显示「等待首次抓取」，**不是**空白页
 * - 抓取时间 / 异常数 / 在途数都来自快照
 *
 * 因此本用例**不依赖辅种站可达**——辅种站不可达时页面仍应正常渲染。
 */
test('辅种下载器统计页面 - 读快照而非实时拉取', async ({ page }) => {
  await login(page)
  await page.goto('/cross-seed')

  // 页面标题
  await expect(page.locator('h1.page-header__title')).toHaveText('辅种下载器统计')

  // 副标题说明是定时抓取
  await expect(page.locator('.page-header__sub')).toContainText('定时抓取')

  // tab 渲染
  const tabs = page.locator('.ant-tabs-tab')
  const tabCount = await tabs.count()
  console.log(`  ✓ tab 数: ${tabCount}`)

  if (tabCount === 0) {
    // 没有辅种下载器 → 空状态（引导去下载器页勾选）
    await expect(page.locator('.ant-empty')).toBeVisible()
    console.log('  ✓ 无辅种下载器：显示空状态引导')
    return
  }

  // 切到第一个 tab
  await tabs.first().click()

  // 页面骨架：要么有快照（显示 summary），要么显示「等待首次抓取」
  // 两种都是**有效状态**——不该是空白（加载完成前不得渲染空状态）
  await page.waitForTimeout(500)
  const hasSummary = await page.locator('.summary-bar').count()
  const hasWaiting = await page.locator('.ant-alert').filter({ hasText: '等待首次抓取' }).count()
  expect(hasSummary + hasWaiting).toBeGreaterThan(0)

  if (hasWaiting > 0) {
    console.log('  ✓ 该辅种下载器还没有快照：显示「等待首次抓取」')
    return
  }

  // 有快照：summary 文本含种子数 / 分组数
  const summary = page.locator('.summary-bar')
  await expect(summary).toBeVisible()
  const summaryText = (await summary.textContent())?.trim() ?? ''
  console.log(`  ✓ summary: ${summaryText.slice(0, 200)}`)
  expect(summaryText).toMatch(/共\s*\d+\s*种子/)
  expect(summaryText).toMatch(/\d+\s*个唯一分组/)
  expect(summaryText).toMatch(/抓取于/)

  // 统计表格列头
  await expect(page.locator('th:has-text("原始名称")').first()).toBeVisible()
  await expect(page.locator('th:has-text("大小")').first()).toBeVisible()
  await expect(page.locator('th:has-text("出现次数")').first()).toBeVisible()

  // 异常 tag
  const tag = page.locator('.summary-bar .ant-tag').first()
  await expect(tag).toBeVisible()
  const tagText = (await tag.textContent())?.trim() ?? ''
  console.log(`  ✓ 异常 tag: ${tagText}`)
  expect(tagText).toMatch(/巡查发现 \d+ 个异常|无异常/)

  // 截图
  await page.screenshot({
    path: 'test-results/cross-seed-page.png',
    fullPage: true,
  })
  console.log('  ✓ 截图: web/test-results/cross-seed-page.png')

  // 菜单项文案
  const menuItem = page.locator('.ant-menu-item:has-text("辅种统计")')
  await expect(menuItem).toBeVisible()
})

test('辅种下载器统计页面 - 初始 tab 渲染', async ({ page }) => {
  await login(page)
  await page.goto('/cross-seed')

  const tabs = page.locator('.ant-tabs-tab')
  const tabCount = await tabs.count()
  if (tabCount === 0) {
    console.log('  ✓ 无辅种下载器：跳过 tab 断言')
    return
  }

  const firstTab = tabs.first()
  await expect(firstTab).toHaveClass(/ant-tabs-tab-active/)
  console.log(`  ✓ 初始选中: ${(await firstTab.textContent())?.trim()}`)
})
