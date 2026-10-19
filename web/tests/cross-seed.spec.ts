import { test, expect } from '@playwright/test'

test('辅种下载器统计页面 (a5444dd @ dev)', async ({ page }) => {
  // 1) 登录
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 10000 })

  // 2) 访问 /cross-seed
  await page.goto('/cross-seed')

  // 3) 页面标题
  await expect(page.locator('h1.page-header__title')).toHaveText('辅种下载器统计')

  // 4) 副标题含 cron 文案
  await expect(page.locator('.page-header__sub')).toContainText('cron 每 6h')

  // 5) tab 渲染 (期望 2 个: mock-cs, tr-172)
  const tabs = page.locator('.ant-tabs-tab')
  await expect(tabs).toHaveCount(2)
  console.log(`  ✓ tab 数: 2 (mock-cs, tr-172)`)

  // 6) 切到 tr-172 tab (192.168.0.172:9091, 真实可达)
  await page.locator('.ant-tabs-tab:has-text("tr-172")').click()

  // 7) 等表格表头渲染 (scan 异步)
  await expect(page.locator('th:has-text("原始名称")').first()).toBeVisible({ timeout: 15000 })
  await expect(page.locator('th:has-text("大小 (GB)")').first()).toBeVisible()
  await expect(page.locator('th:has-text("出现次数")').first()).toBeVisible()
  // 样本 hash 列已移除
  await expect(page.locator('th:has-text("样本 hash")')).toHaveCount(0)
  console.log(`  ✓ 表格列头: 原始名称 / 大小 / 出现次数 (样本 hash 已移除)`)

  // 8) 等首行数据
  await expect(page.locator('.ant-table-tbody > tr.ant-table-row').first()).toBeVisible({ timeout: 15000 })
  const rowCount = await page.locator('.ant-table-tbody > tr.ant-table-row').count()
  expect(rowCount).toBeGreaterThan(0)
  console.log(`  ✓ 首屏行数: ${rowCount}`)

  // 9) summary 文本含"共 N 种子"
  const summary = page.locator('.summary-bar')
  await expect(summary).toBeVisible()
  const summaryText = (await summary.textContent())?.trim() ?? ''
  console.log(`  ✓ summary: ${summaryText.slice(0, 200)}`)
  expect(summaryText).toMatch(/共\s*\d+\s*种子/)
  expect(summaryText).toMatch(/\d+\s*个唯一分组/)

  // 10) 异常 tag
  const tag = page.locator('.summary-bar .ant-tag').first()
  await expect(tag).toBeVisible()
  const tagText = (await tag.textContent())?.trim() ?? ''
  console.log(`  ✓ 异常 tag: ${tagText}`)
  expect(tagText).toMatch(/cron 巡查发现 \d+ 个异常|无异常/)

  // 11) 截图 - tr-172 视图
  await page.screenshot({
    path: '../.orca/drops/cross-seed-page-dev-2026-10-19.png',
    fullPage: true,
  })
  console.log(`  ✓ 截图: .orca/drops/cross-seed-page-dev-2026-10-19.png`)

  // 12) 验证菜单项文案 "辅种统计"
  const menuItem = page.locator('.ant-menu-item:has-text("辅种统计")')
  await expect(menuItem).toBeVisible()
  console.log(`  ✓ 菜单项: 辅种统计`)
})

test('辅种下载器统计页面 - 初始 tab 渲染', async ({ page }) => {
  // 仅验证页面骨架 + 初始 tab 选择逻辑
  await page.goto('/login')
  await page.locator('input[type="text"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 10000 })

  await page.goto('/cross-seed')

  // 第一个 tab 应该是 mock-cs (按 ID 升序)
  const firstTab = page.locator('.ant-tabs-tab').first()
  await expect(firstTab).toContainText('mock-cs')
  await expect(firstTab).toHaveClass(/ant-tabs-tab-active/)
  console.log(`  ✓ 初始选中: mock-cs (ID 最小, 按 ASC 排序)`)
})
