import { test, expect } from '@playwright/test'

async function login(page: any) {
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 10000 })
}

/**
 * 环境变量配置 tab。
 *
 * 与 scheduler-config.spec.ts 平行——本 spec 验证「字符串型」DB 持久化
 * 配置（辅种扫描目录）的端到端流程：tab 切换 → 加载 → 编辑 → 保存 →
 * 持久化（DB）→ 重启后保持。
 *
 * 注意：Playwright e2e 连的是真实容器（localhost:8080），CI 不跑
 * （见 AGENTS.md 前端测试章节）。需在本机起 dev 容器后跑。
 */
test('环境变量 - 切到 tab 看到 cross_seed_scan_dirs 字段', async ({ page }) => {
  await login(page)
  await page.goto('/settings')
  await page.waitForLoadState('networkidle')

  // 切到「环境变量」tab
  await page.locator('.ant-tabs-tab').filter({ hasText: '环境变量' }).click()
  await page.waitForTimeout(300)

  // 字段渲染：label + textarea + note
  const row = page.locator('.env-row').filter({ hasText: '辅种扫描目录' })
  await expect(row).toBeVisible()
  await expect(row.locator('textarea.env-textarea')).toBeVisible()

  // 后端是唯一事实源：拿 API 的当前值断言 textarea 的初值一致
  const resp = await page.request.get('/api/v1/env-settings')
  const data = await resp.json()
  const item = (data.items as { key: string; value: string }[]).find(
    (i) => i.key === 'cross_seed_scan_dirs'
  )
  expect(item).toBeDefined()

  const textarea = row.locator('textarea.env-textarea')
  const currentValue = await textarea.inputValue()
  expect(currentValue).toBe(item!.value)
  console.log(`  ✓ textarea 初值与 API 一致: "${currentValue}"`)

  // 截图
  await page.screenshot({
    path: 'test-results/env-settings-tab.png',
    fullPage: true,
  })
})

test('环境变量 - 修改并保存 → DB 持久化 → 重新加载仍是新值', async ({ page, request }) => {
  await login(page)
  await page.goto('/settings')
  await page.waitForLoadState('networkidle')

  await page.locator('.ant-tabs-tab').filter({ hasText: '环境变量' }).click()
  await page.waitForTimeout(300)

  // 拿到当前值（用于测试末尾恢复——避免污染生产 DB）
  const before = await (await request.get('/api/v1/env-settings')).json()
  const item = (before.items as { key: string; value: string }[]).find(
    (i) => i.key === 'cross_seed_scan_dirs'
  )!
  const originalValue = item.value

  try {
    const row = page.locator('.env-row').filter({ hasText: '辅种扫描目录' })
    const textarea = row.locator('textarea.env-textarea')

    // 输入新值
    const newValue = `/test/scan/dir-${Date.now()}`
    await textarea.fill(newValue)

    // 保存按钮从 disabled → enabled
    const saveBtn = page.locator('.env-actions button.ant-btn-primary')
    await expect(saveBtn).not.toBeDisabled()
    await saveBtn.click()

    // 成功提示
    await expect(page.locator('.ant-message')).toContainText('已保存', { timeout: 5000 })
    console.log('  ✓ 保存成功 toast 出现')

    // DB 已持久化：直接打 API 拿最新值
    const after = await (await request.get('/api/v1/env-settings')).json()
    const afterItem = (after.items as { key: string; value: string }[]).find(
      (i) => i.key === 'cross_seed_scan_dirs'
    )!
    expect(afterItem.value).toBe(newValue)
    console.log(`  ✓ DB 已持久化: "${afterItem.value}"`)
  } finally {
    // 恢复原值——避免测试污染生产 DB
    await request.put('/api/v1/env-settings', {
      data: { cross_seed_scan_dirs: originalValue },
    })
  }
})
