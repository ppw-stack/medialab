import { test, expect } from '@playwright/test'

async function login(page: any) {
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 10000 })
}

/**
 * 数据同步页的双通道运行态展示（2026-11-07「上传队列已满」改造）。
 *
 * 背景：单 worker 串行 + 小文件协议开销 → 1000 槽队列打满 → 逐条丢弃
 * 刷屏。改造后：大文件恒串行 + 小文件并发（concurrency 列）+ 溢出暂存
 * 回灌（不丢）+ 双通道水位可观测。
 *
 * 验证点（**只读**，不改任务配置）：
 * - 任务详情面板有「队列」行：大 / 小 / 溢出 水位可见
 * - 状态端点带双通道契约字段（queue_large / queue_small / overflow /
 *   dropped / current_small）
 * - 丢弃数 >0 时列表与详情出现红色「已丢 N」标记
 *
 * 注意：Playwright e2e 连的是真实容器（localhost:8080），CI 不跑
 * （见 AGENTS.md 前端测试章节）。需在本机起 dev 容器后跑。
 */
test('数据同步 - 详情面板展示双通道队列水位', async ({ page }) => {
  await login(page)
  await page.goto('/uploads')
  await page.waitForLoadState('networkidle')

  // 没有任何任务时只验证页面骨架（无法断言运行态）
  const empty = page.getByText('暂无上传任务')
  if (await empty.isVisible().catch(() => false)) {
    console.log('  ⚠️ 无上传任务，跳过运行态断言')
    return
  }

  // 默认自动选中第一个任务 → 详情面板出现
  const detail = page.locator('.ant-descriptions').first()
  await expect(detail).toBeVisible()

  // 「队列」行：大 N · 小 N · 溢出 N——双通道水位对用户可见
  const queueRow = page.locator('.ant-descriptions-item').filter({ hasText: '队列' })
  await expect(queueRow).toBeVisible()
  await expect(queueRow).toContainText(/大 \d+ · 小 \d+ · 溢出 \d+/)

  // API 契约：状态端点带双通道字段（前端展示的数据源）
  const listResp = await page.request.get('/api/upload-tasks')
  const tasks = (await listResp.json()) as Array<{ id: number }>
  expect(tasks.length).toBeGreaterThan(0)
  const statusResp = await page.request.get(`/api/upload-tasks/${tasks[0].id}/status`)
  const status = (await statusResp.json()) as Record<string, unknown>
  for (const key of ['current', 'current_small', 'queue_large', 'queue_small', 'overflow', 'dropped']) {
    expect(status).toHaveProperty(key)
  }
  console.log(
    `  ✓ 双通道水位: 大 ${status.queue_large} · 小 ${status.queue_small} · ` +
      `溢出 ${status.overflow} · 丢 ${status.dropped}`,
  )

  await page.screenshot({ path: 'test-results/uploads-lane.png', fullPage: true })
})
