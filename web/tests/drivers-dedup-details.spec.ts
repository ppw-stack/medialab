import { test, expect } from '@playwright/test'

/**
 * 存储源页的扫描进度弹框——**去重明细**的展示。
 *
 * 覆盖一条真实缺陷的回归（2026-11-14 审查）：
 *
 * **去重明细塞进 errors** ——去重是**正常业务结果**（同名不同扩展名
 * 保留大的），不是错误。它挤在「失败明细」里既会占满配额、把真正的
 * 失败挤掉，前端也没法单独展示。修复后走独立通道
 * ``dedup_details``，前端默认折叠展示（展开会撑高详情面板）。
 *
 * 注意：Playwright e2e 连的是真实容器（localhost:8092），CI 不跑
 * （见 AGENTS.md 前端测试章节）。需在本机起 dev 容器后跑。
 */

async function login(page: any) {
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url: any) => !url.pathname.startsWith('/login'), { timeout: 10000 })
}

/** 造一条扫描任务。 */
function scanJob(overrides: Record<string, unknown> = {}) {
  return {
    id: 'job-1',
    driver_id: 1,
    driver_name: '测试源',
    state: 'success',
    started_at: Math.floor(Date.now() / 1000) - 30,
    finished_at: Math.floor(Date.now() / 1000),
    elapsed_sec: 30,
    current_path: '',
    processed: 120,
    dirs: 3,
    created: 100,
    updated: 5,
    unchanged: 15,
    attachment_synced: 0,
    attachment_unchanged: 0,
    attachment_failed: 0,
    skipped_unknown: 0,
    skipped_small: 0,
    deduped: 1,
    pruned: 0,
    failed: 0,
    dedup_details: [
      '同名不同扩展名去重：/影视/movie.mkv（100 字节）与 /影视/movie.mp4（200 字节）映射到同一 strm，保留较大的',
    ],
    logs: ['扫描完成'],
    error: null,
    ...overrides,
  }
}

test('存储源 - 去重明细默认折叠，展开可见', async ({ page }) => {
  await login(page)

  await page.route('**/api/drivers', async (route: any) => {
    if (route.request().method() === 'POST') {
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ id: 1, type: 'p115', name: '测试源' }),
      })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([]),
    })
  })

  // 点「扫描」→ 后端返回 job_id → 轮询拿到带去重明细的任务
  await page.route('**/api/drivers/1/scan', async (route: any) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ job_id: 'job-1' }),
    })
  })
  await page.route('**/api/scan-jobs/job-1', async (route: any) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(scanJob()),
    })
  })

  await page.goto('/drivers')
  await page.waitForTimeout(800)

  // 触发扫描
  const scanBtn = page.getByRole('button', { name: /扫描/ }).first()
  await scanBtn.click()
  await page.waitForTimeout(1500)

  // 去重计数可见
  await expect(page.getByText(/去重 1/)).toBeVisible()

  // 明细**默认折叠**：能点到「展开」按钮，但明细本身不可见
  const toggle = page.getByRole('button', { name: /展开去重明细/ })
  await expect(toggle).toBeVisible()
  await expect(page.locator('.dedup-line')).toHaveCount(0)

  // 展开后可见，且内容点名两个源文件
  await toggle.click()
  await expect(page.locator('.dedup-line')).toHaveCount(1)
  await expect(page.locator('.dedup-line').first()).toContainText('movie.mkv')
  await expect(page.locator('.dedup-line').first()).toContainText('movie.mp4')
})
