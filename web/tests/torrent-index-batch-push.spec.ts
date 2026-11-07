import { test, expect } from '@playwright/test'

async function login(page: any) {
  await page.goto('/login')
  await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
  await page.locator('input[type="password"]').first().fill('123456')
  await page.getByRole('button', { name: /登|log|sign/i }).first().click()
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 10000 })
}

/**
 * 确保索引里至少有可勾选的行。
 *
 * 索引是**拉取快照**——dev 环境若从没跑过 ``fetch_all_sites``，表是空的，
 * 勾选无从谈起。这里手动触发一次拉取（与面板「立即运行」同一个端点），
 * 拉不到就跳过用例（站点不可达等）。
 */
async function ensureRows(page: any): Promise<number> {
  const count = async () => {
    const resp = await page.request.get('/api/v1/torrent-index?limit=200')
    const body = await resp.json()
    return Array.isArray(body?.items) ? body.items.length : 0
  }

  let n = await count()
  if (n === 0) {
    await page.request.post('/api/v1/scheduler/fetch_all_sites/run')
    for (let i = 0; i < 12 && n === 0; i++) {
      await page.waitForTimeout(5000)
      n = await count()
    }
  }
  return n
}

test('torrent-index - 勾选后可批量推送', async ({ page }) => {
  // 首次运行可能要等一次拉取（8+ 站点）——默认 30s 不够
  test.setTimeout(180_000)
  await login(page)
  await page.goto('/torrent-index')

  const rows = await ensureRows(page)
  test.skip(rows === 0, '索引里没有可勾选的种子（拉取未产出数据）')

  // 拉取是页面加载之后才发生的 → 重新加载让表格拿到新行
  await page.reload()
  await page.waitForTimeout(1500)

  const checkboxes = page.locator('.ant-table-tbody .ant-checkbox-input')
  await expect(checkboxes.first()).toBeVisible({ timeout: 15000 })

  // 未勾选时不显示批量按钮
  await expect(page.getByRole('button', { name: /推送选中/ })).toHaveCount(0)

  // 勾选第一行 → 按钮出现且计数正确
  await checkboxes.first().check()
  const batchBtn = page.getByRole('button', { name: /推送选中/ })
  await expect(batchBtn).toBeVisible()
  await expect(batchBtn).toContainText('推送选中（1）')

  // 打开批量弹框：说明「只入队 + 队列节流」，并列出选中数
  await batchBtn.click()
  const modal = page.locator('.ant-modal:visible').first()
  await expect(modal).toContainText('批量推送 1 个种子')
  await expect(modal).toContainText('已选')
  await expect(modal).toContainText('队列')

  // 取消后勾选仍在（不误清）
  // ⚠️ 必须限定在**可见弹框**内——单推弹框也渲染一个「取消」按钮；
  // ⚠️ Ant 会把两字中文按钮渲染成「取 消」（中间带空格）→ 用正则
  await modal.getByRole('button', { name: /取\s*消/ }).click()
  await expect(page.getByRole('button', { name: /推送选中/ })).toContainText('（1）')

  // 「清除选择」把勾选清空，按钮随之消失
  await page.getByRole('button', { name: '清除选择' }).click()
  await expect(page.getByRole('button', { name: /推送选中/ })).toHaveCount(0)
})

test('torrent-index - 批量推送端点只入队不改推送状态', async ({ page }) => {
  await login(page)

  const resp = await page.request.get('/api/v1/torrent-index?limit=5')
  const body = await resp.json()
  const items: any[] = Array.isArray(body?.items) ? body.items : []
  test.skip(items.length === 0, '索引里没有种子')

  const target = items.find((i) => i.pushable && i.push_status !== 'pushed')
  test.skip(!target, '没有可推送且未推送过的种子')

  const r = await page.request.post('/api/v1/torrent-index/push-batch', {
    data: { ids: [target.id] },
  })
  // 没有可用下载器时是 400（明确报错，不静默失败）
  expect([200, 400]).toContain(r.status())
  if (r.status() === 200) {
    const res = await r.json()
    expect(res.total).toBe(1)
    expect(res.enqueued + res.skipped).toBe(1)
  }

  // 入队**不**改 push_status（执行器执行后才更新）
  const after = await (await page.request.get(`/api/v1/torrent-index/${target.id}`)).json()
  expect(after.push_status).not.toBe('pushed')
})
