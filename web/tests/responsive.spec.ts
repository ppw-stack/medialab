import { test, expect } from '@playwright/test'

async function login(page: any) {
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 10000 })
}

/** 全站页面清单——响应式检查要覆盖全部页面。 */
const PAGES = [
  '/', '/sites', '/downloaders', '/torrent-index', '/cross-seed', '/settings',
  '/drivers', '/library', '/migrations', '/uploads', '/health', '/dav', '/media-settings',
]

/**
 * 手机端不产生**页面级**横向溢出。
 *
 * 回归（2026-11-04）：此前全站零 `@media`、零断点——
 *   ① 侧栏恒占 280px，375px 视口下内容区只剩 95px（13 个页面实测全是 95px）
 *   ② 表格不配 `:scroll`，Ant 默认 `table-layout:auto` 按内容撑开，
 *      `/sites` 文档宽 1503px、`/downloaders` 1551px（视口只有 375px）
 *   ③ 行内 `style="width: 380px"` 的输入框把视口**撑宽**
 *      （`/media-settings` 的 innerWidth 变成 506px）
 */
test('响应式 - 手机端 375px 无页面级横向溢出', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 667 })
  await login(page)

  const bad: string[] = []
  for (const p of PAGES) {
    await page.goto(p)
    await page.waitForTimeout(1500)
    const g = await page.evaluate(() => ({
      docW: document.documentElement.scrollWidth,
      winW: window.innerWidth,
      contentW: Math.round(
        document.querySelector('.ant-layout-content')?.getBoundingClientRect().width ?? 0,
      ),
    }))
    if (g.docW > g.winW + 1) {
      bad.push(`${p}(文档宽 ${g.docW} > 视口 ${g.winW})`)
    }
    // 内容区必须拿到接近整个视口的宽度（侧栏不能占位）
    if (g.contentW < 340) {
      bad.push(`${p}(内容区仅 ${g.contentW}px)`)
    }
  }
  console.log(`  ✓ 375px 检查 ${PAGES.length} 个页面`)
  expect(bad).toEqual([])
})

test('响应式 - 手机端侧栏隐藏 + 抽屉可开', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 667 })
  await login(page)
  await page.goto('/sites')
  await page.waitForTimeout(1500)

  // 侧栏完全隐藏（不是收成 0 宽——那会让菜单内容溢出到页面左上角）
  const siderDisplay = await page.locator('.ant-layout-sider').evaluate(
    (el) => getComputedStyle(el).display,
  )
  expect(siderDisplay).toBe('none')
  console.log('  ✓ 手机端侧栏 display:none')

  // header 汉堡按钮 → 抽屉
  const burger = page.locator('.header-menu-btn')
  await expect(burger).toBeVisible()
  await burger.click()
  await page.waitForTimeout(600)

  const drawer = page.locator('.ant-drawer')
  await expect(drawer).toBeVisible()
  console.log('  ✓ 汉堡按钮打开抽屉')

  // 抽屉里要有菜单项
  const menuItems = await page.locator('.ant-drawer .ant-menu-item').count()
  expect(menuItems).toBeGreaterThan(0)
  console.log(`  ✓ 抽屉内 ${menuItems} 个菜单项`)

  // 点菜单项 → 路由切换 + 抽屉关闭
  await page.locator('.ant-drawer .ant-menu-item').first().click()
  await page.waitForTimeout(800)
  expect(await page.locator('.ant-drawer-open').count()).toBe(0)
  console.log('  ✓ 点菜单后抽屉自动关闭')
})

test('响应式 - 桌面端侧栏常驻且页面本身不横向溢出', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await login(page)
  await page.goto('/sites')
  await page.waitForTimeout(1500)

  // 桌面端侧栏必须是展开的（宽 280px）
  const siderWidth = await page.locator('.ant-layout-sider').evaluate(
    (el) => Math.round(el.getBoundingClientRect().width),
  )
  expect(siderWidth).toBeGreaterThan(200)
  console.log(`  ✓ 桌面端侧栏 ${siderWidth}px`)

  // 汉堡按钮不该出现
  expect(await page.locator('.header-menu-btn').count()).toBe(0)

  // 页面**本身**不该横向溢出（表格内部横滚是允许的——13 列装不下时
  // 横滚是正确的；不能出现的是「整页被表格顶宽」）
  const doc = await page.evaluate(() => ({
    docW: document.documentElement.scrollWidth,
    winW: window.innerWidth,
  }))
  expect(doc.docW).toBeLessThanOrEqual(doc.winW + 1)
  console.log(`  ✓ 桌面端页面不横向溢出（${doc.docW} ≤ ${doc.winW}）`)
})

test('响应式 - 每个页面都有统一的页面标题', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await login(page)

  for (const p of PAGES) {
    await page.goto(p)
    await page.waitForTimeout(1200)
    const g = await page.evaluate(() => ({
      headers: document.querySelectorAll('.page-header').length,
      h1: document.querySelector('h1')?.textContent?.trim() ?? '',
    }))
    // 统一骨架：每个页面恰好一个 PageHeader + 一个 h1 标题
    expect(g.headers, `${p} 缺 PageHeader`).toBe(1)
    expect(g.h1.length, `${p} 缺 h1 标题`).toBeGreaterThan(0)
  }
  console.log(`  ✓ ${PAGES.length} 个页面都有统一的页面标题`)
})
