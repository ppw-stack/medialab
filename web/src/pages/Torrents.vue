<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { api } from '../api'

interface DownloaderInfo {
  id: number
  name: string
  enabled: boolean
}

interface DownloaderTorrent {
  hash: string
  name: string
  state: string
  progress: number
  size: number
  downloaded: number
  uploaded: number
  dlspeed: number
  upspeed: number
  ratio: number
  save_path: string | null
  category: string | null
  tags: string[]
}

interface ListResp {
  total: number
  items: DownloaderTorrent[]
}

const downloaders = ref<DownloaderInfo[]>([])
const activeTab = ref<number | null>(null)
const items = ref<DownloaderTorrent[]>([])
const total = ref(0)
const loading = ref(false)
const page = ref(1)
const pageSize = ref(100)

const filterState = ref<string | null>(null)
const filterCategory = ref<string>('')
const filterQ = ref<string>('')

// 状态 → 中文标签 + 颜色（单一来源）
// qB/Tr API 返回的状态字符串由 downloaders.py 序列化为 .name
// （即大写枚举值如 SEEDING / UPLOADING / DOWNLOADING）。
const STATE_META: Record<string, { label: string; color: string }> = {
  // qBittorrent / Transmission 大小写（实际接收值）
  DOWNLOADING: { label: '下载中', color: 'blue' },
  UPLOADING: { label: '上传中', color: 'green' },
  SEEDING: { label: '做种中', color: 'green' },
  PAUSED_DL: { label: '暂停下载', color: 'orange' },
  PAUSED_UP: { label: '暂停上传', color: 'orange' },
  STALLED_DL: { label: '下载停滞', color: 'orange' },
  STALLED_UP: { label: '上传停滞', color: 'orange' },
  CHECKING: { label: '校验中', color: 'cyan' },
  CHECKING_RESUME_DATA: { label: '校验恢复数据', color: 'cyan' },
  ERROR: { label: '错误', color: 'red' },
  MISSING_FILES: { label: '文件丢失', color: 'red' },
  QUEUED: { label: '排队', color: 'default' },
  ALLOCATING: { label: '分配空间', color: 'cyan' },
  META_DL: { label: '下载元数据', color: 'cyan' },
  FORCED_DL: { label: '强制下载', color: 'blue' },
  FORCED_UP: { label: '强制上传', color: 'green' },
  MOVING: { label: '移动文件', color: 'cyan' },
  UNKNOWN: { label: '未知', color: 'default' },
  // 兼容小写（备用，避免遗漏）
  downloading: { label: '下载中', color: 'blue' },
  uploading: { label: '上传中', color: 'green' },
  seeding: { label: '做种中', color: 'green' },
  pausedDL: { label: '暂停下载', color: 'orange' },
  pausedUP: { label: '暂停上传', color: 'orange' },
  stalledDL: { label: '下载停滞', color: 'orange' },
  stalledUP: { label: '上传停滞', color: 'orange' },
  checking: { label: '校验中', color: 'cyan' },
  error: { label: '错误', color: 'red' },
  missingFiles: { label: '文件丢失', color: 'red' },
  queued: { label: '排队', color: 'default' },
  unknown: { label: '未知', color: 'default' },
}

const STATE_OPTIONS = [
  { value: '', label: '全部状态' },
  // 下拉只展示一次（去重）：用 UPPER 大写 key 作为 value
  ...Object.entries(STATE_META)
    .filter(([key]) => key === key.toUpperCase())
    .map(([value, m]) => ({ value, label: m.label })),
]

function stateLabel(s: string): string {
  return STATE_META[s]?.label ?? s
}

function stateColor(s: string): string {
  return STATE_META[s]?.color ?? 'default'
}

async function loadDownloaders() {
  try {
    const all = await api.get<DownloaderInfo[]>('/downloaders')
    downloaders.value = all.filter((d) => d.enabled)
    if (downloaders.value.length > 0 && activeTab.value === null) {
      activeTab.value = downloaders.value[0].id
    }
  } catch { /* ignore */ }
}

async function loadItems() {
  if (activeTab.value === null) return
  loading.value = true
  try {
    const params = new URLSearchParams()
    // 前端用大写枚举值展示；后端 TorrentState 接受小写 enum value
    if (filterState.value) {
      const lower = filterState.value.toLowerCase()
      params.set('state', lower)
    }
    if (filterCategory.value.trim()) params.set('category', filterCategory.value.trim())
    params.set('page', String(page.value))
    params.set('page_size', String(pageSize.value))
    const resp = await api.get<ListResp>(
      `/downloaders/${activeTab.value}/torrents?${params.toString()}`,
    )
    items.value = resp.items
    total.value = resp.total
  } catch (err) {
    // 清空旧数据，避免 tab 切换时显示别的下载器的内容
    items.value = []
    total.value = 0
    console.error(`下载器 ${activeTab.value} 拉取失败：`, err)
  }
  finally { loading.value = false }
}

watch(activeTab, () => { page.value = 1; loadItems() })
watch(filterState, () => { page.value = 1; loadItems() })
watch(filterCategory, () => { page.value = 1; loadItems() })

let searchTimer: ReturnType<typeof setTimeout> | null = null
watch(filterQ, () => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => { page.value = 1; loadItems() }, 300)
})

onMounted(async () => {
  await loadDownloaders()
  await loadItems()
})

// 客户端二次过滤：name 包含
const filtered = computed(() => {
  const q = filterQ.value.trim().toLowerCase()
  if (!q) return items.value
  return items.value.filter((t) => t.name.toLowerCase().includes(q))
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

function formatRate(n: number): string {
  if (n <= 0) return '—'
  return `${formatSize(n)}/s`
}

function activeTabName(): string {
  return downloaders.value.find((d) => d.id === activeTab.value)?.name ?? ''
}

const columns = [
  {
    title: '名称', key: 'name', ellipsis: true,
    sorter: (a: DownloaderTorrent, b: DownloaderTorrent) => a.name.localeCompare(b.name),
  },
  {
    title: '进度', key: 'progress', width: 180,
    sorter: (a: DownloaderTorrent, b: DownloaderTorrent) => a.progress - b.progress,
    customRender: ({ record }: { record: DownloaderTorrent }) => {
      const pct = (record.progress * 100).toFixed(1)
      return `${pct}%`
    },
  },
  { title: '大小', key: 'size', width: 100,
    sorter: (a: DownloaderTorrent, b: DownloaderTorrent) => a.size - b.size,
    customRender: ({ record }: { record: DownloaderTorrent }) => formatSize(record.size) },
  { title: '已下载', key: 'downloaded', width: 100,
    customRender: ({ record }: { record: DownloaderTorrent }) => formatSize(record.downloaded) },
  { title: '已上传', key: 'uploaded', width: 100,
    customRender: ({ record }: { record: DownloaderTorrent }) => formatSize(record.uploaded) },
  { title: '比例', key: 'ratio', width: 80,
    sorter: (a: DownloaderTorrent, b: DownloaderTorrent) => a.ratio - b.ratio,
    customRender: ({ record }: { record: DownloaderTorrent }) => record.ratio.toFixed(2) },
  { title: 'DL', key: 'dlspeed', width: 100,
    customRender: ({ record }: { record: DownloaderTorrent }) => formatRate(record.dlspeed) },
  { title: 'UL', key: 'upspeed', width: 100,
    customRender: ({ record }: { record: DownloaderTorrent }) => formatRate(record.upspeed) },
  { title: '状态', key: 'state', width: 100,
    sorter: (a: DownloaderTorrent, b: DownloaderTorrent) => a.state.localeCompare(b.state),
    customRender: ({ record }: { record: DownloaderTorrent }) => stateLabel(record.state) },
  { title: '分类', key: 'category', width: 120,
    customRender: ({ record }: { record: DownloaderTorrent }) => record.category ?? '—' },
  { title: '标签', key: 'tags', width: 160,
    customRender: ({ record }: { record: DownloaderTorrent }) =>
      record.tags.length > 0 ? record.tags.join(', ') : '—' },
]
</script>

<template>
  <a-card title="任务（按下载器分 Tab）">
    <a-alert
      v-if="downloaders.length === 0"
      type="info"
      show-icon
      message="暂未配置下载器"
      description="请先在「下载器」菜单添加 qBittorrent 或 Transmission 实例"
      style="margin-bottom: 16px"
    />

    <a-tabs
      v-if="downloaders.length > 0"
      v-model:active-key="activeTab"
      :tab-bar-style="{ marginBottom: '16px' }"
    >
      <a-tab-pane
        v-for="dl in downloaders"
        :key="dl.id"
        :tab="dl.name"
      >
        <a-space style="margin-bottom: 12px" :wrap="true">
          <a-select
            v-model:value="filterState"
            placeholder="状态"
            style="width: 140px"
            :allow-clear="true"
            :options="STATE_OPTIONS"
          />
          <a-input
            v-model:value="filterCategory"
            placeholder="分类（可选）"
            style="width: 160px"
            :allow-clear="true"
          />
          <a-input-search
            v-model:value="filterQ"
            placeholder="按名称搜索"
            style="width: 240px"
            :allow-clear="true"
          />
        </a-space>

        <a-typography-paragraph type="secondary" style="margin-top: 0">
          {{ activeTabName() }}：共 <b>{{ total }}</b> 条
          <span v-if="filterQ.trim()">（已显示 {{ filtered.length }} 条匹配）</span>
        </a-typography-paragraph>

        <a-table
          :data-source="filtered"
          :columns="columns"
          :loading="loading"
          row-key="hash"
          :pagination="{
            current: page,
            pageSize: pageSize,
            total: total,
            showSizeChanger: true,
            onChange: (p: number) => { page = p; loadItems() },
            onShowSizeChange: (p: number, ps: number) => { page = p; pageSize = ps; loadItems() },
          }"
          :scroll="{ x: 1400 }"
          size="small"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'progress'">
              <a-progress
                :percent="Math.round(record.progress * 1000) / 10"
                :status="record.progress >= 1 ? 'success' : (record.state === 'error' || record.state === 'missingFiles' ? 'exception' : 'active')"
                size="small"
              />
            </template>
            <template v-else-if="column.key === 'state'">
              <a-tag :color="stateColor(record.state)">{{ stateLabel(record.state) }}</a-tag>
            </template>
            <template v-else-if="column.key === 'name'">
              <a-typography-text :ellipsis="true" style="max-width: 360px">
                {{ record.name }}
              </a-typography-text>
            </template>
          </template>
        </a-table>
      </a-tab-pane>
    </a-tabs>
  </a-card>
</template>
