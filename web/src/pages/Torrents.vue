<script setup lang="ts">
import { ref, reactive, onMounted, onUnmounted, watch, h } from 'vue'
import { api } from '../api'
import { message, Modal } from 'ant-design-vue'

// ============================================================ 类型定义
interface Torrent {
  id: number
  info_hash: string
  name: string
  site: string
  site_id: number
  downloader_id: number
  downloader_name: string
  category: string | null
  status: string
  source: string
  size: number
  downloaded: number
  uploaded: number
  ratio: number
  state: string | null
  is_free: boolean
  free_type: string | null
  free_end_at: string | null
  added_at: string
  protect_until: string | null
}

interface Site {
  id: number
  name: string
}

interface DownloaderInfo {
  id: number
  name: string
  type: string
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
  page: number
  page_size: number
  items: Torrent[]
}

interface BatchOpResult {
  requested: number
  succeeded: number
  failed: number
  skipped: number
  errors: string[]
}

// ============================================================ 常量
const STATUS_COLOR: Record<string, string> = {
  active: 'green', paused: 'orange', completed: 'blue', aborted: 'red', deleted: 'gray',
  free_completed: 'green', free_completed_late: 'orange', free_missed: 'red',
}

const SOURCE_LABEL: Record<string, string> = {
  auto_seed: '辅种', free_seed: '免费种', manual: '手动',
}

const FREE_TYPE_LABELS: Record<string, string> = {
  freeleech_permanent: '永久免费',
  freeleech_timed: '限时免费',
  twoup_timed: '限时 2X',
  neutral_leech: '中性种',
  unknown: '收费',
}

const STATE_LABEL: Record<string, string> = {
  downloading: '下载中', uploading: '上传中', paused: '暂停',
  error: '错误', completed: '完成', queued: '排队', checking: '校验',
  stalledDL: '下载停滞', stalledUP: '上传停滞', other: '其他',
}

const TIME_RANGE_OPTIONS = [
  { value: 'all', label: '全部' },
  { value: '1d', label: '近 24h' },
  { value: '7d', label: '近 7 天' },
  { value: '30d', label: '近 30 天' },
]

// ============================================================ 格式化
function formatSize(b: number): string {
  if (!b) return '0 KB'
  if (b < 1024 ** 2) return `${(b / 1024).toFixed(0)} KB`
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(0)} MB`
  if (b < 1024 ** 4) return `${(b / 1024 ** 3).toFixed(1)} GB`
  return `${(b / 1024 ** 4).toFixed(2)} TB`
}

function formatSpeed(b: number): string {
  if (!b) return '0 B/s'
  if (b < 1024) return `${b} B/s`
  if (b < 1024 ** 2) return `${(b / 1024).toFixed(0)} KB/s`
  return `${(b / 1024 ** 2).toFixed(1)} MB/s`
}

function formatDate(d: string | null): string {
  return d ? new Date(d).toLocaleString() : '-'
}

function formatCountdown(d: string | null): string {
  if (!d) return '-'
  const ms = new Date(d).getTime() - Date.now()
  if (ms <= 0) return '已过期'
  const min = Math.floor(ms / 60000)
  if (min < 60) return `${min} 分`
  const h = Math.floor(min / 60)
  if (h < 24) return `${h} 时 ${min % 60} 分`
  const day = Math.floor(h / 24)
  return `${day} 天 ${h % 24} 时`
}

// ============================================================ 状态
const data = ref<ListResp>({ total: 0, page: 1, page_size: 50, items: [] })
const sites = ref<Site[]>([])
const downloaders = ref<DownloaderInfo[]>([])
const stats = ref<{ by_status: Record<string, number>; by_source: Record<string, number> } | null>(null)
const detail = ref<Torrent | null>(null)
const activeTab = ref('local')
const loading = ref(false)

const filters = reactive<{
  site_id?: number; downloader_id?: number; source?: string
  status?: string; q?: string; time_range?: string
}>({ time_range: 'all' })

const page = ref(1)
const pageSize = ref(50)
const selectedRowKeys = ref<number[]>([])
const autoRefresh = ref(false)
const lastRefresh = ref<Date | null>(null)
let refreshTimer: number | null = null

async function loadStats() {
  try {
    const d = await api.get<{ torrents: { by_status: Record<string, number>; by_source: Record<string, number> } }>('/dashboard')
    stats.value = { by_status: d.torrents.by_status, by_source: d.torrents.by_source }
  } catch { /* ignore */ }
}

async function loadFilters() {
  try {
    sites.value = await api.get<Site[]>('/sites')
  } catch { /* ignore */ }
  try {
    downloaders.value = await api.get<DownloaderInfo[]>('/downloaders')
  } catch { /* ignore */ }
}

async function load() {
  loading.value = true
  try {
    const qs = new URLSearchParams()
    if (filters.site_id) qs.set('site_id', String(filters.site_id))
    if (filters.downloader_id) qs.set('downloader_id', String(filters.downloader_id))
    if (filters.source) qs.set('source', filters.source)
    if (filters.status) qs.set('status', filters.status)
    if (filters.q) qs.set('q', filters.q)
    if (filters.time_range && filters.time_range !== 'all') {
      const since = new Date()
      if (filters.time_range === '1d') since.setDate(since.getDate() - 1)
      else if (filters.time_range === '7d') since.setDate(since.getDate() - 7)
      else if (filters.time_range === '30d') since.setDate(since.getDate() - 30)
      // 后端暂无时间范围参数，使用 q 占位（实际不加时间过滤）
    }
    qs.set('page', String(page.value))
    qs.set('page_size', String(pageSize.value))
    data.value = await api.get<ListResp>(`/torrents?${qs.toString()}`)
    lastRefresh.value = new Date()
  } catch { /* ignore */ }
  finally {
    loading.value = false
  }
}

watch(filters, () => { page.value = 1; load() }, { deep: true })
watch(page, load)
watch(pageSize, () => { page.value = 1; load() })

onMounted(() => {
  loadFilters()
  load()
  loadStats()
})

onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})

// ============================================================ 自动刷新
function toggleAutoRefresh() {
  autoRefresh.value = !autoRefresh.value
  if (autoRefresh.value) {
    refreshTimer = window.setInterval(refreshAll, 30000)
    message.info('自动刷新已开启（30s）')
  } else {
    if (refreshTimer) clearInterval(refreshTimer)
    refreshTimer = null
    message.info('自动刷新已关闭')
  }
}

function refreshAll() {
  load()
  loadStats()
  if (activeTab.value === 'downloader') loadDownloaderTorrents()
}

// ============================================================ 单条操作
async function onPause(t: Torrent) {
  try {
    await api.post(`/torrents/${t.id}/pause`, {})
    message.success(`已暂停：${t.name.slice(0, 30)}`)
    load()
  } catch (e) { message.error(e instanceof Error ? e.message : '暂停失败') }
}

async function onResume(t: Torrent) {
  try {
    await api.post(`/torrents/${t.id}/resume`, {})
    message.success(`已恢复：${t.name.slice(0, 30)}`)
    load()
  } catch (e) { message.error(e instanceof Error ? e.message : '恢复失败') }
}

// ------------------------------------------------------------ 删除统一确认
interface RemoveConfirmOpts {
  title: string
  summary: string
  /** 是否允许"删+磁盘数据"分支（FR-5.2.3 下载器原生任务禁用） */
  allowDeleteData: boolean
  onConfirm: (deleteData: boolean) => Promise<void>
}

function confirmRemove(opts: RemoveConfirmOpts): Promise<void> {
  return new Promise<void>((resolve) => {
    let chosen: 'rm' | 'data' | null = null
    const placeholder = 'rm'
    const placeholderData = 'data'
    Modal.confirm({
      title: opts.title,
      okText: '继续',
      cancelText: '取消',
      okButtonProps: { danger: true },
      content: () =>
        h('div', [
          h('p', opts.summary),
          h('p', { style: 'margin-top: 4px; color: #888' },
            opts.allowDeleteData
              ? '请选择删除方式（每种方式都需输入占位符确认）：'
              : '下载器原生任务仅支持删任务记录，磁盘数据由下载器自身管理。'),
          h('div', { style: 'margin-top: 12px; display: flex; gap: 8px' },
            [
              h(
                'button',
                {
                  class: 'ant-btn',
                  onClick: () => { chosen = 'rm' },
                },
                `仅删任务（占位符 "${placeholder}"）`
              ),
              opts.allowDeleteData
                ? h(
                    'button',
                    {
                      class: 'ant-btn ant-btn-dangerous',
                      onClick: () => { chosen = 'data' },
                    },
                    `删任务 + 磁盘数据（占位符 "${placeholderData}"，不可恢复）`
                  )
                : h(
                    'button',
                    {
                      class: 'ant-btn ant-btn-dangerous',
                      disabled: true,
                      title: '下载器原生任务不支持删除数据（FR-5.2.3）',
                    },
                    `删任务 + 磁盘数据（不支持）`
                  ),
            ]
          ),
          h('p', { style: 'margin-top: 8px; color: #888' },
            '点击上方按钮选择模式后，再点下方"继续"完成确认。'),
        ]),
      onOk: async () => {
        if (!chosen) {
          message.warning('请先在上方选择删除方式')
          return Promise.reject()
        }
        const expected = chosen === 'data' ? placeholderData : placeholder
        const userInput = window.prompt(`请输入 "${expected}" 确认删除:`)
        if (userInput !== expected) {
          message.error('输入不匹配，已取消')
          return Promise.reject()
        }
        try {
          await opts.onConfirm(chosen === 'data')
          resolve()
        } catch {
          // 失败由调用方 toast
        }
      },
      onCancel: () => resolve(),
    })
  })
}

async function onRemove(t: Torrent) {
  const summary = `任务名: ${t.name.slice(0, 40)}`
  await confirmRemove({
    title: '删除任务',
    summary,
    allowDeleteData: true,
    onConfirm: async (deleteData) => {
      try {
        await api.post(
          `/torrents/${t.id}/remove?delete_data=${deleteData}&force=true`,
          {}
        )
        message.success('已删除')
        load()
      } catch (e) {
        message.error(e instanceof Error ? e.message : '删除失败')
        throw e
      }
    },
  })
}

// ============================================================ 批量操作
async function onBatchPause(paused: boolean) {
  if (selectedRowKeys.value.length === 0) {
    message.warning('请先选择任务')
    return
  }
  const ids = selectedRowKeys.value
  const items = data.value.items.filter(t => ids.includes(t.id))
  const hashes = items.map(t => t.info_hash)
  try {
    const r = await api.post<BatchOpResult>('/torrents/batch/pause', { hashes, paused })
    message.success(`批量${paused ? '暂停' : '恢复'}：${r.succeeded}/${r.requested}（失败 ${r.failed}）`)
    selectedRowKeys.value = []
    load()
  } catch (e) { message.error(e instanceof Error ? e.message : '操作失败') }
}

async function onBatchRemove() {
  if (selectedRowKeys.value.length === 0) {
    message.warning('请先选择任务')
    return
  }
  const count = selectedRowKeys.value.length
  await confirmRemove({
    title: `批量删除 ${count} 个任务`,
    summary: `将影响 ${count} 个任务`,
    allowDeleteData: true,
    onConfirm: async (deleteData) => {
      const items = data.value.items.filter(t => selectedRowKeys.value.includes(t.id))
      const hashes = items.map(t => t.info_hash)
      try {
        const r = await api.post<BatchOpResult>('/torrents/batch/remove', {
          hashes, delete_data: deleteData, force: true,
        })
        message.success(`批量删除：${r.succeeded}/${r.requested}（失败 ${r.failed}）`)
        selectedRowKeys.value = []
        load()
      } catch (e) {
        message.error(e instanceof Error ? e.message : '操作失败')
        throw e
      }
    },
  })
}

// ============================================================ Tab 2: 下载器任务
const downloaderTorrents = ref<DownloaderTorrent[]>([])
const dlLoading = ref(false)
const selectedDlId = ref<number | null>(null)
const selectedDlKeys = ref<string[]>([])
const dlFilterState = ref<string | undefined>(undefined)

async function loadDownloaderTorrents() {
  if (!selectedDlId.value) {
    downloaderTorrents.value = []
    return
  }
  dlLoading.value = true
  try {
    const qs = new URLSearchParams()
    if (dlFilterState.value) qs.set('state', dlFilterState.value)
    const r = await api.get<{ total: number; items: DownloaderTorrent[] }>(
      `/downloaders/${selectedDlId.value}/torrents?${qs.toString()}`
    )
    downloaderTorrents.value = r.items
  } catch { /* ignore */ }
  finally {
    dlLoading.value = false
  }
}

watch(selectedDlId, () => { selectedDlKeys.value = []; loadDownloaderTorrents() })
watch(dlFilterState, loadDownloaderTorrents)

async function onDownloaderOp(op: 'pause' | 'resume' | 'remove') {
  if (!selectedDlId.value || selectedDlKeys.value.length === 0) {
    message.warning('请先选择任务')
    return
  }
  if (op === 'remove') {
    await confirmRemove({
      title: '删除下载器原生任务',
      summary: `将影响 ${selectedDlKeys.value.length} 个任务`,
      allowDeleteData: false,
      onConfirm: async () => {
        const payload = { hashes: selectedDlKeys.value, delete_data: false }
        try {
          const r = await api.post<BatchOpResult>(
            `/downloaders/${selectedDlId.value}/torrents/remove`, payload
          )
          message.success(`批量删除：${r.succeeded}/${r.requested}`)
          selectedDlKeys.value = []
          loadDownloaderTorrents()
        } catch (e) {
          message.error(e instanceof Error ? e.message : '操作失败')
          throw e
        }
      },
    })
    return
  }
  try {
    const payload: Record<string, unknown> = { hashes: selectedDlKeys.value, paused: op === 'pause' }
    const r = await api.post<BatchOpResult>(
      `/downloaders/${selectedDlId.value}/torrents/pause`, payload
    )
    message.success(`批量${op === 'pause' ? '暂停' : '恢复'}：${r.succeeded}/${r.requested}`)
    selectedDlKeys.value = []
    loadDownloaderTorrents()
  } catch (e) {
    message.error(e instanceof Error ? e.message : '操作失败')
  }
}

// ============================================================ 表格列
const localColumns = [
  { title: '种子', dataIndex: 'name', key: 'name', width: 280, fixed: 'left' as const },
  { title: '站点', dataIndex: 'site', key: 'site', width: 100 },
  { title: '下载器', dataIndex: 'downloader_name', key: 'downloader', width: 100 },
  { title: '来源', dataIndex: 'source', key: 'source', width: 80 },
  { title: '状态', dataIndex: 'status', key: 'status', width: 90 },
  { title: '免费', dataIndex: 'free_type', key: 'free', width: 110 },
  { title: '大小', dataIndex: 'size', key: 'size', width: 80 },
  { title: '进度', key: 'progress', width: 90 },
  { title: '种比', dataIndex: 'ratio', key: 'ratio', width: 70 },
  { title: '免费截止', key: 'free_end', width: 100 },
  { title: '添加于', dataIndex: 'added_at', key: 'added', width: 140 },
  { title: '操作', key: 'action', width: 240, fixed: 'right' as const },
]

const downloaderColumns = [
  { title: '种子', dataIndex: 'name', key: 'name', width: 280, fixed: 'left' as const },
  { title: '状态', dataIndex: 'state', key: 'state', width: 100 },
  { title: '进度', key: 'progress', width: 130 },
  { title: '大小', dataIndex: 'size', key: 'size', width: 90 },
  { title: '下载/上传', key: 'speed', width: 160 },
  { title: '种比', dataIndex: 'ratio', key: 'ratio', width: 80 },
  { title: '分类', dataIndex: 'category', key: 'category', width: 90 },
  { title: '路径', key: 'path', width: 200 },
]

</script>

<template>
  <div>
    <a-typography-title :level="4">任务管理</a-typography-title>

    <a-row v-if="stats" :gutter="16" style="margin-bottom: 16px">
      <a-col :span="6"><a-card size="small"><a-statistic title="活跃任务" :value="stats.by_status.active || 0" /></a-card></a-col>
      <a-col :span="6"><a-card size="small"><a-statistic title="暂停" :value="stats.by_status.paused || 0" /></a-card></a-col>
      <a-col :span="6"><a-card size="small"><a-statistic title="在途免费种" :value="stats.by_source.free_seed || 0" /></a-card></a-col>
      <a-col :span="6"><a-card size="small"><a-statistic title="总计" :value="data.total" /></a-card></a-col>
    </a-row>

    <a-card>
      <template #extra>
        <a-space>
          <a-tag v-if="lastRefresh" color="blue">上次刷新 {{ lastRefresh.toLocaleTimeString() }}</a-tag>
          <a-switch v-model:checked="autoRefresh" checked-children="自动" un-checked-children="手动" @change="toggleAutoRefresh" />
          <a-button @click="refreshAll">刷新</a-button>
        </a-space>
      </template>

      <a-tabs v-model:active-key="activeTab">
        <!-- ========================================== Tab 1: 本地辅种池 -->
        <a-tab-pane key="local" tab="本地辅种池">
          <a-space style="margin-bottom: 16px" wrap>
            <a-input-search
              placeholder="搜索名称 / hash"
              allow-clear style="width: 220px"
              :value="filters.q"
              @search="(v: string) => filters.q = v"
            />
            <a-select
              placeholder="站点" allow-clear style="width: 130px"
              :value="filters.site_id"
              @change="(v: number) => filters.site_id = v"
              :options="sites.map(s => ({ value: s.id, label: s.name }))"
            />
            <a-select
              placeholder="下载器" allow-clear style="width: 130px"
              :value="filters.downloader_id"
              @change="(v: number) => filters.downloader_id = v"
              :options="downloaders.map(d => ({ value: d.id, label: d.name }))"
            />
            <a-select
              placeholder="来源" allow-clear style="width: 110px"
              :value="filters.source"
              @change="(v: string) => filters.source = v"
              :options="[
                { value: 'auto_seed', label: '辅种' },
                { value: 'free_seed', label: '免费种' },
                { value: 'manual', label: '手动' },
              ]"
            />
            <a-select
              placeholder="状态" allow-clear style="width: 130px"
              :value="filters.status"
              @change="(v: string) => filters.status = v"
              :options="[
                { value: 'active', label: 'active' },
                { value: 'paused', label: 'paused' },
                { value: 'aborted', label: 'aborted' },
                { value: 'deleted', label: 'deleted' },
                { value: 'free_completed', label: 'free_completed' },
              ]"
            />
            <a-select
              placeholder="时间范围" style="width: 120px"
              :value="filters.time_range"
              @change="(v: string) => filters.time_range = v"
              :options="TIME_RANGE_OPTIONS"
            />
          </a-space>

          <a-space style="margin-bottom: 12px" v-if="selectedRowKeys.length > 0">
            <a-tag color="blue">已选 {{ selectedRowKeys.length }} 项</a-tag>
            <a-button size="small" @click="onBatchPause(true)">批量暂停</a-button>
            <a-button size="small" @click="onBatchPause(false)">批量恢复</a-button>
            <a-button size="small" danger @click="onBatchRemove()">批量删除</a-button>
            <a-button size="small" @click="selectedRowKeys = []">清空选择</a-button>
          </a-space>

          <a-table
            :data-source="data.items"
            :columns="localColumns"
            row-key="id"
            :loading="loading"
            :pagination="{
              current: page, pageSize, total: data.total,
              showSizeChanger: true, pageSizeOptions: ['20', '50', '100', '200'],
              onChange: (p: number) => page = p,
              onShowSizeChange: (_: number, ps: number) => pageSize = ps,
              showTotal: (t: number) => `共 ${t} 条`,
            }"
            :row-selection="{ selectedRowKeys, onChange: (keys: number[]) => selectedRowKeys = keys }"
            :scroll="{ x: 1500 }"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'name'">
                <div>
                  <a style="font-weight: 500" @click="detail = record">{{ record.name }}</a>
                  <a-typography-text type="secondary" code style="font-size: 11px">{{ record.info_hash.slice(0, 16) }}…</a-typography-text>
                </div>
              </template>
              <template v-else-if="column.key === 'source'">
                <a-tag :color="record.source === 'free_seed' ? 'purple' : record.source === 'auto_seed' ? 'blue' : 'default'">
                  {{ SOURCE_LABEL[record.source] || record.source }}
                </a-tag>
              </template>
              <template v-else-if="column.key === 'status'">
                <a-tag :color="STATUS_COLOR[record.status] || 'default'">{{ record.status }}</a-tag>
              </template>
              <template v-else-if="column.key === 'free'">
                <span v-if="!record.is_free">-</span>
                <a-tag v-else :color="record.free_type === 'freeleech_permanent' ? 'green' : 'orange'">{{ FREE_TYPE_LABELS[record.free_type] || record.free_type }}</a-tag>
              </template>
              <template v-else-if="column.key === 'size'">
                {{ formatSize(record.size) }}
              </template>
              <template v-else-if="column.key === 'progress'">
                <a-progress
                  :percent="record.size ? Math.round((record.downloaded / record.size) * 100) : 0"
                  size="small"
                  :status="record.downloaded >= record.size ? 'success' : 'active'"
                />
              </template>
              <template v-else-if="column.key === 'ratio'">
                {{ record.ratio.toFixed(2) }}
              </template>
              <template v-else-if="column.key === 'free_end'">
                <span v-if="!record.is_free || !record.free_end_at">-</span>
                <a-tooltip v-else :title="formatDate(record.free_end_at)">{{ formatCountdown(record.free_end_at) }}</a-tooltip>
              </template>
              <template v-else-if="column.key === 'added'">
                {{ formatDate(record.added_at) }}
              </template>
              <template v-else-if="column.key === 'action'">
                <a-space size="small">
                  <a-button v-if="record.status !== 'paused'" size="small" @click="onPause(record)">暂停</a-button>
                  <a-button v-else size="small" @click="onResume(record)">恢复</a-button>
                  <a-button size="small" danger @click="onRemove(record)">删除</a-button>
                </a-space>
              </template>
            </template>
          </a-table>
        </a-tab-pane>

        <!-- ========================================== Tab 2: 下载器原生任务 -->
        <a-tab-pane key="downloader" tab="下载器原生任务">
          <a-space style="margin-bottom: 16px" wrap>
            <a-select
              placeholder="选择下载器" style="width: 200px"
              :value="selectedDlId"
              @change="(v: number) => selectedDlId = v"
              :options="downloaders.filter(d => d.enabled).map(d => ({ value: d.id, label: `${d.name} (${d.type})` }))"
            />
            <a-select
              placeholder="状态过滤" allow-clear style="width: 140px"
              :value="dlFilterState"
              @change="(v: string) => dlFilterState = v"
              :options="[
                { value: 'downloading', label: '下载中' },
                { value: 'uploading', label: '上传中' },
                { value: 'paused', label: '暂停' },
                { value: 'completed', label: '完成' },
                { value: 'error', label: '错误' },
              ]"
            />
            <a-button @click="loadDownloaderTorrents">刷新</a-button>
            <a-tag v-if="selectedDlId" color="blue">共 {{ downloaderTorrents.length }} 条</a-tag>
          </a-space>

          <a-space style="margin-bottom: 12px" v-if="selectedDlKeys.length > 0">
            <a-tag color="blue">已选 {{ selectedDlKeys.length }} 项</a-tag>
            <a-button size="small" @click="onDownloaderOp('pause')">暂停</a-button>
            <a-button size="small" @click="onDownloaderOp('resume')">恢复</a-button>
            <a-button size="small" danger @click="onDownloaderOp('remove')">删除</a-button>
            <a-button size="small" @click="selectedDlKeys = []">清空选择</a-button>
          </a-space>

          <a-table
            :data-source="downloaderTorrents"
            :columns="downloaderColumns"
            row-key="hash"
            :loading="dlLoading"
            :pagination="{ pageSize: 50 }"
            :row-selection="{ selectedRowKeys: selectedDlKeys, onChange: (keys: string[]) => selectedDlKeys = keys }"
            :scroll="{ x: 1200 }"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'name'">
                <div>
                  <b>{{ record.name }}</b>
                  <a-typography-text type="secondary" code style="font-size: 11px">{{ record.hash.slice(0, 16) }}…</a-typography-text>
                </div>
              </template>
              <template v-else-if="column.key === 'state'">
                <a-tag>{{ STATE_LABEL[record.state] || record.state }}</a-tag>
              </template>
              <template v-else-if="column.key === 'progress'">
                <a-progress :percent="Math.round(record.progress * 100)" size="small" />
              </template>
              <template v-else-if="column.key === 'size'">
                {{ formatSize(record.size) }}
              </template>
              <template v-else-if="column.key === 'speed'">
                <div style="font-size: 11px">
                  <div style="color: #52c41a">↓ {{ formatSpeed(record.dlspeed) }}</div>
                  <div style="color: #1890ff">↑ {{ formatSpeed(record.upspeed) }}</div>
                </div>
              </template>
              <template v-else-if="column.key === 'ratio'">
                {{ record.ratio.toFixed(2) }}
              </template>
              <template v-else-if="column.key === 'category'">
                {{ record.category || '-' }}
              </template>
              <template v-else-if="column.key === 'path'">
                <a-typography-text code style="font-size: 11px">{{ record.save_path || '-' }}</a-typography-text>
              </template>
            </template>
          </a-table>
        </a-tab-pane>
      </a-tabs>
    </a-card>

    <!-- ============================================================ 详情弹窗 -->
    <a-modal
      :open="detail !== null"
      @cancel="detail = null"
      :title="detail?.name || ''"
      :footer="null"
      :width="720"
    >
      <div v-if="detail">
        <a-row :gutter="[16, 8]">
          <a-col :span="8"><a-typography-text type="secondary">Hash</a-typography-text></a-col>
          <a-col :span="16"><code style="font-size: 12px">{{ detail.info_hash }}</code></a-col>
          <a-col :span="8"><a-typography-text type="secondary">站点</a-typography-text></a-col>
          <a-col :span="16">{{ detail.site }}</a-col>
          <a-col :span="8"><a-typography-text type="secondary">下载器</a-typography-text></a-col>
          <a-col :span="16">{{ detail.downloader_name }}</a-col>
          <a-col :span="8"><a-typography-text type="secondary">分类</a-typography-text></a-col>
          <a-col :span="16">{{ detail.category || '-' }}</a-col>
          <a-col :span="8"><a-typography-text type="secondary">大小 / 已下 / 种比</a-typography-text></a-col>
          <a-col :span="16">
            {{ formatSize(detail.size) }} / {{ formatSize(detail.downloaded) }} / {{ detail.ratio.toFixed(2) }}
          </a-col>
          <a-col :span="8"><a-typography-text type="secondary">来源 / 状态</a-typography-text></a-col>
          <a-col :span="16">
            <a-tag color="blue">{{ SOURCE_LABEL[detail.source] || detail.source }}</a-tag>
            <a-tag :color="STATUS_COLOR[detail.status] || 'default'">{{ detail.status }}</a-tag>
            <a-tag v-if="detail.is_free" color="purple">{{ detail.free_type }}</a-tag>
          </a-col>
          <template v-if="detail.free_end_at">
            <a-col :span="8"><a-typography-text type="secondary">免费窗口截止</a-typography-text></a-col>
            <a-col :span="16">{{ formatDate(detail.free_end_at) }}（剩 {{ formatCountdown(detail.free_end_at) }}）</a-col>
          </template>
          <template v-if="detail.protect_until">
            <a-col :span="8"><a-typography-text type="secondary">禁删保护截止</a-typography-text></a-col>
            <a-col :span="16">{{ formatDate(detail.protect_until) }}（剩 {{ formatCountdown(detail.protect_until) }}）</a-col>
          </template>
          <a-col :span="8"><a-typography-text type="secondary">添加于</a-typography-text></a-col>
          <a-col :span="16">{{ formatDate(detail.added_at) }}</a-col>
        </a-row>
      </div>
    </a-modal>
  </div>
</template>