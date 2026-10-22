import { test, expect } from '@playwright/test'

async function login(page: any) {
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 10000 })
}

/** 时魔列（FR-1.6 增补，2026-10-22）。
 *
 * 站点用户数据页要展示「时魔/小时」列——各站单位不同（魔力值 / 爆米花 /
 * 茉莉 / 憨豆），没有时魔的站显示「—」，达上限的站打「已封顶」标签。
 */
test('站点用户数据 - 展示时魔列（单位 / 封顶标记）', async ({ page }) => {
  await login(page)
  await page.goto('/user-data')
  await page.waitForLoadState('networkidle')

  // 表头有时魔列（按表头文字定位列号，别写死第几列——加列会错位）
  // 注：ant-design 的表头 <tr> 没有 class（只有 tbody 行有 ant-table-row）
  const headers = page.locator('.ant-table-thead tr th')
  const idx = await headers.evaluateAll(
    (els) => els.findIndex((e) => e.textContent?.includes('时魔')) + 1
  )
  expect(idx).toBeGreaterThan(0)

  // 表体：每行要么显示数值（带单位），要么显示「—」（该站没有时魔）
  const cells = page.locator(`.ant-table-tbody tr.ant-table-row td:nth-child(${idx})`)
  const n = await cells.count()
  expect(n).toBeGreaterThan(0)

  let withBonus = 0
  for (let i = 0; i < n; i++) {
    const cell = cells.nth(i)
    const text = (await cell.innerText()).trim()
    // 「58.7 憨豆」或「—」
    expect(text === '—' || /\d/.test(text)).toBeTruthy()
    if (text !== '—') withBonus++
    // 封顶标记：每行最多一个「已封顶」标签
    expect(await cell.locator('.ant-tag').count()).toBeLessThanOrEqual(1)
  }
  console.log(`  ✓ ${n} 站，其中 ${withBonus} 站有时魔数据`)

  // 达上限的站带 tooltip（说明封顶状态 / 进度）
  const capped = page.locator('.ant-table-tbody tr.ant-table-row .ant-tag', {
    hasText: '已封顶',
  })
  if ((await capped.count()) > 0) {
    // tooltip 在同一格里的 <span title> 上（不是 tag 的父节点）
    const title = await capped
      .first()
      .locator('xpath=../span[@title]')
      .getAttribute('title')
    expect(title).toBeTruthy()
    expect(title).toContain('上限')
  }
})
