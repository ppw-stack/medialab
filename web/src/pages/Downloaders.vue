<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, watch } from 'vue'
import { api, errMsg } from '../api'
import { message } from 'ant-design-vue'
import type {
  BatchOpResult,
  Downloader,
  DownloaderTestResult,
  DownloaderTorrent,
  DownloaderTorrentList,
  DownloaderTorrentCount,
  TorrentStateMeta,
} from '../types'
import { formatBytes } from '../utils/format'
import DataTable from '../components/DataTable.vue'
import PageHeader from '../components/PageHeader.vue'

const TYPES = [
  { value: 'qbittorrent', label: 'qBittorrent', implemented: true },
  { value: 'transmission', label: 'Transmission', implemented: true },
  { value: 'deluge', label: 'Deluge', implemented: false },
  { value: 'aria2', label: 'aria2', implemented: false },
]

// 新建下拉只展示已实现的类型（选了也无法创建，后端 501）；编辑已有记录时
// 若其类型未实现（历史遗留），该类型仍需出现在下拉里否则 select 显示原始值
const selectableTypes = computed(() => {
  if (editing.value && !isImplemented(editing.value.type)) {
    return TYPES // 编辑未实现类型的已有记录：全量展示（该类型已选中，撤不掉）
  }
  return TYPES.filter((t) => t.implemented)
})

const data = ref<Downloader[]>([])
const editing = ref<Downloader | null>(null)
const creating = ref(false)
const testing = ref(false)
const testResult = ref<DownloaderTestResult | null>(null)
const modalOpen = ref(false)
const saving = ref(false)
const formRef = ref<{ validate: () => Promise<unknown> } | null>(null)

const formState = reactive<Record<string, unknown>>({
  name: '',
  type: 'qbittorrent',
  url: '',
  username: '',
  password: '',
  save_path: '',
  is_default: false,
  enabled: true,
  verify_tls: true,
  is_cross_seed: false,
})

async function load() {
  try {
    data.value = await api.get<Downloader[]>('/downloaders')
    // 默认选中第一个下载器（下方任务列表显示它的任务）
    if (activeDownloaderId.value === null && data.value.length > 0) {
      selectDownloader(data.value[0].id)
    }
    // 任务数**异步补**（实时统计慢，不阻塞列表渲染）
    void loadTorrentCounts()
  } catch {
    /* ignore */
  }
}

/**
 * 任务状态元信息（中文名 + 颜色）——**后端下发**。
 *
 * 以前前端硬编码一份 `STATE_META`（10 个状态的值 + 中文名 + 颜色），
 * 与后端 `TorrentState` 枚举**必须逐字一致**——后端加状态时漏改前端
 * 就会显示原始英文值（camelCase 的 pausedDL / missingFiles 尤其难认）。
 */
async function loadStateMeta(): Promise<void> {
  try {
    const meta = await api.get<TorrentStateMeta[]>('/downloaders/state-meta')
    stateMeta.value = Object.fromEntries(meta.map((m) => [m.value, m]))
  } catch {
    // 拿不到元信息 → 用空表（页面仍可用，只是标签显示原始值）
    stateMeta.value = {}
  }
}

/**
 * 补各下载器的**任务数**（实时统计，**单独请求**）。
 *
 * **为什么不放在列表接口里**：任务数要实时打下载器接口（辅种站 3000+
 * 种子，实测拉一次 7.6s）。放在列表接口里 → 前端列表超时 → 页面误显示
 * 「暂未配置下载器」。故列表先渲染，任务数异步补。
 */
async function loadTorrentCounts(): Promise<void> {
  const ids = data.value.map((d) => d.id)
  await Promise.all(
    ids.map(async (id) => {
      try {
        const r = await api.get<DownloaderTorrentCount>(`/downloaders/${id}/torrent-count`)
        torrentCounts.value[id] = r.count
      } catch {
        torrentCounts.value[id] = null
      }
    }),
  )
}

onMounted(() => {
  void load()
  void loadStateMeta()
})

function onCreate() {
  editing.value = null
  creating.value = true
  modalOpen.value = true
  testResult.value = null
  Object.assign(formState, {
    name: '',
    type: 'qbittorrent',
    url: 'http://',
    username: 'admin',
    password: '',
    save_path: '',
    is_default: false,
    enabled: true,
    verify_tls: true,
  })
}

function onEdit(d: Downloader) {
  editing.value = d
  creating.value = false
  modalOpen.value = true
  testResult.value = null
  Object.assign(formState, {
    name: d.name,
    type: d.type,
    url: d.url,
    username: d.username || '',
    password: '',
    save_path: d.save_path || '',
    is_default: d.is_default,
    enabled: d.enabled,
    verify_tls: d.verify_tls,
    is_cross_seed: d.is_cross_seed,
  })
}

async function onDelete(id: number) {
  try {
    await api.post('/downloaders/delete', { id })
    message.success('已删除')
    load()
  } catch (e) {
    message.error(errMsg(e, '删除失败'))
  }
}

async function onSave() {
  if (saving.value) return
  saving.value = true
  try {
    // 先校验表单（name 必填 + url 必填 + 名称 pattern）
    if (formRef.value) {
      await formRef.value.validate()
    }
    const payload: Record<string, unknown> = { ...formState }
    if (!payload.password) delete payload.password
    if (!payload.username) payload.username = null
    if (creating.value) {
      await api.post('/downloaders', payload)
      message.success('已创建')
    } else if (editing.value) {
      await api.post('/downloaders/update', { id: editing.value.id, ...payload })
      message.success('已更新')
    }
    closeModal()
    load()
  } catch (e) {
    // validate 抛错是字段级 ValidationError（无 message，是数组）；API 抛 ApiError
    // 两者都走 errMsg 取出可读文本，避免 [object Object]
    message.error(errMsg(e, '保存失败'))
  } finally {
    saving.value = false
  }
}

function closeModal() {
  modalOpen.value = false
  editing.value = null
  creating.value = false
  testResult.value = null
}

async function onTest(record?: Downloader) {
  if (testing.value) return
  const target = record ?? editing.value
  if (!target) return
  testing.value = true
  testResult.value = null
  try {
    if (record) {
      // 列表行测连：测 DB 里已保存的配置
      const r = await api.post<DownloaderTestResult>(
        `/downloaders/${record.id}/test`,
        {},
      )
      testResult.value = { reachable: r.reachable, detail: r.detail }
    } else {
      // 弹窗测连：用**表单里的值**测（用户可能刚改了 URL/账号/密码），
      // 否则测出来的是旧配置，结果与表单内容矛盾
      const r = await api.post<DownloaderTestResult>(
        '/downloaders/test-config',
        {
          id: target.id,
          type: formState.type,
          url: formState.url,
          username: formState.username,
          password: formState.password,
          verify_tls: formState.verify_tls,
        },
      )
      testResult.value = { reachable: r.reachable, detail: r.detail }
    }
    if (testResult.value.reachable) {
      message.success('测连成功')
    } else {
      message.error(`测连失败：${testResult.value.detail}`)
    }
  } catch (e) {
    message.error(errMsg(e, '测连失败'))
  } finally {
    testing.value = false
  }
}

function typeLabel(type: string): string {
  return TYPES.find((t) => t.value === type)?.label || type
}

function isImplemented(type: string): boolean {
  return TYPES.find((t) => t.value === type)?.implemented ?? false
}

// ==================================================================== 任务列表
//
// 「下载器实例」与「它的实时任务」合并到一个页面，**上下两段**：
// 上方 = 下载器管理表格（点行选中），下方 = 该下载器的实时任务列表。

/** 当前选中的下载器 id（上方表格点行切换；null = 未选）。 */
const activeDownloaderId = ref<number | null>(null)

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

// 任务状态元信息（后端下发，见 loadStateMeta）。
const stateMeta = ref<Record<string, { label: string; color: string }>>({})

/** 各下载器的任务数（异步补，见 loadTorrentCounts）。null = 取不到。 */
const torrentCounts = ref<Record<number, number | null>>({})

// STATE_OPTIONS：过滤下拉。value 直接用后端枚举值，不再做
// toLowerCase() 转换——后端枚举值是 camelCase（pausedDL / missingFiles），
// 转小写会得到后端不认的值。
const STATE_OPTIONS = computed(() => [
  { value: '', label: '全部状态' },
  ...Object.entries(stateMeta.value).map(([value, m]) => ({ value, label: m.label })),
])

function stateLabel(s: string): string {
  return stateMeta.value[s]?.label ?? s
}

function stateColor(s: string): string {
  return stateMeta.value[s]?.color ?? 'default'
}

/** 选中一个下载器（上方表格点行）→ 下方任务列表切到它。 */
function selectDownloader(id: number) {
  if (activeDownloaderId.value === id) return
  activeDownloaderId.value = id
}

async function loadItems() {
  const dlId = activeDownloaderId.value
  if (dlId === null) return
  loading.value = true
  try {
    const params = new URLSearchParams()
    if (filterState.value) {
      // 不做 toLowerCase()——后端枚举值是 camelCase（pausedDL / missingFiles）
      params.set('state', filterState.value)
    }
    if (filterCategory.value.trim()) params.set('category', filterCategory.value.trim())
    const resp = await api.get<DownloaderTorrentList>(
      `/downloaders/${dlId}/torrents?${params.toString()}`,
    )
    items.value = resp.items
    total.value = resp.total
  } catch (err) {
    items.value = []
    total.value = 0
    // loadItems 由多个 watch 触发（切 tab / 改筛选 / 搜索防抖），
    // 一次网络抖动会连弹多个 toast。全局限一次，避免刷屏。
    message.error({
      content: `下载器 ${activeDownloaderId.value} 拉取失败：${errMsg(err)}`,
      key: 'torrents-load-failed',
    })
  }
  finally { loading.value = false }
}

watch(activeDownloaderId, () => {
  page.value = 1
  selectedRowKeys.value = []
  loadItems()
})
watch(filterState, () => { page.value = 1; loadItems() })
watch(filterCategory, () => { page.value = 1; loadItems() })

let searchTimer: ReturnType<typeof setTimeout> | null = null
watch(filterQ, () => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => { page.value = 1; loadItems() }, 300)
})

onUnmounted(() => {
  // 路由切走后定时器仍会触发 loadItems() 对已销毁组件的 ref 写入
  if (searchTimer) {
    clearTimeout(searchTimer)
    searchTimer = null
  }
})

const filtered = computed(() => {
  const q = filterQ.value.trim().toLowerCase()
  if (!q) return items.value
  return items.value.filter((t) => t.name.toLowerCase().includes(q))
})

/**
 * 任务体积 / 速率格式化：1 位小数，字节级整数不带小数
 * （"500 B" / "1.5 KB" / "1.0 MB"）。
 */
function formatSize(n: number): string {
  return formatBytes(n, { decimals: 1, integerBytes: true })
}

function formatRate(n: number): string {
  if (n <= 0) return '—'
  return `${formatSize(n)}/s`
}

function activeTabName(): string {
  const dlId = activeDownloaderId.value
  if (dlId === null) return ''
  return data.value.find((d) => d.id === dlId)?.name ?? ''
}

/** 选中行的 class（高亮显示当前查看任务的下载器）。 */
function manageRowClass(record: Downloader): string {
  return record.id === activeDownloaderId.value ? 'manage-row--active' : ''
}

const hasSelection = computed(() => selectedRowKeys.value.length > 0)

function onSelectChange(keys: string[]) {
  selectedRowKeys.value = keys
}

async function onBatchPause(paused: boolean) {
  const hashes = [...selectedRowKeys.value]
  const dlId = activeDownloaderId.value
  if (hashes.length === 0 || dlId === null) return
  batchLoading.value = true
  try {
    const r = await api.post<BatchOpResult>(
      `/downloaders/${dlId}/torrents/pause`,
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
  const dlId = activeDownloaderId.value
  if (hashes.length === 0 || dlId === null) return
  deleteModalOpen.value = false
  batchLoading.value = true
  try {
    const r = await api.post<BatchOpResult>(
      `/downloaders/${dlId}/torrents/remove`,
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

const taskColumns = [
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

const manageColumns = [
  { title: '名称', key: 'name' },
  { title: '类型', key: 'type', width: 140 },
  { title: 'URL', key: 'url' },
  { title: '保存路径', key: 'save_path', width: 200 },
  { title: '凭据', key: 'password', width: 100 },
  { title: '默认', key: 'default', width: 60 },
  { title: '辅种', key: 'cross_seed', width: 60 },
  { title: '任务数', key: 'torrent_count', width: 90 },
  { title: '启用', key: 'enabled', width: 60 },
  { title: '', key: 'action', width: 180 },
]
</script>

<template>
  <div>
    <PageHeader
      eyebrow="下载器 · DOWNLOADERS"
      title="下载器与任务"
      sub="实例管理 · 实时任务 · qBittorrent / Transmission 已完整适配"
    >
      <template #sub>
        <span v-if="activeTabName()">{{ activeTabName() }}&nbsp;·&nbsp;</span>
        实例管理 · 实时任务 · qBittorrent / Transmission 已完整适配
      </template>
      <template #actions>
        <a-button type="primary" @click="onCreate">新增下载器</a-button>
      </template>
    </PageHeader>

    <hr class="divider-hairline" style="margin: 0" />

    <section class="section">
      <a-alert
        v-if="data.length === 0"
        type="info"
        show-icon
        message="暂未配置下载器"
        description="点击右上角「新增下载器」添加 qBittorrent 或 Transmission 实例。"
        style="margin-bottom: var(--sp-3)"
      />

      <div v-if="data.length > 0" class="panel">
        <!-- 上：下载器管理表格（点行选中 → 下方任务列表切到它） -->
        <div class="label-caps" style="margin-bottom: var(--sp-2)">
          下载器实例 · DOWNLOADERS
        </div>
        <DataTable
          :data-source="data"
          :columns="manageColumns"
          row-key="id"
          :pagination="{ pageSize: 20 }"
          :row-class-name="manageRowClass"
          @rowClick="(record: Downloader) => selectDownloader(record.id)"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'name'">
              <span class="row-name">{{ record.name }}</span>
            </template>
            <template v-else-if="column.key === 'type'">
              <a-space :size="4">
                <a-tag :color="isImplemented(record.type) ? 'blue' : 'default'">
                  {{ typeLabel(record.type) }}
                </a-tag>
                <a-tag v-if="!isImplemented(record.type)" color="orange">未实现</a-tag>
              </a-space>
            </template>
            <template v-else-if="column.key === 'url'">
              <span class="mono code">{{ record.url }}</span>
            </template>
            <template v-else-if="column.key === 'save_path'">
              <span v-if="record.save_path" class="mono code">{{ record.save_path }}</span>
              <span v-else class="dim">—</span>
            </template>
            <template v-else-if="column.key === 'password'">
              <a-tag v-if="record.has_password" color="green">已配置</a-tag>
              <a-tag v-else>—</a-tag>
            </template>
            <template v-else-if="column.key === 'default'">
              <a-tag v-if="record.is_default" color="purple">★ 默认</a-tag>
              <span v-else class="dim">—</span>
            </template>
            <template v-else-if="column.key === 'cross_seed'">
              <a-tooltip v-if="record.is_cross_seed" title="标记为辅种下载器（由辅种巡查作业自动扫描并暂停异常种子）">
                <a-tag color="cyan">辅种</a-tag>
              </a-tooltip>
              <span v-else class="dim">—</span>
            </template>
            <template v-else-if="column.key === 'torrent_count'">
              <span v-if="torrentCounts[record.id] !== null && torrentCounts[record.id] !== undefined" class="mono">
                {{ torrentCounts[record.id] }}
              </span>
              <span v-else class="dim" title="下载器不可达或未启用">—</span>
            </template>
            <template v-else-if="column.key === 'enabled'">
              <span v-if="record.enabled" class="status-dot status-dot--ok" />
              <span v-else class="status-dot status-dot--off" />
            </template>
            <template v-else-if="column.key === 'action'">
              <a-space :size="4">
                <a-button size="small" type="link" @click.stop="onEdit(record)">编辑</a-button>
                <a-button size="small" type="link" :loading="testing" :disabled="!isImplemented(record.type)" @click.stop="onTest(record)">测连</a-button>
                <a-popconfirm title="确认删除？" @confirm="onDelete(record.id)">
                  <a-button size="small" type="link" danger @click.stop>删除</a-button>
                </a-popconfirm>
              </a-space>
            </template>
          </template>
        </DataTable>

        <!-- 下：选中下载器的实时任务 -->
        <div class="label-caps" style="margin: var(--sp-4) 0 var(--sp-2)">
          实时任务 · TORRENTS
          <span v-if="activeTabName()" class="mono">（{{ activeTabName() }}）</span>
        </div>

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
          <a-space :size="4">
            <a-button
              size="small"
              :disabled="!hasSelection"
              :loading="batchLoading"
              @click="onBatchPause(true)"
            >暂停选中</a-button>
            <a-button
              size="small"
              :disabled="!hasSelection"
              :loading="batchLoading"
              @click="onBatchPause(false)"
            >恢复选中</a-button>
            <a-button
              danger
              size="small"
              :disabled="!hasSelection"
              :loading="batchLoading"
              @click="openDeleteModal"
            >删除选中 ({{ selectedRowKeys.length }})</a-button>
          </a-space>
        </div>

        <DataTable
          :data-source="filtered"
          :columns="taskColumns"
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
        </DataTable>
      </div>
    </section>

    <a-modal
      :open="modalOpen"
      :title="creating ? '新增下载器' : `编辑下载器 · ${editing?.name || ''}`"
      :width="640"
      :confirm-loading="saving"
      @cancel="closeModal"
      @ok="onSave"
    >
      <a-form
        ref="formRef"
        layout="vertical"
        :model="formState"
      >
        <a-space style="width: 100%" size="middle">
          <a-form-item
            label="名称"
            name="name"
            :rules="[{ required: true, pattern: /^[A-Za-z0-9_.-]+$/, message: '字母/数字/_/-/.' }]"
            style="flex: 1"
          >
            <a-input v-model:value="formState.name" placeholder="qbt-main" />
          </a-form-item>
          <a-form-item label="类型" name="type" style="flex: 1">
            <a-select v-model:value="formState.type">
              <a-select-option
                v-for="t in selectableTypes"
                :key="t.value"
                :value="t.value"
              >
                {{ t.label }}
              </a-select-option>
            </a-select>
          </a-form-item>
        </a-space>
        <a-form-item label="URL（含协议与端口）" name="url" :rules="[{ required: true }]">
          <a-input v-model:value="formState.url" placeholder="http://qbittorrent:8080" />
        </a-form-item>
        <a-form-item
          label="保存路径（可选）"
          name="save_path"
          tooltip="推送/添加种子时传给下载器（qB savepath / TR download_dir）。填下载器容器视角的路径，如 /data/torrents；留空用下载器默认路径。"
        >
          <a-input v-model:value="formState.save_path" placeholder="/data/torrents" allow-clear />
        </a-form-item>
        <a-space style="width: 100%" size="middle">
          <a-form-item label="用户名" name="username" style="flex: 1">
            <a-input v-model:value="formState.username" />
          </a-form-item>
          <a-form-item
            :label="editing?.has_password ? '密码（留空保留）' : '密码'"
            name="password"
            style="flex: 1"
          >
            <a-input-password v-model:value="formState.password" autocomplete="new-password" />
          </a-form-item>
        </a-space>
        <a-form-item label="设为默认下载器" name="is_default" tooltip="辅种和免费种会自动投到默认下载器">
          <a-switch v-model:checked="formState.is_default" />
        </a-form-item>
        <a-form-item label="校验 TLS 证书" name="verify_tls" tooltip="自签名证书场景下关闭">
          <a-switch v-model:checked="formState.verify_tls" />
        </a-form-item>
        <a-form-item label="辅种下载器" name="is_cross_seed" tooltip="标记此下载器参与辅种巡查（定时作业自动扫描并暂停 tracker 未注册 / 数据丢失的种子）">
          <a-switch v-model:checked="formState.is_cross_seed" />
        </a-form-item>
        <a-form-item name="enabled" label="启用">
          <a-switch v-model:checked="formState.enabled" />
        </a-form-item>
      </a-form>

      <div v-if="editing" class="test-card">
        <a-space direction="vertical" style="width: 100%">
          <a-space>
            <a-button :loading="testing" @click="onTest()">测连（实际适配器 ping）</a-button>
            <a-tag v-if="testResult" :color="testResult.reachable ? 'green' : 'red'">
              {{ testResult.reachable ? '✓ 已连通' : `✗ ${testResult.detail}` }}
            </a-tag>
          </a-space>
        </a-space>
      </div>
    </a-modal>

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
.row-name {
  font-weight: 500;
}

.dim {
  color: var(--text-tertiary);
}

.code {
  font-size: var(--text-xs);
  color: var(--text-tertiary);
}

.status-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
}

.status-dot--ok {
  background: var(--status-green);
  box-shadow: 0 0 8px var(--status-green-soft);
}

.status-dot--off {
  background: var(--text-tertiary);
}

.test-card {
  margin-top: var(--sp-4);
  padding: var(--sp-3);
  background: var(--bg-surface);
  border: 1px solid var(--border-hairline);
  border-radius: var(--r-md);
}

/* 筛选行（状态 / 分类 / 搜索 / 统计 / 批量操作）——横向排布，
   统计数字靠右（margin-left:auto），窄屏换行。 */
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

/* 进度单元格：进度条 + 百分比同一行 */
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

/* 下载器表格：整行可点（切换下方任务列表）+ 选中行高亮 */
:deep(.ant-table-tbody > tr) {
  cursor: pointer;
}

:deep(.manage-row--active > td) {
  background: var(--accent-red-soft) !important;
}

:deep(.manage-row--active > td:first-child) {
  box-shadow: inset 3px 0 0 var(--accent-red);
}
</style>
