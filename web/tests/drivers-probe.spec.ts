import { test, expect } from '@playwright/test'

/**
 * 存储源页的测连状态展示。
 *
 * 覆盖两条**真实缺陷**的回归：
 *
 * 1. **陈旧状态无提示**——后端的测连缓存只在进程启动时刷新一次，
 *    面板上的「正常」可能已是几小时前的快照。用户看不到时间戳，
 *    会误以为状态是实时的。
 * 2. **load() 覆盖本地状态**——后端缓存里没有的存储源会把本地已有
 *    的状态抹掉（显示退回「未测连」）。
 *
 * 注意：Playwright e2e 连的是真实容器（localhost:8092），CI 不跑
 * （见 AGENTS.md 前端测试章节）。需在本机起 dev 容器后跑。
 */

/** 造一条测连状态记录。 */
function conn(overrides: Record<string, unknown> = {}) {
  return {
    logged_in: true,
    status: 'ok',
    message: '账号正常，可正常获取直链',
    account_id: 'uid-test',
    total_bytes: 100 * 1024 ** 3,
    used_bytes: 10 * 1024 ** 3,
    remain_bytes: 90 * 1024 ** 3,
    can_get_link: true,
    checked_at: bjNow(),
    ...overrides,
  }
}

/**
 * 造**北京时间字符串**（``yyyy-MM-dd HH:mm:ss``）。
 *
 * 后端给的就是这个口径，前端按 ``+08:00`` 解析——用 UTC 会偏 8 小时。
 */
function bjNow(offsetMs = 0): string {
  const d = new Date(Date.now() + offsetMs)
  // 转到 UTC+8 再格式化
  const bj = new Date(d.getTime() + 8 * 60 * 60 * 1000)
  return bj.toISOString().slice(0, 19).replace('T', ' ')
}

/** 造一个存储源。 */
function driver(id: number, name: string, connValue: unknown) {
  return {
    id,
    type: 'p115',
    name,
    device_type: '115android',
    enabled: true,
    credential_masked: '****',
    media_cid: null,
    media_path: null,
    media_cid_label: '115 媒体库 cid',
    output_dir: null,
    video_exts: 'mkv',
    image_exts: '',
    data_exts: '',
    min_size_mb: 0,
    include_subdirs: true,
    last_scan_at: null,
    last_synced_count: 0,
    conn: connValue,
  }
}

async function login(page: any) {
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url: any) => !url.pathname.startsWith('/login'), { timeout: 10000 })
}

test('存储源 - 陈旧的测连状态给出提示', async ({ page }) => {
  await login(page)

  // 拦截 /api/drivers，返回一条**一小时前**测连的状态（已陈旧）
  const stale = conn({ checked_at: bjNow(-60 * 60 * 1000) })
  await page.route('**/api/drivers', async (route: any) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([driver(1, '陈旧源', stale)]),
    })
  })

  await page.goto('/drivers')
  await page.waitForTimeout(800)

  // 状态本身仍显示「正常」
  await expect(page.locator('.status-hint').first()).toContainText('直链正常')

  // 但**额外**给出「已过期」提示 + 测连时间
  const staleHint = page.locator('.status-hint.stale')
  await expect(staleHint).toBeVisible()
  await expect(staleHint).toContainText('状态已过期')
})

test('存储源 - 新的测连状态不给「已过期」提示', async ({ page }) => {
  await login(page)

  await page.route('**/api/drivers', async (route: any) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([driver(1, '新鲜源', conn())]),
    })
  })

  await page.goto('/drivers')
  await page.waitForTimeout(800)

  await expect(page.locator('.status-hint').first()).toContainText('直链正常')
  // 新鲜状态**不**显示过期提示
  await expect(page.locator('.status-hint.stale')).toHaveCount(0)
})

test('存储源 - 后端无缓存时保留本地已有状态（不被抹成「未测连」）', async ({ page }) => {
  await login(page)

  // **组件内**的两次 load()：首次后端有缓存，创建存储源后后端缓存为空。
  // 不能用 page.reload() 触发——那会重挂组件、重置 statuses，测不到合并。
  let call = 0
  await page.route('**/api/drivers', async (route: any) => {
    const method = route.request().method()
    if (method === 'POST') {
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ id: 2, type: 'p115', name: '新源', conn: null }),
      })
      return
    }
    call += 1
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(
        call === 1 ? [driver(1, '源', conn())] : [driver(1, '源', conn()), driver(2, '新源', null)],
      ),
    })
  })
  await page.route('**/api/drivers/device-types', async (route: any) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([{ value: '115android', label: '115安卓', qr_capable: true, note: '' }]),
    })
  })

  await page.goto('/drivers')
  await page.waitForTimeout(800)
  await expect(page.locator('.status-hint').first()).toContainText('直链正常')

  // 创建存储源 → onCreate 成功后调 load()（第二次，后端 conn=null）
  await page.getByRole('button', { name: '新增' }).click()
  await page.getByPlaceholder('我的 115').fill('新源')
  await page.locator('.ant-modal-footer').getByRole('button', { name: /创建/ }).click()
  await page.waitForTimeout(800)

  // 已有源的状态**仍然在**（没有被 conn=null 的响应抹掉）
  await expect(page.locator('.status-hint').first()).toContainText('直链正常')
})
