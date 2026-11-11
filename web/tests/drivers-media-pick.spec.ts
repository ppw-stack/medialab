import { test, expect } from '@playwright/test'

/**
 * 存储源页：媒体库目录「浏览选择」+ 扫描按钮的**禁用原因提示**。
 *
 * 覆盖两条真实痛点：
 *
 * 1. **禁用没有原因**——扫描按钮的禁用条件是「媒体库目录 / 本地媒体库目录
 *    缺一」，但灰按钮不吭声，用户只知道「点不动」，不知道该去补什么。
 * 2. **手填 id**——光鸭的 fileId 是 19 位数字（115 的 cid 同理），
 *    让用户照着网盘网页抄——抄错一位扫描就扫不到东西，且无从核对。
 *
 * 注意：Playwright e2e 连的是真实容器（localhost:8092），CI 不跑
 * （见 AGENTS.md 前端测试章节）。需在本机起 dev 容器后跑。
 */

/** 造一条存储源。 */
function driver(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    type: 'guangya',
    name: '光鸭盘',
    device_type: null,
    enabled: true,
    credential_masked: '****',
    media_cid: null,
    media_path: null,
    media_cid_label: '光鸭媒体库 fileId',
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

test('存储源 - 扫描按钮禁用时说明缺什么', async ({ page }) => {
  await login(page)

  // media_cid / output_dir 都没配 → 扫描按钮必然禁用
  await page.route('**/api/drivers', async (route: any) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([driver()]),
    })
  })

  await page.goto('/drivers')
  await page.waitForTimeout(800)

  // Ant 会给**两个字**的按钮加 letter-spacing——accessible name 是
  // 「扫 描」（中间有空格），故用正则而非精确文本定位。
  const scanBtn = page.getByRole('button', { name: /扫\s*描/ })
  await expect(scanBtn).toBeDisabled()

  // 悬停出提示，并**点名**缺哪一项（光鸭叫 fileId，不叫 cid）
  await scanBtn.hover({ force: true })
  await expect(page.getByText(/尚未配置光鸭媒体库 fileId/).first()).toBeVisible()
  await expect(page.getByText(/本地媒体库目录/).first()).toBeVisible()
})

test('存储源 - 媒体库目录用目录浏览器选，不手填 id', async ({ page }) => {
  await login(page)

  await page.route('**/api/drivers', async (route: any) => {
    if (route.request().method() === 'POST') {
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify(driver()),
      })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([driver()]),
    })
  })

  // 目录浏览：根层给两个目录；「电影」下面再给一个子目录
  await page.route('**/api/drivers/1/browse-dirs**', async (route: any) => {
    const url = route.request().url()
    const parent = new URL(url).searchParams.get('parent_id') ?? ''
    const dirs =
      parent === ''
        ? [
            { id: 'd1', name: '电影' },
            { id: 'd2', name: '剧集' },
          ]
        : [{ id: 'd3', name: '纪录片' }]
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ driver_id: 1, parent_id: parent, dirs }),
    })
  })

  // 捕获保存请求的 payload
  let saved: any = null
  await page.route('**/api/drivers/1/config', async (route: any) => {
    saved = route.request().postDataJSON()
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(driver({ media_cid: 'd1', output_dir: '/app/library' })),
    })
  })

  await page.goto('/drivers')
  await page.waitForTimeout(800)

  await page.getByRole('button', { name: /配\s*置/ }).first().click()

  // 抽屉里是**目录浏览器**，不是 id 输入框
  await expect(page.getByText('网盘媒体库目录')).toBeVisible()
  await expect(
    page.locator('.ant-drawer input[placeholder="/app/library"]'),
  ).toHaveCount(1)

  // 点进「电影」→ 即选中它
  await page.getByText('电影', { exact: true }).click()
  await expect(page.getByText(/已选：电影/).first()).toBeVisible()
  // 进入后加载的是**子**目录（逐层懒加载）
  await expect(page.getByText('纪录片', { exact: true })).toBeVisible()

  // 补上本地媒体库目录后保存
  await page.locator('.ant-drawer input[placeholder="/app/library"]').fill('/app/library')
  await page.getByRole('button', { name: /保\s*存/ }).click()
  await page.waitForTimeout(800)

  // 保存的 media_cid 是**选出来的 id**，不是手填的
  expect(saved?.media_cid).toBe('d1')
  expect(saved?.output_dir).toBe('/app/library')
})
