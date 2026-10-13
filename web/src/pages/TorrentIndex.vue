<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { api } from '../api'

interface TorrentIndexItem {
  id: number
  site_id: number
  site_name: string
  item_id: string | null
  info_hash: string | null
  name: string
  size: number
  category: string | null
  free_type: string
  free_end_at: string | null
  free_source: string | null
  free_confidence: number | null
  discount_pct: number | null
  has_torrent_url: boolean
  first_seen_at: string
  last_seen_at: string
}

interface TorrentIndexPage {
  total: number
  items: TorrentIndexItem[]
}

interface SiteOpt {
  id: number
  name: string
}

const FREE_TYPE_LABELS: Record<string, { label: string; color: string }> = {
  freeleech_permanent: { label: '永久免费', color: 'green' },
  freeleech_timed:     { label: '限时免费', color: 'gold'  },
  neutral_leech:       { label: '折扣',     color: 'blue'  },
  twoup_timed:         { label: '2X 限时',  color: 'orange' },
  unknown:             { label: '未知',     color: 'default' },
}

const items = ref<TorrentIndexItem[]>([])
const total = ref(0)
const sites = ref<SiteOpt[]>([])
const freeTypes = ref<string[]>([])

const filterSite = ref<number | null>(null)
const filterFreeType = ref<string | null>(null)
const filterQ = ref<string>('')
const limit = ref(50)
const offset = ref(0)
const loading = ref(false)

let timer: ReturnType<typeof setInterval> | null = null

async function load(opts?: { reset?: boolean }) {
  loading.value = true
  try {
    const params = new URLSearchParams()
    if (filterSite.value !== null) params.set('site_id', String(filterSite.value))
    if (filterFreeType.value) params.set('free_type', filterFreeType.value)
    if (filterQ.value.trim()) params.set('q', filterQ.value.trim())
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

function resetFilters() {
  filterSite.value = null
  filterFreeType.value = null
  filterQ.value = ''
  load({ reset: true })
}

function onPageChange(p: number) {
  offset.value = (p - 1) * limit.value
  load()
}

watch([filterSite, filterFreeType], () => load({ reset: true }))
let searchTimer: ReturnType<typeof setTimeout> | null = null
watch(filterQ, () => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => load({ reset: true }), 400)
})

onMounted(() => {
  loadMeta()
  load({ reset: true })
  timer = setInterval(() => load({ reset: true }), 5000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
  if (searchTimer) clearTimeout(searchTimer)
})

function formatSize(n: number): string {
  if (n < 1024) return `${n} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let v = n / 1024
  for (const u of units) {
    if (v < 1024) return `${v.toFixed(1)} ${u}`
    v /= 1024
  }
  return `${v.toFixed(1)} PB`
}

function formatDate(s: string | null): string {
  if (!s) return '—'
  const d = new Date(s)
  return d.toLocaleString('zh-CN', { hour12: false })
}

function freeTypeLabel(ft: string, discountPct?: number | null) {
  const base = FREE_TYPE_LABELS[ft] ?? { label: ft, color: 'default' }
  if (ft === 'neutral_leech' && discountPct != null && discountPct > 0 && discountPct < 100) {
    return { label: `${discountPct}% 折扣`, color: 'blue' }
  }
  return base
}

function freeEndColor(end: string | null): string {
  if (!end) return 'green'
  const ms = new Date(end).getTime() - Date.now()
  if (ms < 0) return 'red'
  if (ms < 6 * 3600 * 1000) return 'orange'
  return 'green'
}

const columns = [
  { title: '站点', key: 'site', width: 110 },
  { title: '分类', key: 'category', width: 130, ellipsis: true },
  { title: '种子名', key: 'name', ellipsis: true },
  { title: '大小', key: 'size', width: 100 },
  { title: '免费类型', key: 'free_type', width: 110 },
  { title: '免费截止', key: 'free_end_at', width: 170 },
  { title: 'info_hash', key: 'info_hash', width: 130 },
  { title: '最近拉取', key: 'last_seen_at', width: 170 },
]
</script>

<template>
  <a-card title="拉取索引（torrent_index）">
    <a-space style="margin-bottom: 16px" :wrap="true">
        <a-select
          v-model:value="filterSite"
          placeholder="全部站点"
          style="width: 160px"
          :allow-clear="true"
        >
          <a-select-option
            v-for="s in sites"
            :key="s.id"
            :value="s.id"
          >
            {{ s.name }}
          </a-select-option>
        </a-select>

        <a-select
          v-model:value="filterFreeType"
          placeholder="全部免费类型"
          style="width: 160px"
          :allow-clear="true"
        >
          <a-select-option
            v-for="ft in freeTypes"
            :key="ft"
            :value="ft"
          >
            {{ freeTypeLabel(ft).label }} ({{ ft }})
          </a-select-option>
        </a-select>

        <a-input-search
          v-model:value="filterQ"
          placeholder="按名字 / info_hash 搜索"
          style="width: 280px"
          :allow-clear="true"
        />

        <a-button @click="resetFilters">重置</a-button>
      </a-space>

      <a-typography-paragraph type="secondary" style="margin-top: 0">
        共 <b>{{ total }}</b> 条；本作业只刷新「列表 / 过滤」，无写入操作。
      </a-typography-paragraph>

      <a-table
        :data-source="items"
        :columns="columns"
        :loading="loading"
        :pagination="{
          current: Math.floor(offset / limit) + 1,
          pageSize: limit,
          total: total,
          showSizeChanger: false,
          onChange: onPageChange,
        }"
        row-key="id"
        :scroll="{ x: 1250 }"
        size="middle"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'site'">
            <a-tag color="blue">{{ record.site_name }}</a-tag>
          </template>
          <template v-else-if="column.key === 'category'">
            <a-typography-text v-if="record.category">{{ record.category }}</a-typography-text>
            <a-typography-text v-else type="secondary">—</a-typography-text>
          </template>
          <template v-else-if="column.key === 'name'">
            <a-typography-text :ellipsis="true" style="max-width: 480px">
              {{ record.name }}
            </a-typography-text>
          </template>
          <template v-else-if="column.key === 'size'">
            <template v-if="record.size > 0">
              {{ formatSize(record.size) }}
            </template>
            <a-typography-text v-else type="secondary">—</a-typography-text>
          </template>
          <template v-else-if="column.key === 'free_type'">
            <a-tag :color="freeTypeLabel(record.free_type, record.discount_pct).color">
              {{ freeTypeLabel(record.free_type, record.discount_pct).label }}
            </a-tag>
          </template>
          <template v-else-if="column.key === 'free_end_at'">
            <a-typography-text
              v-if="record.free_type === 'unknown'"
              type="secondary"
            >
              —
            </a-typography-text>
            <a-tag v-else-if="record.free_type === 'freeleech_permanent'" color="green">
              永久
            </a-tag>
            <a-tag v-else :color="freeEndColor(record.free_end_at)">
              {{ formatDate(record.free_end_at) }}
            </a-tag>
          </template>
          <template v-else-if="column.key === 'info_hash'">
            <code v-if="record.info_hash" style="font-size: 12px">
              {{ record.info_hash.slice(0, 16) }}…
            </code>
            <a-typography-text v-else type="secondary">—</a-typography-text>
          </template>
          <template v-else-if="column.key === 'last_seen_at'">
            {{ formatDate(record.last_seen_at) }}
          </template>
        </template>
      </a-table>
  </a-card>
</template>