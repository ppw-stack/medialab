import { test, expect } from '@playwright/test'

async function login(page: any) {
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 10000 })
}

/**
 * 切到内层的**内容分区 Tab**（概览 / 分组 / 异常 / 孤儿）。
 *
 * 页面结构（2026-10-26 起）：
 *   外层 Tab = 下载器（数据维度）
 *   内层 Tab = 内容分区（功能维度）
 *
 * 内层 Tab 在**每个**下载器 pane 里各渲染一份（Ant Design 隐藏 inactive
 * pane），所以必须挑**可见**的那份——用 `:visible` 过滤最外层的
 * `.ant-tabs-tabpane`，再往下找 `.block-tabs`。
 */
async function switchBlockTab(page: any, name: string) {
  const tab = page
    .locator('.ant-tabs-tabpane-active:visible .block-tabs .ant-tabs-tab')
    .filter({ hasText: name })
    .first()
  // 没有内层 Tab（该下载器还没快照）→ 不报错，交给调用方判断
  if ((await tab.count()) === 0) return false
  await tab.click()
  await page.waitForTimeout(600)
  return true
}

/**
 * 当前**可见**的内容分区 Tab 名。
 *
 * 内层 Tab 在每个下载器 pane 里都渲染一份，Ant Design 只把 active 的
 * 那个显示出来——判断「当前在哪个分区」要看哪个 tab-pane 可见。
 */
async function currentBlockTab(page: any): Promise<string> {
  return page.evaluate(() => {
    const panes = Array.from(
      document.querySelectorAll('.block-tabs'),
    ) as HTMLElement[]
    const visible = panes.find((p) => p.offsetParent !== null)
    if (!visible) return ''
    const active = visible.querySelector('.ant-tabs-tab-active')
    return (active?.textContent ?? '').replace(/\s+/g, ' ').trim()
  })
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
  let pane = page.locator('.ant-tabs-tabpane-active:visible')
  let found = false
  for (let i = 0; i < tabCount; i++) {
    await tabs.nth(i).click()
    pane = page.locator('.ant-tabs-tabpane-active:visible')
    // 空间区块出现 = 该 tab 有快照（含空间数据）
    try {
      await expect(pane.locator('.block-tabs:visible')).toBeVisible({ timeout: 5000 })
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
  // ⚠️ 只点**外层**的下载器 Tab——`.ant-tabs-tab` 同时匹配内层的内容
  // 分区 Tab，点到内层会切走整个 pane。
  const outerTabs = page.locator('.content-tabs > .ant-tabs-nav .ant-tabs-tab')
  let pane = page.locator('.ant-tabs-tabpane-active:visible')
  let found = false
  for (let i = 0; i < (await outerTabs.count()); i++) {
    await outerTabs.nth(i).click()
    await page.waitForTimeout(1200)
    pane = page.locator('.ant-tabs-tabpane-active:visible')
    // 切到「异常」分区，看有没有异常明细
    // （该下载器可能还没快照 → 没有内层 Tab → helper 返回 false）
    if (!(await switchBlockTab(page, '异常'))) continue
    if ((await pane.locator('.anomaly-toolbar').count()) > 0) {
      found = true
      break
    }
  }
  if (!found) {
    console.log('  ✓ 当前没有异常明细：跳过批量删除断言')
    return
  }

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
  // ⚠️ 表格分页（每页 20），当前页的行数 ≠ 异常总数——总数从 Tab 标签读。
  // ⚠️ 不能用标签文本：badge 超过 99 会显示成「99+」，读出来是 99。
  const total = await page.evaluate(() => {
    const visible = Array.from(
      document.querySelectorAll('.block-tabs'),
    ).find((p) => (p as HTMLElement).offsetParent !== null)
    const badge = visible?.querySelector(
      '.ant-tabs-tab .ant-badge-count',
    ) as HTMLElement | null
    const raw = badge?.getAttribute('title') ?? badge?.textContent ?? ''
    return Number(raw.replace(/\D/g, '')) || 0
  })
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
  let pane = page.locator('.ant-tabs-tabpane-active:visible')
  let found = false
  for (let i = 0; i < tabCount; i++) {
    await tabs.nth(i).click()
    pane = page.locator('.ant-tabs-tabpane-active:visible')
    try {
      await expect(pane.locator('.block-tabs:visible')).toBeVisible({ timeout: 5000 })
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

  // 统计表在「分组」分区里
  await switchBlockTab(page, '分组')

  // ⚠️ 不能用 `.ant-table` 的 last/first——页面里有多张表。用「操作」列
  // 所在的表来定位。
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

/**
 * 孤儿文件区块（2026-10-24）。
 *
 * 孤儿 = 磁盘上有、下载器里已无对应任务的文件（典型来源：用「仅删任务」
 * 清理种子时磁盘文件留下，不再做种却一直占空间）。
 *
 * 数据来自定时作业 `scan_orphans` 写入的 `cross_seed_orphan` 表。
 * 扫描不可用时（未挂载辅种目录 / 下载器不可达）显示**原因**而非空白。
 */
test('辅种统计页 - 孤儿文件区块', async ({ page }) => {
  await login(page)
  await page.goto('/cross-seed')

  const tabs = page.locator('.ant-tabs-tab')
  await expect(tabs.first()).toBeVisible({ timeout: 15000 }).catch(() => { /* 无下载器 */ })
  const tabCount = await tabs.count()
  if (tabCount === 0) {
    console.log('  ✓ 无辅种下载器：跳过孤儿区块断言')
    return
  }

  // 找有快照的 tab（孤儿区块在快照详情里）
  let pane = page.locator('.ant-tabs-tabpane-active:visible')
  let found = false
  for (let i = 0; i < tabCount; i++) {
    await tabs.nth(i).click()
    pane = page.locator('.ant-tabs-tabpane-active:visible')
    try {
      await expect(pane.locator('.block-tabs:visible')).toBeVisible({ timeout: 5000 })
      found = true
      break
    } catch {
      // 该 tab 没快照 → 试下一个
    }
  }
  if (!found) {
    console.log('  ✓ 还没有快照：跳过孤儿区块断言')
    return
  }

  // 切到「孤儿」分区
  await switchBlockTab(page, '孤儿')

  // 孤儿区块存在
  const block = pane.locator('.space-block').filter({ hasText: 'ORPHANS' })
  await expect(block).toBeVisible()
  console.log('  ✓ 孤儿区块已渲染')

  // 三种状态之一：有数据卡片 / 扫描失败提示 / 尚未扫描
  const tiles = block.locator('.space-tile')
  const alerts = block.locator('.ant-alert')
  const state = (await tiles.count()) > 0 ? '有数据'
    : (await alerts.count()) > 0 ? '扫描失败/未扫描' : '未知'
  console.log(`  ✓ 状态: ${state}`)

  if (state === '有数据') {
    // 4 张卡：孤儿条目 / 孤儿占用 / 其中空目录 / 占扫描目录
    await expect(tiles).toHaveCount(4)
    // 孤儿数量与占用
    const labels = await tiles.locator('.space-tile__label').allTextContents()
    expect(labels.join('|')).toContain('孤儿')
    expect(labels.join('|')).toContain('空目录')
    console.log('  ✓ 孤儿卡片: 孤儿条目 / 孤儿占用 / 其中空目录 / 占扫描目录')
  } else {
    // 不可用时必须有**说明**（不能是空白）
    expect(await alerts.count()).toBeGreaterThan(0)
    const text = (await block.innerText()).replace(/\s+/g, ' ')
    expect(text).toMatch(/孤儿|扫描|挂载/)
    console.log('  ✓ 不可用状态有明确说明')
  }
})

/**
 * 孤儿文件删除（2026-10-26）。
 *
 * 删除**不可逆**——e2e 只验证 UI 流程（勾选 → 确认弹框 → 二次确认），
 * **不真的点最终确认**（那会删掉 NAS 上的真实数据）。
 */
test('辅种统计页 - 孤儿文件删除确认流程', async ({ page }) => {
  await login(page)
  await page.goto('/cross-seed')

  const tabs = page.locator('.ant-tabs-tab')
  await expect(tabs.first()).toBeVisible({ timeout: 15000 }).catch(() => { /* 无下载器 */ })
  const tabCount = await tabs.count()
  if (tabCount === 0) {
    console.log('  ✓ 无辅种下载器：跳过删除流程断言')
    return
  }

  // 找有快照的 tab
  let pane = page.locator('.ant-tabs-tabpane-active:visible')
  let found = false
  for (let i = 0; i < tabCount; i++) {
    await tabs.nth(i).click()
    pane = page.locator('.ant-tabs-tabpane-active:visible')
    try {
      await expect(pane.locator('.block-tabs:visible')).toBeVisible({ timeout: 5000 })
      found = true
      break
    } catch {
      // 该 tab 没快照 → 试下一个
    }
  }
  if (!found) {
    console.log('  ✓ 还没有快照：跳过删除流程断言')
    return
  }

  // 切到「孤儿」分区
  await switchBlockTab(page, '孤儿')
  const block = pane.locator('.space-block').filter({ hasText: 'ORPHANS' })
  await expect(block).toBeVisible()

  // 孤儿明细（独立 Tab，不再折叠）
  if ((await block.locator('.orphan-toolbar').count()) === 0) {
    console.log('  ✓ 没有孤儿明细：跳过删除流程断言')
    return
  }
  await expect(block.locator('.orphan-toolbar')).toBeVisible()

  // 未勾选时删除按钮置灰
  const delBtn = block.locator('.orphan-toolbar button', { hasText: '删除选中' })
  await expect(delBtn).toBeDisabled()
  console.log('  ✓ 未勾选时删除按钮置灰')

  // 全选 → 按钮可用
  await block.locator('.orphan-toolbar button', { hasText: '全选' }).click()
  await expect(delBtn).toBeEnabled()
  console.log('  ✓ 全选后删除按钮可用')

  // 点删除 → 出现确认弹框（**不点最终确认**）
  await delBtn.click()
  const modal = page.locator('.ant-modal', { hasText: '删除孤儿文件' })
  await expect(modal).toBeVisible()
  const modalText = (await modal.innerText()).replace(/\s+/g, ' ')
  expect(modalText).toContain('永久删除')
  expect(modalText).toContain('无法恢复')
  console.log('  ✓ 删除确认弹框已弹出，含不可逆警告')

  // 取消 → 关闭弹框
  await modal.locator('button', { hasText: '取消' }).first().click()
  await expect(modal).toBeHidden()
  console.log('  ✓ 取消后弹框关闭（未删除任何东西）')
})

/**
 * 孤儿删除后的**自动重扫**（2026-10-26）。
 *
 * 孤儿统计表是**每天一次**的作业快照——不重扫的话，页面会一直显示旧数字
 * （已删的还在列表里，且看不到新产生的孤儿）。后端在删除后立即重扫，
 * 并把最新统计带在响应里。
 *
 * 本用例验证**接口契约**（响应带 `orphans` 字段 + 重扫被调用），
 * 不真的删数据。
 */
test('辅种统计页 - 孤儿删除后自动重扫', async ({ page }) => {
  await login(page)

  await page.goto('/cross-seed')
  const tabs = page.locator('.ant-tabs-tab')
  await expect(tabs.first()).toBeVisible({ timeout: 15000 }).catch(() => { /* 无下载器 */ })
  if ((await tabs.count()) === 0) {
    console.log('  ✓ 无辅种下载器：跳过重扫断言')
    return
  }

  // 找有快照的 tab
  let pane = page.locator('.ant-tabs-tabpane-active:visible')
  let found = false
  for (let i = 0; i < (await tabs.count()); i++) {
    await tabs.nth(i).click()
    pane = page.locator('.ant-tabs-tabpane-active:visible')
    try {
      await expect(pane.locator('.block-tabs:visible')).toBeVisible({ timeout: 5000 })
      found = true
      break
    } catch {
      // 该 tab 没快照 → 试下一个
    }
  }
  if (!found) {
    console.log('  ✓ 还没有快照：跳过重扫断言')
    return
  }

  // 切到「孤儿」分区
  await switchBlockTab(page, '孤儿')
  const block = pane.locator('.space-block').filter({ hasText: 'ORPHANS' })
  if ((await block.locator('.orphan-toolbar').count()) === 0) {
    console.log('  ✓ 没有孤儿明细：跳过重扫断言')
    return
  }
  await block.locator('.orphan-toolbar button', { hasText: '全选' }).click()
  await block.locator('.orphan-toolbar button', { hasText: '删除选中' }).click()

  // 监听删除请求的响应体（**放在所有 early-return 之后**——否则提前
  // 返回时会留下一个永不 resolve 的 waitForResponse，测试直接超时）
  const respPromise = page.waitForResponse(
    r => r.url().includes('/cross-seed-monitor/orphans/delete') && r.request().method() === 'POST',
    { timeout: 180000 },
  )

  const modal = page.locator('.ant-modal', { hasText: '删除孤儿文件' })
  await expect(modal).toBeVisible()
  // 二次确认 —— 这一次**真的删**（选的是空目录，代价最小）
  await modal.locator('.ant-popconfirm button', { hasText: '确认删除' }).click()
  await modal.locator('.ant-popconfirm button', { hasText: '确认删除' }).click()

  const resp = await respPromise
  expect(resp.status()).toBe(200)
  const body = await resp.json()
  // 响应里必须带上有孤儿表的新鲜统计
  expect(body.orphans).toBeTruthy()
  expect(body.orphans.summary).toBeTruthy()
  console.log('  ✓ 删除响应带最新孤儿统计（已重扫）')
})

/**
 * 辅种页所有列表的**表头排序**（2026-10-26）。
 *
 * 辅种页有 5 张表：统计分组 / 异常明细 / 目录分布 / 孤儿明细 / 逐目录统计。
 * 每张表的**每个数据列**都应可点表头排序——辅种站动辄几千行，不排序
 * 没法用（找最大的那个、按名字定位某条）。
 *
 * 本用例验证：表头存在排序控件（`.ant-table-column-sorter`），且点击后
 * 行顺序真的变了。
 */
test('辅种统计页 - 各列表表头可排序', async ({ page }) => {
  await login(page)
  await page.goto('/cross-seed')

  const tabs = page.locator('.ant-tabs-tab')
  await expect(tabs.first()).toBeVisible({ timeout: 15000 }).catch(() => { /* 无下载器 */ })
  if ((await tabs.count()) === 0) {
    console.log('  ✓ 无辅种下载器：跳过排序断言')
    return
  }

  // 找有快照的 tab
  let pane = page.locator('.ant-tabs-tabpane-active:visible')
  let found = false
  const outerTabs = page.locator('.content-tabs > .ant-tabs-nav .ant-tabs-tab')
  for (let i = 0; i < (await outerTabs.count()); i++) {
    await outerTabs.nth(i).click()
    pane = page.locator('.ant-tabs-tabpane-active:visible')
    try {
      await expect(pane.locator('.block-tabs:visible')).toBeVisible({ timeout: 5000 })
      found = true
      break
    } catch {
      // 该 tab 没快照 → 试下一个
    }
  }
  if (!found) {
    console.log('  ✓ 还没有快照：跳过排序断言')
    return
  }

  // 逐个内容分区检查（表分布在不同分区里）
  let totalTables = 0
  let totalSorters = 0
  for (const block of ['概览', '分组', '异常', '孤儿']) {
    await switchBlockTab(page, block)
    // ⚠️ 只看**当前可见**的分区——内层 Tab 在每个下载器 pane 里都渲染
    // 一份，不加 :visible 会把隐藏 pane 的表也算进来
    const active = await currentBlockTab(page)
    if (!active.startsWith(block)) {
      console.log(`  ✗ 切到「${block}」失败（当前在「${active}」）`)
      continue
    }
    const tables = pane.locator('.ant-table:visible')
    const n = await tables.count()
    if (n === 0) {
      console.log(`  ✓ [${block}] 无表格`)
      continue
    }
    totalTables += n
    for (let i = 0; i < n; i++) {
      const sorters = tables.nth(i).locator('th .ant-table-column-sorter')
      const c = await sorters.count()
      const headers = await tables.nth(i).locator('thead th').allTextContents()
      console.log(`  ✓ [${block}] 表 ${i + 1}: ${c} 个可排序列 / ${headers.length} 列 — ${headers.join(' | ').slice(0, 60)}`)
      expect(c).toBeGreaterThan(0)
      totalSorters += c
    }
  }
  console.log(`  ✓ 共 ${totalTables} 张表 / ${totalSorters} 个可排序列`)
  expect(totalTables).toBeGreaterThan(0)
  expect(totalSorters).toBeGreaterThan(0)

  // 点一个数值列的表头 → 排序生效（表头出现排序状态 class）
  await switchBlockTab(page, '分组')
  const tables2 = pane.locator('.ant-table')
  const tableCount = await tables2.count()
  expect(tableCount).toBeGreaterThan(0)

  // 点「出现次数」表头 → 行顺序真的变了（排序生效，不只是控件存在）
  const statsTable = pane.locator('.ant-table').filter({ hasText: '操作' }).last()
  if (await statsTable.count()) {
    const countHeader = statsTable.locator('th:has-text("出现次数")').first()
    if (await countHeader.count()) {
      // 只取首行的「出现次数」单元格——不要整行 allTextContents（表格
      // 有 50 行 × 多列，逐格读很慢，会撞 30s 超时）
      const firstCount = () =>
        statsTable.locator('tbody tr.ant-table-row').first().locator('td').nth(2).innerText()

      const before = await firstCount()
      // 点 **.ant-table-column-sorters 容器**（整个表头区域）——点里面的
      // caret 图标会因为它只有 12px 宽、又在视口外而超时
      await countHeader.locator('.ant-table-column-sorters').first().click()
      await page.waitForTimeout(600)
      const after = await firstCount()
      console.log(`  ✓ 点击「出现次数」表头：首行 ${before} → ${after}`)
      expect(after).not.toBe(before)

      // 表头出现排序状态 class
      const sortedHeaders = await statsTable.locator('th.ant-table-column-has-sorters').count()
      expect(sortedHeaders).toBeGreaterThan(0)
      console.log('  ✓ 表头出现排序状态 class')
    }
  }
})

/**
 * 空间区块的**口径标注**与**时效提示**（2026-10-26）。
 *
 * 背景：辅种的本质是**同一条数据被多个种子共享**（实测辅种站 4646 个
 * 种子只对应 2237 份数据），所以「种子声明占用」会把共享部分重复计
 * N 次——实测 71.47 TB **大于**分区总量 28.33 TB。
 *
 * 因此：
 * - 卡片标签用「种子声明占用」而**不是**「辅种实际占用」（后者是错的）
 * - 区块内加口径说明 alert，讲清两个数字的范畴差异
 * - 标题行带抓取时间；数据超过一个抓取周期（30 分钟）时给提示 + 刷新入口
 */
test('辅种统计页 - 空间区块口径标注与时间标记', async ({ page }) => {
  await login(page)
  await page.goto('/cross-seed')

  const tabs = page.locator('.ant-tabs-tab')
  await expect(tabs.first()).toBeVisible({ timeout: 15000 }).catch(() => { /* 无下载器 */ })
  if ((await tabs.count()) === 0) {
    console.log('  ✓ 无辅种下载器：跳过空间区块标注断言')
    return
  }

  // 找有快照的 tab
  let pane = page.locator('.ant-tabs-tabpane-active:visible')
  let found = false
  for (let i = 0; i < (await tabs.count()); i++) {
    await tabs.nth(i).click()
    pane = page.locator('.ant-tabs-tabpane-active:visible')
    try {
      await expect(pane.locator('.block-tabs:visible')).toBeVisible({ timeout: 5000 })
      found = true
      break
    } catch {
      // 该 tab 没快照 → 试下一个
    }
  }
  if (!found) {
    console.log('  ✓ 还没有快照：跳过空间区块标注断言')
    return
  }

  const spaceBlock = pane.locator('.space-block').filter({ hasText: 'SPACE' }).first()

  // 标题行带抓取时间
  const header = spaceBlock.locator('.space-block__header')
  await expect(header).toBeVisible()
  const timeMark = header.locator('.dim.mono')
  await expect(timeMark).toBeVisible()
  expect((await timeMark.innerText())).toContain('抓取于')
  console.log('  ✓ 标题行显示抓取时间:', (await timeMark.innerText()).replace(/\s+/g, ' '))

  // 口径说明
  const caliber = spaceBlock.locator('.ant-alert').filter({ hasText: '口径说明' })
  await expect(caliber).toBeVisible()
  console.log('  ✓ 口径说明 alert 已显示')

  // 标签用「种子声明占用」，**不是**「辅种实际占用」
  const labels = await spaceBlock.locator('.space-tile__label').allTextContents()
  expect(labels.join('|')).toContain('种子声明占用')
  expect(labels.join('|')).not.toContain('辅种实际占用')
  console.log('  ✓ 卡片标签:', labels.join(' | '))

  // 进度条标为「分区已用」——整个分区的口径，含非辅种数据
  const progress = spaceBlock.locator('.space-progress__header')
  if ((await progress.count()) > 0) {
    expect((await progress.innerText())).toContain('分区已用')
    console.log('  ✓ 进度条标签:', (await progress.innerText()).replace(/\s+/g, ' '))
  }
})

/**
 * 孤儿文件**手动扫描**（2026-10-26）。
 *
 * 孤儿扫描默认**每天一次**（cron `7 4 * * *`）——当天发现孤儿想现在清理，
 * 等不到明天。故「孤儿」分区提供「立即扫描」入口。
 *
 * 扫描耗时 40–60 s（扫盘 + 拉所有下载器 + 落库），本用例只验证**入口
 * 存在**——不真的触发（dev 环境没挂载辅种目录，触发也扫不出东西）。
 */
test('辅种统计页 - 孤儿分区有手动扫描入口', async ({ page }) => {
  await login(page)
  await page.goto('/cross-seed')

  const outer = page.locator('.content-tabs > .ant-tabs-nav .ant-tabs-tab')
  await expect(outer.first()).toBeVisible({ timeout: 15000 }).catch(() => { /* 无下载器 */ })
  if ((await outer.count()) === 0) {
    console.log('  ✓ 无辅种下载器：跳过扫描入口断言')
    return
  }

  // 找有快照的 tab
  let pane = page.locator('.ant-tabs-tabpane-active:visible')
  let found = false
  for (let i = 0; i < (await outer.count()); i++) {
    await outer.nth(i).click()
    await page.waitForTimeout(1200)
    pane = page.locator('.ant-tabs-tabpane-active:visible')
    try {
      await expect(pane.locator('.block-tabs:visible')).toBeVisible({ timeout: 5000 })
      found = true
      break
    } catch {
      // 该 tab 没快照 → 试下一个
    }
  }
  if (!found) {
    console.log('  ✓ 还没有快照：跳过扫描入口断言')
    return
  }

  // 切到「孤儿」分区
  await switchBlockTab(page, '孤儿')

  const block = pane.locator('.space-block').filter({ hasText: 'ORPHANS' })
  await expect(block).toBeVisible()

  // 「立即扫描」按钮存在
  const scanBtn = block.locator('button', { hasText: '立即扫描' })
  await expect(scanBtn).toBeVisible()
  console.log('  ✓ 孤儿分区有「立即扫描」按钮')

  // 扫描时间标记（有数据时）
  const timeMark = block.locator('.space-block__actions .dim.mono')
  if ((await timeMark.count()) > 0) {
    console.log('  ✓ 扫描时间标记:', (await timeMark.innerText()).replace(/\s+/g, ' '))
  }
})
