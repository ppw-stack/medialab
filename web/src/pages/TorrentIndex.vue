<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { api } from '../api'
import type {
  Downloader as DownloaderOpt,
  PushResponse,
  SiteOption as SiteOpt,
  TorrentIndexItem,
  TorrentIndexPage,
  TorrentIndexStats,
} from '../types'
import { formatBytes } from '../utils/format'
import { message } from 'ant-design-vue'






const FREE_TYPE_LABELS: Record<string, { label: string; color: string }> = {
  freeleech_permanent: { label: '永久免费', color: 'cyan' },
  freeleech_timed:     { label: '限时免费', color: 'gold' },
  neutral_leech:       { label: '折扣',     color: 'geekblue' },
  twoup_timed:         { label: '2X 限时',  color: 'orange' },
  refund:              { label: '退还种',   color: 'red' },
  unknown:             { label: '收费',     color: 'default' },
}

const items = ref<TorrentIndexItem[]>([])
const total = ref(0)
const sites = ref<SiteOpt[]>([])
const freeTypes = ref<string[]>([])
const stats = ref<TorrentIndexStats>({ total: 0, by_type: {} })
const downloaders = ref<DownloaderOpt[]>([])

const pushModalOpen = ref(false)
const pushItem = ref<TorrentIndexItem | null>(null)
const pushDownloaderId = ref<number | null>(null)
const pushing = ref(false)
const pushingId = ref<number | null>(null)

const filterSite = ref<number | null>(null)
const filterFreeType = ref<string | null>(null)
const filterQ = ref<string>('')
const sortBy = ref<string>('last_seen_at')
const sortOrder = ref<'asc' | 'desc'>('desc')
const limit = ref(50)
const offset = ref(0)
const loading = ref(false)

async function load(opts?: { reset?: boolean }) {
  loading.value = true
  try {
    const params = new URLSearchParams()
    if (filterSite.value !== null) params.set('site_id', String(filterSite.value))
    if (filterFreeType.value) params.set('free_type', filterFreeType.value)
    if (filterQ.value.trim()) params.set('q', filterQ.value.trim())
    params.set('sort_by', sortBy.value)
    params.set('order', sortOrder.value)
    params.set('limit', String(limit.value))
    params.set('offset', String((opts?.reset ? 0 : offset.value)))

    const page = await api.get<TorrentIndexPage>(
      `/torrent-index?${params.toString()}`,
    )
    if (opts?.reset) offset.value = 0
    items.value = page.items
    total.value = page.total
  } catch { /* ignore */ }
  finally { loading.value = false }
}

async function loadMeta() {
  try {
    const [s, ft] = await Promise.all([
      api.get<SiteOpt[]>('/torrent-index/sites'),
      api.get<string[]>('/torrent-index/free-types'),
    ])
    sites.value = s
    freeTypes.value = ft
  } catch { /* ignore */ }
}

async function loadDownloaders() {
  try {
    const all = await api.get<DownloaderOpt[]>('/downloaders')
    downloaders.value = all.filter((d) => d.enabled)
    const def = downloaders.value.find((d) => d.is_default)
    if (def) pushDownloaderId.value = def.id
    else if (downloaders.value.length > 0) pushDownloaderId.value = downloaders.value[0].id
  } catch { /* ignore */ }
}

function openPushModal(record: TorrentIndexItem) {
  pushItem.value = record
  pushModalOpen.value = true
}

async function doPush() {
  if (!pushItem.value) return
  pushing.value = true
  pushingId.value = pushItem.value.id
  try {
    const r = await api.post<PushResponse>(
      `/torrent-index/${pushItem.value.id}/push`,
      { downloader_id: pushDownloaderId.value },
    )
    if (r.ok) {
      message.success(r.detail || '推送成功')
      pushModalOpen.value = false
      await load({ reset: true })
    } else {
      message.warning(r.detail || '推送失败')
    }
  } catch (e) {
    message.error(e instanceof Error ? e.message : '推送失败')
  } finally {
    pushing.value = false
    pushingId.value = null
  }
}

async function loadStats() {
  try {
    const params = new URLSearchParams()
    if (filterSite.value !== null) params.set('site_id', String(filterSite.value))
    stats.value = await api.get<TorrentIndexStats>(
      `/torrent-index/stats?${params.toString()}`,
    )
  } catch { /* ignore */ }
}

function toggleTypeFilter(ft: string) {
  filterFreeType.value = filterFreeType.value === ft ? null : ft
}

interface Tile {
  key: string
  label: string
  count: number
  hint: string
  active: boolean
}

const STAT_TILES = computed<Tile[]>(() => {
  const bt = stats.value.by_type
  return [
    {
      key: '__total__', label: '总条数', count: stats.value.total,
      hint: 'torrent_index', active: filterFreeType.value === null,
    },
    ...Object.entries(FREE_TYPE_LABELS).map(([key, meta]) => ({
      key,
      label: meta.label,
      count: (bt as Record<string, number>)[key] || 0,
      hint: key,
      active: filterFreeType.value === key,
    })),
  ]
})

function resetFilters() {
  filterSite.value = null
  filterFreeType.value = null
  filterQ.value = ''
  load({ reset: true })
}

function onTableChange(
  pagination: { current?: number; pageSize?: number },
  _filters: unknown,
  sorter: { field?: string; order?: 'ascend' | 'descend' | null } | { field?: string; order?: 'ascend' | 'descend' | null }[],
) {
  const s = Array.isArray(sorter) ? sorter[0] : sorter
  if (s && s.field) {
    sortBy.value = String(s.field)
    sortOrder.value = s.order === 'ascend' ? 'asc' : 'desc'
  } else if (s && s.order === null) {
    sortBy.value = 'last_seen_at'
    sortOrder.value = 'desc'
  }
  if (pagination?.current) {
    offset.value = (pagination.current - 1) * limit.value
  }
  load()
}

watch(filterFreeType, () => load({ reset: true }))
watch(filterSite, () => {
  load({ reset: true })
  loadStats()
})
let searchTimer: ReturnType<typeof setTimeout> | null = null
watch(filterQ, () => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => load({ reset: true }), 400)
})

onMounted(() => {
  loadMeta()
  loadStats()
  loadDownloaders()
  load({ reset: true })
})

onUnmounted(() => {
  if (searchTimer) clearTimeout(searchTimer)
})

/**
 * 种子体积格式化：1 位小数，字节级整数不带小数
 * （"500 B" / "1.5 KB" / "1.0 MB"）。
 */
function formatSize(n: number): string {
  return formatBytes(n, { decimals: 1, integerBytes: true })
}

function formatDate(s: string | null): string {
  if (!s) return '—'
  // 后端返回 "yyyy-MM-dd HH:mm:ss" 北京时间字符串，直接截取显示
  return s.length >= 16 ? s.substring(0, 16) : s
}

function freeTypeLabel(ft: string, discountPct?: number | null) {
  const base = FREE_TYPE_LABELS[ft] ?? { label: ft, color: 'default' }
  if (ft === 'neutral_leech' && discountPct != null && discountPct > 0 && discountPct < 100) {
    return { label: `${discountPct}% 折扣`, color: 'blue' }
  }
  return base
}

interface Countdown {
  ms: number
  label: string
  cls: string
}

function countdownFor(end: string | null): Countdown {
  if (!end) return { ms: Infinity, label: '—', cls: 'countdown--none' }
  // 后端返回北京时间字符串 "yyyy-MM-dd HH:mm:ss"，需加 +08:00 让 JS 正确解析为 UTC 时间
  const d = new Date(end.replace(' ', 'T') + '+08:00')
  // 防御：若后端未来改返回 ISO 8601 UTC（带 Z）或其他格式，
  // `+08:00` 拼接会产出 Invalid Date → NaN
  if (Number.isNaN(d.getTime())) return { ms: Infinity, label: '—', cls: 'countdown--none' }
  const ms = d.getTime() - Date.now()
  if (ms <= 0) return { ms, label: '已截止', cls: 'countdown--urgent' }
  const hours = ms / 3600 / 1000
  let cls = 'countdown--safe'
  if (hours < 1) cls = 'countdown--urgent'
  else if (hours < 6) cls = 'countdown--soon'
  if (hours >= 24) {
    const d = Math.floor(hours / 24)
    const h = Math.floor(hours % 24)
    return { ms, label: `${d}D ${h}H`, cls }
  }
  const h = Math.floor(hours)
  const m = Math.floor((ms / 60 / 1000) % 60)
  return { ms, label: `${h}H ${m.toString().padStart(2, '0')}M`, cls }
}

function freeEndChip(end: string | null, ft: string) {
  if (ft === 'unknown') return { label: '—', cls: 'countdown--none' }
  if (ft === 'freeleech_permanent') return { label: '永久', cls: 'countdown--perm' }
  if (ft === 'refund') return { label: '退还', cls: 'countdown--refund' }
  return countdownFor(end)
}

const columns = [
  { title: '站点', key: 'site_name', dataIndex: 'site_name', width: 110 },
  {
    title: '分类', key: 'category', dataIndex: 'category', width: 130, ellipsis: true,
    sorter: (a: TorrentIndexItem, b: TorrentIndexItem) =>
      (a.category ?? '').localeCompare(b.category ?? ''),
  },
  {
    // **必须给宽度**——不设的话 table-layout:auto 会把它当「弹性列」，
    // 而其它列的固定宽度总和已超过容器宽，结果这一列被压成 0px，
    // 种子名完全看不见。配合 <a-table :scroll="{ x: 1580 }"> 横向滚动。
    title: '种子名', key: 'name', dataIndex: 'name', width: 320, ellipsis: true,
    sorter: (a: TorrentIndexItem, b: TorrentIndexItem) =>
      (a.name ?? '').localeCompare(b.name ?? ''),
  },
  {
    title: '大小', key: 'size', dataIndex: 'size', width: 110,
    sorter: (a: TorrentIndexItem, b: TorrentIndexItem) => (a.size ?? 0) - (b.size ?? 0),
  },
  {
    title: '免费类型', key: 'free_type', dataIndex: 'free_type', width: 120,
    sorter: (a: TorrentIndexItem, b: TorrentIndexItem) =>
      (a.free_type ?? '').localeCompare(b.free_type ?? ''),
  },
  {
    title: '剩余时间', key: 'free_end_at', dataIndex: 'free_end_at', width: 130,
    sorter: (a: TorrentIndexItem, b: TorrentIndexItem) =>
      (a.free_end_at ?? '').localeCompare(b.free_end_at ?? ''),
  },
  {
    title: '发布', key: 'published_at', dataIndex: 'published_at', width: 160, ellipsis: true,
    sorter: (a: TorrentIndexItem, b: TorrentIndexItem) =>
      (a.published_at ?? '').localeCompare(b.published_at ?? ''),
  },
  {
    title: '推送状态', key: 'push_status', dataIndex: 'push_status', width: 100,
    sorter: (a: TorrentIndexItem, b: TorrentIndexItem) =>
      (a.push_status ?? '').localeCompare(b.push_status ?? ''),
  },
  {
    title: '推送时间', key: 'pushed_at', dataIndex: 'pushed_at', width: 150, ellipsis: true,
    sorter: (a: TorrentIndexItem, b: TorrentIndexItem) =>
      (a.pushed_at ?? '').localeCompare(b.pushed_at ?? ''),
  },
  {
    title: '最近拉取', key: 'last_seen_at', dataIndex: 'last_seen_at', width: 150, ellipsis: true,
    sorter: (a: TorrentIndexItem, b: TorrentIndexItem) =>
      (a.last_seen_at ?? '').localeCompare(b.last_seen_at ?? ''),
  },
  { title: '', key: 'action', width: 100 },
]
</script>

<template>
  <div>
    <header class="page-header">
      <div>
        <div class="page-header__eyebrow">拉取索引 · INDEX</div>
        <h1 class="page-header__title">免费种信号池</h1>
        <div class="page-header__sub">自动拉取的种子清单（API/HTML），按免费类型筛选</div>
      </div>
      <div class="mono" style="font-size: var(--text-xs); color: var(--text-tertiary); letter-spacing: 0.06em">
        <span class="live-dot" :class="{ 'live-dot--idle': !loading }" />
        <span style="margin-left: var(--sp-2)">{{ loading ? '拉取中' : '已同步' }}</span>
      </div>
    </header>

    <hr class="divider-hairline" style="margin: 0" />

    <section class="section">
      <div class="label-caps" style="margin-bottom: var(--sp-3)">免费类型分布 · BY TYPE</div>
      <a-row :gutter="[12, 12]">
        <a-col v-for="t in STAT_TILES" :key="t.key" :xs="12" :sm="8" :md="4">
          <div
            class="stat-tile"
            :class="{ 'stat-tile--active': t.active }"
            @click="t.key === '__total__' ? (filterFreeType = null) : toggleTypeFilter(t.key)"
          >
            <div class="stat-tile__label">{{ t.label }}</div>
            <div class="stat-tile__value">{{ t.count }}</div>
            <div class="stat-tile__hint">{{ t.hint }}</div>
          </div>
        </a-col>
      </a-row>
    </section>

    <section class="section">
      <a-alert
        v-if="stats.total === 0 && !loading"
        type="info"
        show-icon
        message="信号池为空"
        description="尚未拉取到免费种。请先在「站点」页配置并启用站点，fetch_all_sites 作业（每 10 分钟）会自动灌入索引。"
        style="margin-bottom: var(--sp-3)"
      />
      <div class="panel">
      <div class="filter-row">
        <a-select
          v-model:value="filterSite"
          placeholder="全部站点"
          style="width: 160px"
          :allow-clear="true"
          size="middle"
        >
          <a-select-option v-for="s in sites" :key="s.id" :value="s.id">
            {{ s.name }}
          </a-select-option>
        </a-select>
        <a-select
          v-model:value="filterFreeType"
          placeholder="全部免费类型"
          style="width: 160px"
          :allow-clear="true"
          size="middle"
        >
          <a-select-option v-for="ft in freeTypes" :key="ft" :value="ft">
            {{ freeTypeLabel(ft).label }} ({{ ft }})
          </a-select-option>
        </a-select>
        <a-input-search
          v-model:value="filterQ"
          placeholder="按名字搜索"
          style="width: 280px"
          :allow-clear="true"
          size="middle"
        />
        <a-button size="middle" @click="resetFilters">重置筛选</a-button>
        <span class="mono filter-row__meta">
          共 <b style="color: var(--text-primary)">{{ total }}</b> 条
        </span>
      </div>

      <a-table
        :data-source="items"
        :columns="columns"
        :loading="loading"
        :pagination="{
          current: Math.floor(offset / limit) + 1,
          pageSize: limit,
          total: total,
          showSizeChanger: false,
        }"
        :on-change="onTableChange"
        :scroll="{ x: 1580 }"
        row-key="id"
        size="middle"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'site_name'">
            <span class="site-tag mono">{{ record.site_name }}</span>
          </template>
          <template v-else-if="column.key === 'published_at'">
            <a-tooltip v-if="record.published_at" :title="record.published_at">
              <span class="mono dim">{{ formatDate(record.published_at) }}</span>
            </a-tooltip>
            <span v-else class="dim">—</span>
          </template>
          <template v-else-if="column.key === 'category'">
            <span v-if="record.category">{{ record.category }}</span>
            <span v-else class="dim">—</span>
          </template>
          <template v-else-if="column.key === 'name'">
            <a-tooltip :title="record.name">
              <a-typography-text
                :ellipsis="true"
                :content="record.name"
                style="max-width: 480px"
              />
            </a-tooltip>
          </template>
          <template v-else-if="column.key === 'size'">
            <span v-if="record.size > 0" class="mono">{{ formatSize(record.size) }}</span>
            <span v-else class="dim">—</span>
          </template>
          <template v-else-if="column.key === 'free_type'">
            <a-tag :color="freeTypeLabel(record.free_type, record.discount_pct).color">
              {{ freeTypeLabel(record.free_type, record.discount_pct).label }}
            </a-tag>
          </template>
          <template v-else-if="column.key === 'free_end_at'">
            <span class="countdown" :class="freeEndChip(record.free_end_at, record.free_type).cls">
              {{ freeEndChip(record.free_end_at, record.free_type).label }}
            </span>
          </template>
          <template v-else-if="column.key === 'push_status'">
            <a-tag v-if="record.push_status === 'pushed'" color="green">已推送</a-tag>
            <a-tag v-else-if="record.push_status === 'failed'" color="red">失败</a-tag>
            <a-tag v-else-if="record.push_status" color="default">{{ record.push_status }}</a-tag>
            <span v-else class="dim">—</span>
          </template>
          <template v-else-if="column.key === 'pushed_at'">
            <a-tooltip v-if="record.pushed_at" :title="record.pushed_at">
              <span class="mono dim">{{ formatDate(record.pushed_at) }}</span>
            </a-tooltip>
            <span v-else class="dim">—</span>
          </template>
          <template v-else-if="column.key === 'last_seen_at'">
            <a-tooltip v-if="record.last_seen_at" :title="record.last_seen_at">
              <span class="mono dim">{{ formatDate(record.last_seen_at) }}</span>
            </a-tooltip>
            <span v-else class="dim">—</span>
          </template>
          <template v-else-if="column.key === 'action'">
            <a-tooltip :title="record.pushable ? '' : '该种无可用的下载链接'">
              <a-button
                type="link"
                size="small"
                :disabled="!record.pushable"
                :loading="pushingId === record.id"
                @click="openPushModal(record)"
              >
                推送 →
              </a-button>
            </a-tooltip>
          </template>
        </template>
      </a-table>
      </div>
    </section>

    <a-modal
      v-model:open="pushModalOpen"
      title="推送种子到下载器"
      ok-text="推送"
      cancel-text="取消"
      :confirm-loading="pushing"
      @ok="doPush"
    >
      <p class="push-modal-name">
        <b>{{ pushItem?.name }}</b>
      </p>
      <a-form layout="vertical">
        <a-form-item label="目标下载器">
          <a-select
            v-model:value="pushDownloaderId"
            placeholder="选择下载器"
            style="width: 100%"
            :options="downloaders.map((d) => ({ value: d.id, label: d.name + (d.is_default ? '（默认）' : '') }))"
          />
        </a-form-item>
      </a-form>
      <a-alert
        v-if="downloaders.length === 0"
        type="warning"
        show-icon
        message="暂无可用的下载器，请先在「下载器」页添加"
      />
    </a-modal>
  </div>
</template>

<style scoped>
.filter-row {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  margin-bottom: var(--sp-3);
  flex-wrap: wrap;
}

.filter-row__meta {
  margin-left: auto;
  font-size: var(--text-xs);
  color: var(--text-secondary);
}

.site-tag {
  display: inline-block;
  padding: 1px 6px;
  border-radius: var(--r-sm);
  background: var(--bg-inset);
  font-size: var(--text-xs);
  letter-spacing: 0.05em;
  color: var(--text-secondary);
}

.dim {
  color: var(--text-tertiary);
}

.push-modal-name {
  margin-bottom: var(--sp-3);
  padding-bottom: var(--sp-3);
  border-bottom: 1px solid var(--border-hairline);
}
</style>
