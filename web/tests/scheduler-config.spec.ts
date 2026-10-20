import { test, expect } from '@playwright/test'

async function login(page: any) {
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 10000 })
}

test('调度作业 - 配置 modal (push_free_torrents 3 params)', async ({ page }) => {
  await login(page)
  await page.goto('/settings')
  await page.waitForLoadState('networkidle')

  // 找到 push_free_torrents 行的「配置」按钮
  const row = page.locator('.job-item').filter({ hasText: 'push_free_torrents' })
  await expect(row).toBeVisible()
  await row.getByRole('button', { name: /配\s*置/ }).click()

  // modal 打开
  const modal = page.locator('.ant-modal-content').filter({ hasText: '配置 · 免费种推送' })
  await expect(modal).toBeVisible()

  // 3 个 param input-number (限时种最少剩余/单次推送上限/下载间隔)
  const numbers = modal.locator('.ant-input-number-input')
  await expect(numbers).toHaveCount(3)
  console.log('  ✓ push_free_torrents modal 渲染 3 个 param')

  // cron 输入
  const cronInput = modal.locator('input[placeholder="*/10 * * * *"]')
  await expect(cronInput).toBeVisible()
  const cronVal = await cronInput.inputValue()
  expect(cronVal).toMatch(/^\*\/30 \* \* \* \*$/)
  console.log(`  ✓ cron 默认值: ${cronVal}`)

  // 关闭
  await modal.getByRole('button', { name: /取\s*消/ }).click()
})

test('调度作业 - 配置 modal (fetch_all_sites 0 params)', async ({ page }) => {
  await login(page)
  await page.goto('/settings')
  await page.waitForLoadState('networkidle')

  const row = page.locator('.job-item').filter({ hasText: 'fetch_all_sites' })
  await expect(row).toBeVisible()
  await row.getByRole('button', { name: /配\s*置/ }).click()

  const modal = page.locator('.ant-modal-content').filter({ hasText: '配置 · 站点拉取' })
  await expect(modal).toBeVisible()

  // 0 个 param (无 input-number)
  const numbers = modal.locator('.ant-input-number-input')
  await expect(numbers).toHaveCount(0)

  // 提示: 本作业无可调参数
  await expect(modal.locator('text=本作业无可调参数')).toBeVisible()
  console.log('  ✓ fetch_all_sites modal 无 param (符合预期)')

  await modal.getByRole('button', { name: /取\s*消/ }).click()
})

test('调度作业 - 配置 modal (monitor_free_window 1 param)', async ({ page }) => {
  await login(page)
  await page.goto('/settings')
  await page.waitForLoadState('networkidle')

  const row = page.locator('.job-item').filter({ hasText: 'monitor_free_window' })
  await row.getByRole('button', { name: /配\s*置/ }).click()

  const modal = page.locator('.ant-modal-content').filter({ hasText: '配置 · 免费窗口监测' })
  await expect(modal).toBeVisible()

  const numbers = modal.locator('.ant-input-number-input')
  await expect(numbers).toHaveCount(1)
  console.log('  ✓ monitor_free_window modal 渲染 1 个 param')

  await modal.getByRole('button', { name: /取\s*消/ }).click()
})

test('调度作业 - 历史 modal 打开并展示结果', async ({ page }) => {
  await login(page)
  await page.goto('/settings')
  await page.waitForLoadState('networkidle')

  // 关掉任何残留 modal
  await page.keyboard.press('Escape')
  await page.waitForTimeout(200)

  // 找最后一个 .job-item (scan_cross_seed 是最末 5 个之一)
  const row = page.locator('.job-item').filter({ hasText: 'scan_cross_seed' })
  await expect(row).toBeVisible()

  // 用 getByRole 准确找「历史」按钮 (antd 渲染可能含空格, 用 name 选项)
  const historyBtn = row.getByRole('button', { name: /历\s*史/ })
  await historyBtn.click({ force: true })

  const modal = page.locator('.ant-modal-content').filter({ hasText: '运行历史' })
  await expect(modal).toBeVisible()
  console.log('  ✓ 历史 modal 打开')

  // 表格加载
  await page.waitForTimeout(1500)
  const tableRows = modal.locator('.ant-table-tbody > tr.ant-table-row')
  const cnt = await tableRows.count()
  console.log(`  历史行数: ${cnt}`)
  expect(cnt).toBeGreaterThan(0)

  // 点第一行查看详情
  await tableRows.first().click()
  await page.waitForTimeout(500)
  await expect(modal.locator('.history-detail')).toBeVisible()
  console.log('  ✓ 点击行展示详情面板')

  await modal.locator('.ant-modal-close').click()
})

test('调度作业 - 历史 modal 展示任务步骤', async ({ page }) => {
  await login(page)
  await page.goto('/settings')
  await page.waitForLoadState('networkidle')
  await page.keyboard.press('Escape')
  await page.waitForTimeout(200)

  const row = page.locator('.job-item').filter({ hasText: 'scan_cross_seed' })
  await row.getByRole('button', { name: /历\s*史/ }).click({ force: true })

  const modal = page.locator('.ant-modal-content').filter({ hasText: '运行历史' })
  await expect(modal).toBeVisible()
  await page.waitForTimeout(1500)

  // 顶部统计条
  const statPills = modal.locator('.stat-pill')
  await expect(statPills.first()).toBeVisible()
  const statCount = await statPills.count()
  expect(statCount).toBeGreaterThanOrEqual(3)
  console.log(`  ✓ 顶部统计 ${statCount} 个 pill`)

  // 点第一行看步骤
  const tableRows = modal.locator('.ant-table-tbody > tr.ant-table-row')
  const cnt = await tableRows.count()
  expect(cnt).toBeGreaterThan(0)
  await tableRows.first().click()
  await page.waitForTimeout(500)

  // 步骤卡片
  const stepCards = modal.locator('.step-card')
  const stepCount = await stepCards.count()
  expect(stepCount).toBeGreaterThan(0)
  console.log(`  ✓ 步骤卡片 ${stepCount} 个`)

  // 步骤状态色 (ok/warn/skip/fail)
  const withStatus = modal.locator('.step-card[class*="step--"]')
  expect(await withStatus.count()).toBe(stepCount)
  console.log('  ✓ 步骤卡片带状态类 (step--ok/warn/skip/fail)')

  // 步骤图标
  const icons = modal.locator('.step-icon')
  expect(await icons.count()).toBe(stepCount)

  // 列表里的步骤小圆点
  const dots = modal.locator('.step-dot')
  const dotCount = await dots.count()
  expect(dotCount).toBeGreaterThan(0)
  console.log(`  ✓ 列表步骤小圆点 ${dotCount} 个`)

  await modal.locator('.ant-modal-close').click()
})

test('调度作业 - 截图', async ({ page }) => {
  await login(page)
  await page.goto('/settings')
  await page.waitForLoadState('networkidle')
  await page.waitForTimeout(500)
  await page.screenshot({
    path: 'test-results/scheduler-tab.png',
    fullPage: true,
  })
  console.log('  ✓ 截图: web/test-results/scheduler-tab.png')
})
