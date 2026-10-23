import { test, expect } from '@playwright/test'

async function login(page: any) {
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 10000 })
}

test('调度作业 - 配置 modal (push_free_torrents params)', async ({ page }) => {
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

  // param 数量**跟 API 对齐**，别写死个数
  // （push_within_bucket_order 加入后是 4 个——写死会在加参数时误报失败。
  //   同 dashboard.spec.ts 的做法：API 才是唯一事实源。）
  const resp = await page.request.get('/api/v1/scheduler/push_free_torrents/config')
  const data = await resp.json()
  const specs = (data.specs ?? []) as { key: string; options?: [number, string][] | null }[]

  // 控件按 param 类型分流：**枚举参数**（options 非空）渲染单选按钮组，
  // 其余（连续数值）渲染数字输入框。断言也按类型分开数——
  // 不能笼统断言"控件数 == param 数"，否则加个枚举参数就会误报失败。
  const enumSpecs = specs.filter((s) => s.options && s.options.length > 0)
  const numberSpecs = specs.filter((s) => !s.options || s.options.length === 0)

  const numbers = modal.locator('.ant-input-number-input')
  await expect(numbers).toHaveCount(numberSpecs.length)
  console.log(`  ✓ push_free_torrents modal 渲染 ${numberSpecs.length} 个数字 param`)

  // 枚举参数 → 单选按钮组
  const radios = modal.locator('.ant-radio-group')
  await expect(radios).toHaveCount(enumSpecs.length)
  for (const spec of enumSpecs) {
    const group = modal.locator('.ant-radio-group').nth(enumSpecs.indexOf(spec))
    await expect(group.locator('.ant-radio-button')).toHaveCount(spec.options!.length)
  }
  console.log(`  ✓ 枚举 param 渲染 ${enumSpecs.length} 个单选组`)

  // cron 输入
  const cronInput = modal.locator('input[placeholder="*/10 * * * *"]')
  await expect(cronInput).toBeVisible()
  const cronVal = await cronInput.inputValue()
  expect(cronVal).toMatch(/^\*\/30 \* \* \* \*$/)
  console.log(`  ✓ cron 默认值: ${cronVal}`)

  // 关闭
  await modal.getByRole('button', { name: /取\s*消/ }).click()
})

test('调度作业 - 枚举 param 用单选按钮组且切换后持久化', async ({ page }) => {
  await login(page)
  await page.goto('/settings')
  await page.waitForLoadState('networkidle')

  const row = page.locator('.job-item').filter({ hasText: 'monitor_free_window' })
  await expect(row).toBeVisible()
  await row.getByRole('button', { name: /配\s*置/ }).click()

  const modal = page.locator('.ant-modal-content').filter({ hasText: '配置 · 免费窗口监测' })
  await expect(modal).toBeVisible()

  // monitor_delete_expired 是 0/1 枚举 → 单选按钮组（不是数字输入框）
  const radio = modal.locator('.ant-radio-group').first()
  await expect(radio).toBeVisible()
  await expect(radio.locator('.ant-radio-button')).toHaveCount(2)
  console.log('  ✓ 枚举 param 渲染为单选按钮组（2 个选项）')

  // 不可逆操作的风险提示必须可见
  await expect(modal.locator('.config-note')).toContainText('不可恢复')
  console.log('  ✓ 不可恢复的风险提示可见')

  // 切到「删除任务+删数据」→ 保存 → 重开确认持久化
  await radio.getByText('删除任务+删数据').click()
  await expect(radio.locator('.ant-radio-button-wrapper-checked')).toHaveText('删除任务+删数据')
  await modal.getByRole('button', { name: /保\s*存/ }).click()
  await page.waitForTimeout(1500)

  await row.getByRole('button', { name: /配\s*置/ }).click()
  const modal2 = page.locator('.ant-modal-content').filter({ hasText: '配置 · 免费窗口监测' })
  await expect(modal2).toBeVisible()
  const radio2 = modal2.locator('.ant-radio-group').first()
  await expect(radio2.locator('.ant-radio-button-wrapper-checked')).toHaveText('删除任务+删数据')
  console.log('  ✓ 单选切换已持久化')

  // 改回默认（仅暂停），不留副作用
  await radio2.getByText('仅暂停').click()
  await modal2.getByRole('button', { name: /保\s*存/ }).click()
  await page.waitForTimeout(1500)
  console.log('  ✓ 已改回默认（仅暂停）')
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

test('调度作业 - 每日签到 (daily_checkin) 出现在作业列表', async ({ page }) => {
  await login(page)
  await page.goto('/settings')
  await page.waitForLoadState('networkidle')

  // daily_checkin 作业行
  const row = page.locator('.job-item').filter({ hasText: 'daily_checkin' })
  await expect(row).toBeVisible()

  // 显示中文名 + 说明
  await expect(row.locator('.job-item__name')).toHaveText('每日签到')
  await expect(row.locator('.job-item__desc')).toContainText('签到')
  console.log('  ✓ daily_checkin 作业行渲染（每日签到）')

  // cron 默认 37 8 * * *（错峰：避开所有多 5 分钟的既有作业）
  const cronText = row.locator('.job-item__cron-text')
  await expect(cronText).toHaveText('37 8 * * *')
  console.log('  ✓ daily_checkin cron 默认 37 8 * * *')

  // 「配置」弹框可用（该作业无专属 runtime param）
  await row.getByRole('button', { name: /配\s*置/ }).click()
  const modal = page.locator('.ant-modal-content').filter({ hasText: '配置 · 每日签到' })
  await expect(modal).toBeVisible()
  await expect(modal.locator('.ant-input-number-input')).toHaveCount(0)
  console.log('  ✓ daily_checkin modal 无 param (符合预期)')
  await modal.getByRole('button', { name: /取\s*消/ }).click()
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
