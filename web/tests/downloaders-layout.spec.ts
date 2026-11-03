import { test, expect } from '@playwright/test'

async function login(page: any) {
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 10000 })
}

/**
 * 「下载器与任务」页的样式契约。
 *
 * 背景：2026-11-01 的 `544f8f9 feat(downloaders): 合并「下载器」页与「任务」页`
 * 把 `Torrents.vue` 并入 `Downloaders.vue` 时，**只搬了 template + script，
 * 没搬 `<style scoped>`**——原文件里的 `.filter-row` / `.progress-cell` 等
 * 6 个 class 定义就此丢失。页面表现为：筛选栏控件挤成一团、进度条与
 * 百分比上下堆叠。**没有任何闸门能发现这件事**（vue-tsc 不管 CSS、
 * oxlint 也不管 CSS），直到用户截图反馈。
 *
 * 本用例把这些「必须有样式」的 class 固化成断言——配合
 * `web/scripts/check-css-classes.mjs`（CI 闸门）双保险。
 */
test('下载器与任务 - 筛选行 / 进度单元格 / 表头都有正确样式', async ({ page }) => {
  await login(page)
  await page.goto('/downloaders')
  await page.waitForLoadState('networkidle')

  // 没配下载器 → 空状态，页面其余部分（表头等）无法验证
  const empty = await page.locator('.ant-empty, .ant-alert').filter({ hasText: '暂未配置下载器' }).count()
  if (empty > 0) {
    console.log('  ✓ 无下载器实例：跳过（需要至少一个下载器才能验证任务表）')
    return
  }

  // ---- ① 筛选行是 flex 横向排布 ----
  const filterRow = page.locator('.filter-row')
  await expect(filterRow).toBeVisible()
  const filterDisplay = await filterRow.evaluate((el) => getComputedStyle(el).display)
  expect(filterDisplay).toBe('flex')

  // 各控件之间要有间距（gap 生效，而不是挤在一起）
  const gaps = await filterRow.evaluate((el) => {
    const kids = [...el.children].filter((c) => c.getBoundingClientRect().width > 0)
    const xs = kids.map((c) => Math.round(c.getBoundingClientRect().x))
    const out: number[] = []
    for (let i = 1; i < xs.length; i++) out.push(xs[i] - xs[i - 1])
    return out
  })
  console.log(`  ✓ 筛选行 display=flex，子元素间距: ${gaps.join(', ')}`)
  // 相邻控件至少留 8px（--sp-3 = 12px）
  for (const g of gaps) expect(g).toBeGreaterThanOrEqual(8)

  // ---- ② 「总计 N」被推到行尾（margin-left:auto） ----
  const meta = page.locator('.filter-row__meta')
  if ((await meta.count()) > 0) {
    const marginLeft = await meta.evaluate((el) => getComputedStyle(el).marginLeft)
    expect(parseFloat(marginLeft)).toBeGreaterThan(0)
    console.log(`  ✓ 统计数字靠右（margin-left=${marginLeft}）`)
  }

  // ---- ③ 进度条与百分比同一行（flex，不是上下堆叠） ----
  const progressCell = page.locator('.progress-cell').first()
  if ((await progressCell.count()) > 0) {
    const disp = await progressCell.evaluate((el) => getComputedStyle(el).display)
    expect(disp).toBe('flex')

    const ys = await progressCell.evaluate((el) => {
      const bar = el.querySelector('.ant-progress')
      const num = el.querySelector('.progress-cell__num')
      if (!bar || !num) return null
      return {
        bar: Math.round(bar.getBoundingClientRect().y),
        num: Math.round(num.getBoundingClientRect().y),
      }
    })
    if (ys) {
      console.log(`  ✓ 进度条 y=${ys.bar}，百分比 y=${ys.num}（同行）`)
      // 同一行 = y 坐标相差不超过一行文字的高度
      expect(Math.abs(ys.bar - ys.num)).toBeLessThan(20)
    }
  }

  // ---- ④ 表头文字不竖排（窄列的「比例」曾被压成两行） ----
  const tallHeaders = await page.evaluate(() => {
    const out: string[] = []
    document.querySelectorAll('.ant-table-thead th').forEach((th) => {
      const title = th.querySelector('.ant-table-column-title')
      if (!title) return
      const h = title.getBoundingClientRect().height
      // 单行文字高度约 19px；超过 24px 说明换行了
      if (h > 24) out.push(`${th.innerText.trim().replace(/\s+/g, '')}(${Math.round(h)}px)`)
    })
    return out
  })
  console.log(`  ✓ 换行的表头: ${tallHeaders.length ? tallHeaders.join(', ') : '无'}`)
  expect(tallHeaders).toEqual([])
})
