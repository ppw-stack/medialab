<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { api } from '../api'
import { message } from 'ant-design-vue'

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

interface BatchOpResult {
  requested: number
  succeeded: number
  failed: number
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
const selectedRowKeys = ref<string[]>([])
const batchLoading = ref(false)
const deleteModalOpen = ref(false)

const STATE_META: Record<string, { label: string; color: string }> = {
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
    if (filterState.value) {
      params.set('state', filterState.value.toLowerCase())
    }
    if (filterCategory.value.trim()) params.set('category', filterCategory.value.trim())
    const resp = await api.get<ListResp>(
      `/downloaders/${activeTab.value}/torrents?${params.toString()}`,
    )
    items.value = resp.items
    total.value = resp.total
  } catch (err) {
    items.value = []
    total.value = 0
    console.error(`下载器 ${activeTab.value} 拉取失败：`, err)
  }
  finally { loading.value = false }
}

watch(activeTab, () => { page.value = 1; selectedRowKeys.value = []; loadItems() })
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

const hasSelection = computed(() => selectedRowKeys.value.length > 0)

function onSelectChange(keys: string[]) {
  selectedRowKeys.value = keys
}

async function onBatchPause(paused: boolean) {
  const hashes = [...selectedRowKeys.value]
  if (hashes.length === 0 || activeTab.value === null) return
  batchLoading.value = true
  try {
    const r = await api.post<BatchOpResult>(
      `/downloaders/${activeTab.value}/torrents/pause`,
      { hashes, paused },
    )
    if (r.failed > 0) {
      message.warning(`已${paused ? '暂停' : '恢复'} ${r.succeeded} 个，失败 ${r.failed} 个`)
    } else {
      message.success(`已${paused ? '暂停' : '恢复'} ${r.succeeded} 个任务`)
    }
    selectedRowKeys.value = []
    loadItems()
  } catch (e) {
    message.error(e instanceof Error ? e.message : '操作失败')
  } finally {
    batchLoading.value = false
  }
}

function openDeleteModal() {
  if (!hasSelection.value) return
  deleteModalOpen.value = true
}

async function doDelete(deleteData: boolean) {
  const hashes = [...selectedRowKeys.value]
  if (hashes.length === 0 || activeTab.value === null) return
  deleteModalOpen.value = false
  batchLoading.value = true
  try {
    const r = await api.post<BatchOpResult>(
      `/downloaders/${activeTab.value}/torrents/remove`,
      { hashes, delete_data: deleteData },
    )
    const what = deleteData ? '任务 + 磁盘数据' : '任务'
    if (r.failed > 0) {
      message.warning(`已删除${what} ${r.succeeded} 个，失败 ${r.failed} 个`)
    } else {
      message.success(`已删除${what} ${r.succeeded} 个`)
    }
    selectedRowKeys.value = []
    loadItems()
  } catch (e) {
    message.error(e instanceof Error ? e.message : '删除失败')
  } finally {
    batchLoading.value = false
  }
}

const columns = [
  {
    title: '名称', key: 'name', ellipsis: true,
    sorter: (a: DownloaderTorrent, b: DownloaderTorrent) => a.name.localeCompare(b.name),
  },
  {
    title: '进度', key: 'progress', width: 140,
    sorter: (a: DownloaderTorrent, b: DownloaderTorrent) => a.progress - b.progress,
  },
  { title: '大小', key: 'size', width: 90,
    sorter: (a: DownloaderTorrent, b: DownloaderTorrent) => a.size - b.size,
    customRender: ({ record }: { record: DownloaderTorrent }) => formatSize(record.size) },
  { title: '已下载', key: 'downloaded', width: 90,
    customRender: ({ record }: { record: DownloaderTorrent }) => formatSize(record.downloaded) },
  { title: '已上传', key: 'uploaded', width: 90,
    customRender: ({ record }: { record: DownloaderTorrent }) => formatSize(record.uploaded) },
  { title: '比例', key: 'ratio', width: 70,
    sorter: (a: DownloaderTorrent, b: DownloaderTorrent) => a.ratio - b.ratio,
    customRender: ({ record }: { record: DownloaderTorrent }) => record.ratio.toFixed(2) },
  { title: 'DL', key: 'dlspeed', width: 90,
    customRender: ({ record }: { record: DownloaderTorrent }) => formatRate(record.dlspeed) },
  { title: 'UL', key: 'upspeed', width: 90,
    customRender: ({ record }: { record: DownloaderTorrent }) => formatRate(record.upspeed) },
  { title: '状态', key: 'state', width: 100,
    sorter: (a: DownloaderTorrent, b: DownloaderTorrent) => a.state.localeCompare(b.state) },
  { title: '分类', key: 'category', width: 110,
    customRender: ({ record }: { record: DownloaderTorrent }) => record.category ?? '—' },
  { title: '标签', key: 'tags', width: 140,
    customRender: ({ record }: { record: DownloaderTorrent }) =>
      record.tags.length > 0 ? record.tags.join(', ') : '—' },
  { title: '保存路径', key: 'save_path', width: 200, ellipsis: true,
    customRender: ({ record }: { record: DownloaderTorrent }) => record.save_path ?? '—' },
]
</script>

<template>
  <div>
    <header class="page-header">
      <div>
        <div class="page-header__eyebrow">下载任务 · TORRENTS</div>
        <h1 class="page-header__title">下载器实时任务</h1>
        <div class="page-header__sub">
          {{ activeTabName() }}&nbsp;·&nbsp;<span class="mono">共 {{ total }} 条</span>
        </div>
      </div>
      <div class="page-header__actions">
        <a-button
          :disabled="!hasSelection"
          :loading="batchLoading"
          @click="onBatchPause(true)"
        >暂停选中</a-button>
        <a-button
          :disabled="!hasSelection"
          :loading="batchLoading"
          @click="onBatchPause(false)"
        >恢复选中</a-button>
        <a-button
          danger
          :disabled="!hasSelection"
          :loading="batchLoading"
          @click="openDeleteModal"
        >删除选中 ({{ selectedRowKeys.length }})</a-button>
      </div>
    </header>

    <hr class="divider-hairline" style="margin: 0" />

    <section class="section">
      <a-alert
        v-if="downloaders.length === 0"
        type="info"
        show-icon
        message="暂未配置下载器"
        description="请先在「下载器」菜单添加 qBittorrent 或 Transmission 实例"
        style="margin-bottom: var(--sp-3)"
      />

      <div v-if="downloaders.length > 0" class="panel">
      <a-tabs
        v-model:active-key="activeTab"
        :tab-bar-style="{ marginBottom: 'var(--sp-3)' }"
      >
        <a-tab-pane
          v-for="dl in downloaders"
          :key="dl.id"
          :tab="dl.name"
        >
          <div class="filter-row">
            <a-select
              v-model:value="filterState"
              placeholder="状态"
              style="width: 140px"
              :allow-clear="true"
              size="middle"
              :options="STATE_OPTIONS"
            />
            <a-input
              v-model:value="filterCategory"
              placeholder="分类"
              style="width: 140px"
              :allow-clear="true"
              size="middle"
            />
            <a-input-search
              v-model:value="filterQ"
              placeholder="按名称搜索"
              style="width: 240px"
              :allow-clear="true"
              size="middle"
            />
            <span class="mono filter-row__meta">
              <span v-if="filterQ.trim()" style="color: var(--text-secondary)">
                匹配 <b style="color: var(--accent-red)">{{ filtered.length }}</b> /
              </span>
              <span style="color: var(--text-secondary)">总计 <b style="color: var(--text-primary)">{{ total }}</b></span>
            </span>
          </div>

          <a-table
            :data-source="filtered"
            :columns="columns"
            :loading="loading"
            row-key="hash"
            :row-selection="{
              selectedRowKeys: selectedRowKeys,
              onChange: onSelectChange,
            }"
            :pagination="{
              current: page,
              pageSize: pageSize,
              total: total,
              showSizeChanger: true,
              onChange: (p: number) => { page = p; loadItems() },
              onShowSizeChange: (p: number, ps: number) => { page = p; pageSize = ps; loadItems() },
            }"
            :scroll="{ x: 1600 }"
            size="small"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'progress'">
                <div class="progress-cell">
                  <a-progress
                    :percent="Math.round(record.progress * 1000) / 10"
                    :status="record.progress >= 1 ? 'success' : (record.state === 'error' || record.state === 'missingFiles' ? 'exception' : 'active')"
                    size="small"
                    :show-info="false"
                    :stroke-width="3"
                  />
                  <span class="mono progress-cell__num">{{ (record.progress * 100).toFixed(1) }}%</span>
                </div>
              </template>
              <template v-else-if="column.key === 'state'">
                <a-tag :color="stateColor(record.state)">{{ stateLabel(record.state) }}</a-tag>
              </template>
              <template v-else-if="column.key === 'name'">
                <a-typography-text
                  :ellipsis="true"
                  :content="record.name"
                  style="max-width: 360px"
                />
              </template>
              <template v-else-if="['size','downloaded','uploaded','dlspeed','upspeed','ratio'].includes(column.key as string)">
                <span class="mono">{{ column.customRender?.({ record }) }}</span>
              </template>
            </template>
          </a-table>
        </a-tab-pane>
      </a-tabs>
      </div>
    </section>

    <a-modal
      v-model:open="deleteModalOpen"
      title="删除任务"
      :footer="null"
      :mask-closable="false"
    >
      <p class="modal-text">
        即将删除选中下载器中的
        <b>{{ selectedRowKeys.length }}</b> 个任务，请选择删除方式：
      </p>
      <div class="modal-actions">
        <a-button @click="deleteModalOpen = false">取消</a-button>
        <a-button :loading="batchLoading" @click="doDelete(false)">
          仅删任务（保留文件）
        </a-button>
        <a-popconfirm
          title="磁盘数据将被永久删除，无法恢复。确认继续？"
          ok-text="确认删除"
          cancel-text="取消"
          :ok-button-props="{ danger: true }"
          @confirm="doDelete(true)"
        >
          <a-button danger :loading="batchLoading">
            删任务 + 磁盘数据
          </a-button>
        </a-popconfirm>
      </div>
    </a-modal>
  </div>
</template>

<style scoped>
.page-header__actions {
  display: flex;
  gap: var(--sp-2);
}

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
}

.progress-cell {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}

.progress-cell :deep(.ant-progress) {
  flex: 1;
  margin: 0;
}

.progress-cell__num {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  flex: 0 0 auto;
}

.modal-text {
  color: var(--text-primary);
  line-height: 1.6;
}

.modal-actions {
  display: flex;
  gap: var(--sp-2);
  justify-content: flex-end;
  margin-top: var(--sp-4);
}
</style>
