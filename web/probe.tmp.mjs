import { chromium } from '@playwright/test'
const base = 'http://localhost:5199'
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1400, height: 1000 }, deviceScaleFactor: 2 })
const dlItems = Array.from({ length: 6 }, (_, i) => ({
  hash: 'a'.repeat(40) + i, name: `Some.Long.Movie.Name.2024.1080p.WEB-DL.${i}.mkv`,
  progress: i / 10, size: 110_000_000_000 + i, downloaded: 40_000_000, uploaded: 0, ratio: 0,
  dlspeed: 3_500, upspeed: 0, state: 'downloading', category: 'auto-push', tags: [],
  save_path: '/share/Public/home/downloads/辅种',
}))
await page.route('**/api/v1/downloaders**', async (route) => {
  const url = route.request().url()
  if (url.includes('/state-meta')) return route.fulfill({ json: [{ value: 'downloading', label: '下载中', color: 'blue' }] })
  if (url.includes('/torrent-count')) return route.fulfill({ json: { count: 22 } })
  if (url.includes('/torrents?')) return route.fulfill({ json: { items: dlItems, total: dlItems.length } })
  if (url.match(/downloaders\/\d+\/torrents/)) return route.fulfill({ json: { items: [], total: 0 } })
  return route.fulfill({ json: [
    { id: 1, name: 'qb', type: 'qbittorrent', url: 'http://192.168.0.172:9090', username: 'admin', save_path: '/share/Public/home/downloads/辅种', is_default: true, enabled: true, has_password: true, is_cross_seed: false },
    { id: 2, name: 'tr', type: 'transmission', url: 'http://192.168.0.172:9091', username: '', save_path: '', is_default: false, enabled: true, has_password: true, is_cross_seed: false },
  ]})
})
await page.goto(base + '/login')
await page.locator('input[type="text"], input[autocomplete="username"]').first().fill('admin')
await page.locator('input[type="password"]').first().fill('123456')
await page.getByRole('button', { name: /登|log|sign/i }).first().click()
await page.waitForURL((u) => !u.pathname.startsWith('/login'), { timeout: 10000 })
await page.goto(base + '/downloaders')
await page.waitForTimeout(2500)

const geom = async (label) => {
  const g = await page.evaluate(() => {
    const out = []
    document.querySelectorAll('.ant-table').forEach((t, ti) => {
      const row = t.querySelector('.ant-table-thead tr')
      const content = t.querySelector('.ant-table-content')
      const tbl = t.querySelector('table')
      const heads = [...row.querySelectorAll('th')].map(th => {
        const inner = th.querySelector('.ant-table-cell-content') || th
        return { txt: th.innerText.trim().replace(/\s+/g,''), h: Math.round(inner.getBoundingClientRect().height) }
      }).filter(h => h.txt)
      out.push({ table: ti, rowH: Math.round(row.getBoundingClientRect().height),
        headHeights: heads.map(h => `${h.txt}:${h.h}`).join(' '),
        tableW: Math.round(tbl.getBoundingClientRect().width),
        containerW: Math.round(content.clientWidth), scrollW: content.scrollWidth,
        hScroll: content.scrollWidth > content.clientWidth })
    })
    return out
  })
  console.log(`\n=== ${label} ===`)
  for (const t of g) console.log(`table${t.table} rowH=${t.rowH} table=${t.tableW} container=${t.containerW} scroll=${t.scrollW} 横向滚动=${t.hScroll}\n   ${t.headHeights}`)
  return g
}

await geom('修复前')
await page.addStyleTag({ content: `.ant-table-thead > tr > th { white-space: nowrap; }` })
await page.waitForTimeout(400)
await geom('方案A: nowrap')
await page.locator('.panel').screenshot({ path: '/private/var/folders/kk/jt598gds7ngbkw53kq2gxbs00000gn/T/opencode/final_th.png' })
await browser.close()
