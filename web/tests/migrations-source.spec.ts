import { test, expect } from '@playwright/test'

/**
 * 迁移页：源存储源可选**光鸭账号**（光鸭 → 光鸭）。
 *
 * 背景（2026-11-13）：迁移原本只支持 115 → 光鸭。现已支持任意光鸭账号
 * → 另一个光鸭账号（走「直链 → 云下载 → 改名」），所以页面上必须能
 * **选源账号**，且源是光鸭时给出与 115 不同的提示（并发恒 1、落盘要改名、
 * 云添加额度有限）。
 *
 * 注意：Playwright e2e 连的是真实容器（localhost:8080），CI 不跑
 * （见 AGENTS.md 前端测试章节）。需在本机起 dev 容器后跑。
 */

function driver(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    type: 'p115',
    name: '115',
    device_type: null,
    enabled: true,
    credential_masked: '****',
    media_cid: '111',
    media_path: '/影视',
    media_cid_label: '115 媒体库 cid',
    output_dir: null,
    video_exts: 'mkv',
    image_exts: '',
    data_exts: '',
    min_size_mb: 0,
    include_subdirs: true,
    last_scan_at: null,
    last_synced_count: 0,
    conn: null,
    ...overrides,
  }
}

async function login(page: any) {
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url: any) => !url.pathname.startsWith('/login'), { timeout: 10000 })
}

const DRIVERS = [
  driver({ id: 1, type: 'p115', name: '115' }),
  driver({ id: 2, type: 'guangya', name: '光鸭盘', media_cid: '1962124234139361322' }),
  driver({ id: 3, type: 'guangya', name: '小号1' }),
]

test('迁移 - 源可选光鸭账号，目标只列光鸭', async ({ page }) => {
  await login(page)

  await page.route('**/api/drivers', async (route: any) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(DRIVERS),
    })
  })
  await page.route('**/api/migrations/sources**', async (route: any) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ driver_id: 2, parent_id: '', dirs: [] }),
    })
  })
  await page.route('**/api/migrations/targets**', async (route: any) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ driver_id: 2, parent_id: '', dirs: [] }),
    })
  })

  await page.goto('/migrations')
  await page.waitForLoadState('networkidle')

  // 源存储源下拉：115 + 两个光鸭账号都在
  const sourceSelect = page.locator('.driver-row').first().locator('.ant-select')
  await sourceSelect.click()
  await expect(page.locator('.ant-select-dropdown:visible .ant-select-item')).toHaveCount(3)

  // 目标存储源下拉：只有两个光鸭账号
  await page.keyboard.press('Escape')
  const targetSelect = page.locator('.driver-row').nth(1).locator('.ant-select')
  await targetSelect.click()
  await expect(page.locator('.ant-select-dropdown:visible .ant-select-item')).toHaveCount(2)
  await page.keyboard.press('Escape')
})

test('迁移 - 源选光鸭时提示并发恒 1 且提交间隔收紧', async ({ page }) => {
  await login(page)

  await page.route('**/api/drivers', async (route: any) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(DRIVERS),
    })
  })
  await page.route('**/api/migrations/sources**', async (route: any) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ driver_id: 3, parent_id: '', dirs: [] }),
    })
  })
  await page.route('**/api/migrations/targets**', async (route: any) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ driver_id: 2, parent_id: '', dirs: [] }),
    })
  })

  await page.goto('/migrations')
  await page.waitForLoadState('networkidle')

  // 切源到「光鸭盘」
  const sourceSelect = page.locator('.driver-row').first().locator('.ant-select')
  await sourceSelect.click()
  await page.locator('.ant-select-dropdown:visible .ant-select-item', { hasText: '光鸭盘' }).first().click()
  await page.waitForTimeout(500)

  // 标题改成「① 光鸭源目录」
  await expect(page.getByText('① 光鸭源目录')).toBeVisible()
  // 并发/间隔被置灰并说明原因
  await expect(page.getByText('光鸭源恒为 1')).toBeVisible()
  await expect(page.getByText('光鸭源自动收紧到 5s')).toBeVisible()
  // 光鸭源的额度提示
  await expect(page.getByText(/云添加额度有限/)).toBeVisible()
})
