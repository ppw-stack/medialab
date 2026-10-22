import { test, expect } from '@playwright/test'

async function login(page: any) {
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 10000 })
}

test('站点 - 拉取测试弹窗展示结果', async ({ page }) => {
  await login(page)
  await page.goto('/sites')
  await page.waitForLoadState('networkidle')

  // 第一行站点点「拉取测试」
  const firstRow = page.locator('.ant-table-tbody tr').first()
  await firstRow.getByRole('button', { name: /拉取测试/ }).click()

  // modal 打开
  const modal = page.locator('.ant-modal-content').filter({ hasText: '拉取测试' })
  await expect(modal).toBeVisible()

  // 等加载完成（spin 消失）
  await expect(modal.locator('.ant-spin-spinning')).toHaveCount(0, { timeout: 30000 })

  // 结果区渲染：成功/失败 tag + 状态 + 拉取条数 + 耗时
  await expect(modal.getByText(/成功|失败/)).toBeVisible()
  await expect(modal.getByText('拉取条数')).toBeVisible()
  await expect(modal.getByText('耗时')).toBeVisible()

  const status = await modal.locator('.ant-tag + .mono').innerText().catch(() => '')
  console.log(`  ✓ 拉取测试状态: ${status}`)

  // 成功时应有样本标题列表
  const okTag = await modal.locator('.ant-tag-green').count()
  if (okTag > 0) {
    const samples = modal.locator('.feed-test-samples li')
    const n = await samples.count()
    console.log(`  ✓ 样本标题 ${n} 条`)
    expect(n).toBeGreaterThan(0)
  } else {
    const err = await modal.getByText('错误').count()
    console.log(`  ✓ 失败路径：错误区块 ${err > 0 ? '已展示' : '（无错误文案）'}`)
  }

  await modal.getByRole('button', { name: 'Close' }).click()
})

test('站点 - 表格无「间隔」列', async ({ page }) => {
  await login(page)
  await page.goto('/sites')
  await page.waitForLoadState('networkidle')

  // min_interval_minutes 字段已删除，表头不应再出现「间隔」
  const header = page.locator('.ant-table-thead')
  await expect(header.getByText('间隔')).toHaveCount(0)
  console.log('  ✓ 无「间隔」列')
})

/**
 * 删除站点的二次确认文案（2026-10-23）。
 *
 * 删站点现在会**级联**删掉该站的拉取索引 / 用户数据 / 推送队列记录
 * （原先是 409「请先清理」的死胡同）。删数据不可恢复 → 确认弹窗必须
 * 说清楚会删什么（AGENTS.md「前端删除需确认」）。
 *
 * 注意：**不点确认**——e2e 连的是真实 dev 容器 + 真实站点配置，
 * 点了就把真站删了。只验证弹窗文案，然后取消。
 */
test('站点 - 删除二次确认说明会连带删除哪些数据', async ({ page }) => {
  await login(page)
  await page.goto('/sites')
  await page.waitForLoadState('networkidle')

  const firstRow = page.locator('.ant-table-tbody tr').first()
  await firstRow.getByRole('button', { name: '删除' }).click()

  // 确认弹窗出现，且说明文字点明级联删除范围
  // （description 渲染在 .ant-popconfirm-description 里——别假设整块文本，
  //   antd 把 title 和 description 分开渲染）
  const popconfirm = page.locator('.ant-popconfirm').filter({ hasText: '确认删除该站点' })
  await expect(popconfirm).toBeVisible()

  const desc = popconfirm.locator('.ant-popconfirm-description')
  await expect(desc).toContainText('拉取索引')
  await expect(desc).toContainText('用户数据')
  await expect(desc).toContainText('不可恢复')
  console.log('  ✓ 删除确认弹窗说明了级联删除范围')

  // 取消——不删真实站点
  // 注意：antd 的 autoInsertSpace 会在两个汉字之间插空格（"取消" → "取 消"），
  // 所以用正则而不是精确文本匹配。
  await popconfirm.getByRole('button', { name: /取\s*消/ }).click()
  await expect(popconfirm).toBeHidden()
  console.log('  ✓ 已取消，未删除真实站点')
})
