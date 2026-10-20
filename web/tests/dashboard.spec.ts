import { test, expect } from '@playwright/test'

async function login(page: any) {
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 10000 })
}

test('总览 - 3 个统计卡显示数字而非占位符', async ({ page }) => {
  await login(page)
  await page.goto('/')
  await page.waitForLoadState('networkidle')

  // 「免费即将截止」与「索引总条数」是纯 DB 统计，必然有值
  const endingSoon = page.locator('.stat-tile').filter({ hasText: '免费即将截止' })
  await expect(endingSoon).toBeVisible()
  const endingSoonVal = await endingSoon.locator('.stat-tile__value').innerText()
  expect(endingSoonVal.trim()).not.toBe('—')
  expect(Number(endingSoonVal.trim())).toBeGreaterThanOrEqual(0)
  console.log(`  ✓ 免费即将截止: ${endingSoonVal.trim()}`)

  const indexTotal = page.locator('.stat-tile').filter({ hasText: '索引总条数' })
  await expect(indexTotal).toBeVisible()
  const indexTotalVal = await indexTotal.locator('.stat-tile__value').innerText()
  expect(indexTotalVal.trim()).not.toBe('—')
  expect(Number(indexTotalVal.trim())).toBeGreaterThan(0)
  console.log(`  ✓ 索引总条数: ${indexTotalVal.trim()}`)

  // 「启用站点」/「启用下载器」原本就有值，顺带回归
  const sites = page.locator('.stat-tile').filter({ hasText: '启用站点' })
  await expect(sites).toBeVisible()
  const sitesVal = await sites.locator('.stat-tile__value').innerText()
  expect(sitesVal.trim()).not.toBe('—')
  console.log(`  ✓ 启用站点: ${sitesVal.trim().replace(/\s+/g, ' ')}`)
})

test('总览 - 下载器在途区块在有数据时渲染', async ({ page }) => {
  await login(page)
  await page.goto('/')
  await page.waitForLoadState('networkidle')

  // inflight_torrents 依赖下载器可达；不可达时后端返回 null，前端不渲染该区块。
  // 两种情况都算通过——只要区块与 API 语义一致。
  const inflightSection = page.locator('section').filter({ hasText: '下载器在途' })
  const visible = await inflightSection.isVisible().catch(() => false)
  const resp = await page.request.get('/api/v1/dashboard')
  const data = await resp.json()

  if (data.inflight_torrents === null || data.inflight_torrents === undefined) {
    expect(visible).toBe(false)
    console.log('  ✓ inflight 不可用 → 区块不渲染（降级一致）')
  } else {
    expect(visible).toBe(true)
    const val = await inflightSection.locator('.stat-tile__value').innerText()
    expect(val.trim()).toBe(String(data.inflight_torrents))
    console.log(`  ✓ 下载器在途: ${val.trim()}`)
  }
})

test('总览 - 调度作业列表显示 6 个作业', async ({ page }) => {
  await login(page)
  await page.goto('/')
  await page.waitForLoadState('networkidle')

  const rows = page.locator('.job-row')
  await expect(rows).toHaveCount(6)
  console.log('  ✓ 调度作业 6 行')
})
