import { test, expect } from '@playwright/test'

/**
 * 网盘聚合页：组 / 角色 / 水位 / 在途台账。
 *
 * 覆盖三条真实痛点：
 *
 * 1. **副数据源不该出现在媒体库流程里**——副盘是缓冲，不扫描、不生成 strm。
 *    存储源页要给「主 / 副」徽章，且副盘的扫描按钮禁用并说明原因。
 * 2. **额度是硬墙**——光鸭云添加 3 次/账号/天，面板必须让人看见「今日已用 /
 *    上限」，否则用户只看到「文件不动」，不知道为什么。
 * 3. **副盘内容不展示**——面板只报「在途 N 个 / X GB」，不列副盘文件。
 *
 * 注意：Playwright e2e 连的是真实容器（localhost:8092），CI 不跑
 * （见 AGENTS.md 前端测试章节）。需在本机起 dev 容器后跑。
 */

function driver(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    type: 'guangya',
    name: '主号',
    device_type: null,
    enabled: true,
    credential_masked: '****',
    media_cid: 'root',
    media_path: '/影视',
    media_cid_label: '光鸭媒体库 fileId',
    output_dir: '/app/library',
    video_exts: 'mkv',
    image_exts: '',
    data_exts: '',
    min_size_mb: 0,
    include_subdirs: true,
    auto_prune: false,
    last_scan_at: null,
    last_synced_count: 0,
    aggregate_group_id: 1,
    role: 'master',
    staging_root_id: null,
    conn: null,
    ...overrides,
  }
}

const group = {
  id: 1,
  name: '影视聚合',
  enabled: true,
  created_at: '2026-11-15 10:00:00',
  updated_at: '2026-11-15 10:00:00',
}

const stats = {
  group_id: 1,
  group_name: '影视聚合',
  master_driver_id: 1,
  secondary_driver_ids: [2],
  counts: { pending: 2, done: 5 },
  pending_bytes: 1024 * 1024 * 1024,
}

const staging = [
  {
    id: 10,
    group_id: 1,
    source_driver_id: 2,
    target_driver_id: 1,
    remote_id: 'f1',
    file_name: 'ep1.mkv',
    source_dir: '三体 (2023)/Season 1',
    target_dir: '三体 (2023)/Season 1',
    size_bytes: 1024 * 1024 * 1024,
    state: 'pending',
    attempts: 0,
    last_error: null,
    created_at: '2026-11-15 10:00:00',
    updated_at: '2026-11-15 10:00:00',
    synced_at: null,
    cleaned_at: null,
  },
]

async function login(page: any) {
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url: any) => !url.pathname.startsWith('/login'), { timeout: 10000 })
}

test('聚合页 - 展示组 / 水位 / 额度 / 在途台账', async ({ page }) => {
  await login(page)

  await page.route('**/api/aggregate/groups', async (route: any) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([group]),
    })
  })
  await page.route('**/api/aggregate/stats', async (route: any) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([stats]),
    })
  })
  await page.route('**/api/aggregate/limits', async (route: any) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ daily_cloud_add_quota: 3 }),
    })
  })
  await page.route('**/api/aggregate/staging', async (route: any) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(staging),
    })
  })
  await page.route('**/api/drivers', async (route: any) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        driver(),
        driver({
          id: 2,
          name: '副号',
          role: 'secondary',
          aggregate_group_id: 1,
          media_cid: null,
          output_dir: null,
          staging_root_id: 'buf',
        }),
      ]),
    })
  })

  await page.goto('/aggregate')
  await page.waitForTimeout(800)

  // 组名与成员
  await expect(page.getByText('影视聚合')).toBeVisible()
  await expect(page.getByText('主号')).toBeVisible()
  await expect(page.getByText('副号')).toBeVisible()

  // 额度提示（服务端下发常量：约 3 次/账号/天）
  await expect(page.getByText(/云添加额度/)).toBeVisible()
  await expect(page.getByText(/3 次\/账号\/天/)).toBeVisible()

  // 在途台账（副盘文件只在台账里出现）
  await expect(page.getByText('ep1.mkv')).toBeVisible()
  await expect(page.getByText('待归拢')).toBeVisible()
})

test('存储源页 - 副数据源有徽章且不参与扫描', async ({ page }) => {
  await login(page)

  await page.route('**/api/drivers', async (route: any) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        driver(),
        driver({
          id: 2,
          name: '副号',
          role: 'secondary',
          aggregate_group_id: 1,
          media_cid: null,
          output_dir: null,
          staging_root_id: 'buf',
        }),
      ]),
    })
  })

  await page.goto('/drivers')
  await page.waitForTimeout(800)

  // 角色徽章
  await expect(page.getByText('主', { exact: true })).toBeVisible()
  await expect(page.getByText('副', { exact: true })).toBeVisible()

  // 副盘的扫描按钮禁用（内容会归拢到主盘，不进媒体库）
  const disabled = page.locator('button:has-text("扫描")').filter({ hasText: /^扫描$/ })
  await expect(disabled.first()).toBeDisabled()
})
