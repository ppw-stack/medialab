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

/**
 * 辅种空间占用区块（2026-10-24）。
 *
 * 数据来自定时抓取的快照（`cross_seed_snapshot` 的空间列）——
 * 辅种实际占用 / 磁盘剩余 / 磁盘总量 / 按目录分布。
 *
 * qBittorrent 拿不到磁盘总量（`total_bytes` 为 NULL）→ 前端不画进度条
 * （画出来会误导成「磁盘已满」）。
 */
test('辅种统计页 - 空间占用区块', async ({ page }) => {
  await login(page)
  await page.goto('/cross-seed')

  const tabs = page.locator('.ant-tabs-tab')
  // 下载器列表是异步加载的——等它渲染出来再判断
  await expect(tabs.first()).toBeVisible({ timeout: 15000 }).catch(() => { /* 无下载器 */ })
  if ((await tabs.count()) === 0) {
    console.log('  ✓ 无辅种下载器：跳过空间区块断言')
    return
  }

  // 逐个 tab 找**有快照**的那个（没快照的 tab 显示「等待首次抓取」，
  // 没有空间区块可断言）。
  // ⚠️ 详情是**异步**加载的——用 expect 轮询等它出现（isVisible 不轮询，
  // 会在数据到达前就返回 false → 误判成「没有快照」）。
  const tabCount = await tabs.count()
  let pane = page.locator('.ant-tabs-tabpane-active')
  let found = false
  for (let i = 0; i < tabCount; i++) {
    await tabs.nth(i).click()
    pane = page.locator('.ant-tabs-tabpane-active')
    // 空间区块出现 = 该 tab 有快照（含空间数据）
    try {
      await expect(pane.locator('.space-block')).toBeVisible({ timeout: 5000 })
      found = true
      break
    } catch {
      // 该 tab 没快照 → 试下一个
    }
  }
  if (!found) {
    console.log('  ✓ 还没有快照（含空间数据）：跳过空间区块断言')
    return
  }

  // 空间区块（4 张卡片）
  const spaceBlock = pane.locator('.space-block')
  await expect(spaceBlock).toBeVisible()
  await expect(spaceBlock.locator('.space-tile')).toHaveCount(4)
  console.log('  ✓ 空间卡片: 实际占用 / 总大小 / 磁盘剩余 / 磁盘总量')

  // 总量未知时不画进度条（qB）
  const hasProgress = await spaceBlock.locator('.space-progress').count()
  const totalTile = spaceBlock.locator('.space-tile').nth(3)
  const totalText = (await totalTile.locator('.space-tile__value').textContent())?.trim()
  if (totalText === '—') {
    expect(hasProgress).toBe(0)
    console.log('  ✓ 磁盘总量未知 → 不画进度条')
  } else {
    expect(hasProgress).toBe(1)
    console.log('  ✓ 磁盘总量已知 → 画进度条')
  }

  // 目录分布表（有目录信息时）
  const dirRows = await spaceBlock.locator('.ant-table-tbody tr.ant-table-row').count()
  console.log(`  ✓ 目录分布行数: ${dirRows}`)
})

/**
 * 异常种子明细的**批量删除**（2026-10-24）。
 *
 * 异常种子（tracker 未注册 / 数据丢失）已无价值，批量清理能腾空间、
 * 减少无效连接。删除走「任务」页同一套语义：可选是否连带删磁盘数据。
 *
 * 真实删除会动用户的下载器，故本用例**只验证 UI 交互**（勾选、全选、
 * 确认弹框），**不点最终的删除按钮**。
 */
test('辅种统计页 - 异常明细批量删除 UI', async ({ page }) => {
  await login(page)
  await page.goto('/cross-seed')

  const tabs = page.locator('.ant-tabs-tab')
  await expect(tabs.first()).toBeVisible({ timeout: 15000 }).catch(() => { /* 无下载器 */ })
  const tabCount = await tabs.count()
  if (tabCount === 0) {
    console.log('  ✓ 无辅种下载器：跳过批量删除断言')
    return
  }

  // 找有异常明细的 tab
  let pane = page.locator('.ant-tabs-tabpane-active')
  let found = false
  for (let i = 0; i < tabCount; i++) {
    await tabs.nth(i).click()
    pane = page.locator('.ant-tabs-tabpane-active')
    await page.waitForTimeout(800)
    if ((await pane.locator('.ant-collapse-header:has-text("异常种子明细")').count()) > 0) {
      found = true
      break
    }
  }
  if (!found) {
    console.log('  ✓ 当前没有异常明细：跳过批量删除断言')
    return
  }

  // 展开异常明细
  await pane.locator('.ant-collapse-header:has-text("异常种子明细")').click()
  await page.waitForTimeout(600)

  const toolbar = pane.locator('.anomaly-toolbar')
  await expect(toolbar).toBeVisible()

  // 「删除选中」初始禁用（没勾选）
  const delBtn = toolbar.locator('button:has-text("删除选中")')
  await expect(delBtn).toBeDisabled()
  console.log('  ✓ 未勾选时删除按钮禁用')

  // 勾选一行 → 按钮可用且计数更新
  await pane.locator('.ant-table-tbody .ant-checkbox-input').first().click()
  await page.waitForTimeout(400)
  await expect(delBtn).toBeEnabled()
  expect((await delBtn.textContent()) ?? '').toContain('1')
  console.log('  ✓ 勾选后按钮可用，计数 = 1')

  // 全选 → 计数 = 全部异常数
  // ⚠️ 表格分页（每页 20），当前页的行数 ≠ 异常总数——总数从折叠标题读
  const headerText =
    (await pane.locator('.ant-collapse-header:has-text("异常种子明细")').textContent()) ?? ''
  const total = Number(headerText.match(/异常种子明细（(\d+)）/)?.[1] ?? 0)
  expect(total).toBeGreaterThan(0)
  await toolbar.locator('button:has-text("全选")').click()
  await page.waitForTimeout(400)
  expect((await delBtn.textContent()) ?? '').toContain(String(total))
  console.log(`  ✓ 全选后计数 = ${total}（跨分页全选）`)

  // 打开确认弹框 → 校验两个删除选项都在
  await delBtn.click()
  const modal = page.locator('.ant-modal-content:has-text("删除种子")')
  await expect(modal).toBeVisible()
  await expect(modal.locator('button:has-text("仅删任务")')).toBeVisible()
  await expect(modal.locator('button:has-text("删任务 + 磁盘数据")')).toBeVisible()
  console.log('  ✓ 确认弹框含「仅删任务」与「删任务 + 磁盘数据」')

  // 取消（**不执行真实删除**）
  // 注：AntD 对两个中文字符的按钮会渲染成「取 消」（中间有空格），
  // 故用正则而非精确文本匹配。
  await modal.getByRole('button', { name: /取\s*消/ }).click()
  await expect(modal).toBeHidden()
})

/**
 * 统计卡片的**删除**（2026-10-24）。
 *
 * 统计表一行 = 一个 (名称, 大小) 分组，可能代表**多个**种子（count=13
 * 就是 13 个）。删除该行 = 删该组全部种子。
 *
 * 这些是**正常做种**的种子（不是异常），误删就没了——故弹框必须写清
 * 「将删除 N 个种子」并给出「仅删任务会留孤儿数据」的提示。
 *
 * 用例**只验证 UI 交互**（按钮、弹框文案），不点最终的删除按钮。
 */
test('辅种统计页 - 统计卡片删除 UI', async ({ page }) => {
  await login(page)
  await page.goto('/cross-seed')

  const tabs = page.locator('.ant-tabs-tab')
  await expect(tabs.first()).toBeVisible({ timeout: 15000 }).catch(() => { /* 无下载器 */ })
  const tabCount = await tabs.count()
  if (tabCount === 0) {
    console.log('  ✓ 无辅种下载器：跳过统计卡片删除断言')
    return
  }

  // 找有快照的 tab
  let pane = page.locator('.ant-tabs-tabpane-active')
  let found = false
  for (let i = 0; i < tabCount; i++) {
    await tabs.nth(i).click()
    pane = page.locator('.ant-tabs-tabpane-active')
    try {
      await expect(pane.locator('.space-block')).toBeVisible({ timeout: 5000 })
      found = true
      break
    } catch {
      // 该 tab 没快照 → 试下一个
    }
  }
  if (!found) {
    console.log('  ✓ 还没有快照：跳过统计卡片删除断言')
    return
  }

  // 统计表（页面下半部分的分组表）
  // ⚠️ 不能用 `.ant-table` 的 last/first——页面里有多张表（目录分布表在
  // 统计表**上面**）。用「操作」列所在的表来定位。
  const statsTable = pane.locator('.ant-table').filter({
    hasText: '操作',
  }).last()
  await expect(statsTable).toBeVisible()

  // 「操作」列存在
  await expect(statsTable.locator('th:has-text("操作")')).toBeVisible()

  const delBtns = statsTable.locator('tbody button:has-text("删 除"), tbody button:has-text("删除")')
  const btnCount = await delBtns.count()
  if (btnCount === 0) {
    console.log('  ✓ 统计表没有可删除的行（可能都是旧快照，缺完整 hash）')
    return
  }

  // 第一个可用的删除按钮 → 打开弹框
  await delBtns.first().click()
  const modal = page.locator('.ant-modal-content:has-text("删除种子")')
  await expect(modal).toBeVisible()

  const text = (await modal.innerText()).replace(/\s+/g, ' ')
  console.log('  ✓ 弹框:', text.slice(0, 220))

  // 必须写清数量与「同名同大小」
  expect(text).toMatch(/即将从下载器中删除选中的\s*\d+/)
  expect(text).toContain('同名同大小')
  // 必须有孤儿数据提示
  expect(text).toContain('孤儿数据')
  // 两个删除选项
  await expect(modal.locator('button:has-text("仅删任务")')).toBeVisible()
  await expect(modal.locator('button:has-text("删任务 + 磁盘数据")')).toBeVisible()
  console.log('  ✓ 弹框含数量、组名说明与孤儿数据提示')

  // 取消（**不执行真实删除**）
  await modal.getByRole('button', { name: /取\s*消/ }).click()
  await expect(modal).toBeHidden()
})
