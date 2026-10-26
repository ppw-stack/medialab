<script setup lang="ts">
import { ref, onMounted, watch, computed } from 'vue'
import { api, errMsg } from '../api'
import type {
  BatchOpResult,
  CrossSeedAnomaly,
  CrossSeedDirUsage,
  CrossSeedDownloader,
  CrossSeedOrphan,
  CrossSeedOrphanDeleteResult,
  CrossSeedOrphanSummary,
  CrossSeedScannedDir,
  CrossSeedSnapshot,
  CrossSeedSnapshotHeader,
  CrossSeedSnapshotList,
  CrossSeedStatsRow,
} from '../types'
import { formatBytes as fmtBytes } from '../utils/format'
import { message } from 'ant-design-vue'


//: 辅种下载器数据来自**定时抓取的快照**（表 cross_seed_snapshot），
//: 前端只读 DB——不打下载器接口。辅种站种子量大（3000+），实时拉一次
//: 要 20s+，每次打开页面都拉既慢又给下载器加压。

const downloaders = ref<CrossSeedDownloader[]>([])
const selectedId = ref<number | null>(null)
const snapshot = ref<CrossSeedSnapshot | null>(null)
const pendingList = ref<CrossSeedDownloader[]>([])

/** 列表加载中（DB 读，毫秒级） */
const loading = ref(false)
/** 详情加载中 */
const detailLoading = ref(false)
/** 手动刷新中（要 await 完整抓取，20s+） */
const refreshing = ref(false)

const statsColumns = [
  { title: '原始名称', dataIndex: 'name', key: 'name', ellipsis: true, minWidth: 360 },
  { title: '大小 (GB)', dataIndex: 'size_gb', key: 'size_gb', width: 120, sorter: (a: CrossSeedStatsRow, b: CrossSeedStatsRow) => a.size_gb - b.size_gb },
  { title: '出现次数', dataIndex: 'count', key: 'count', width: 120, sorter: (a: CrossSeedStatsRow, b: CrossSeedStatsRow) => a.count - b.count },
  { title: '操作', key: 'actions', width: 100, fixed: 'right' as const },
]

const anomalyColumns = [
  { title: '名称', dataIndex: 'name', key: 'name', ellipsis: true, minWidth: 260 },
  { title: 'Hash', dataIndex: 'hash', key: 'hash', width: 300 },
  { title: '异常类型', dataIndex: 'anomaly_type', key: 'anomaly_type', width: 180 },
  { title: '错误描述', dataIndex: 'error_string', key: 'error_string', ellipsis: true, minWidth: 220 },
  { title: '状态', key: 'paused', width: 100 },
]

//: 异常种子里勾选的 hash（批量删除用）
const selectedAnomalyHashes = ref<string[]>([])
/** 删除确认弹框 */
const deleteModalOpen = ref(false)
/** 删除中 */
const deleting = ref(false)
/**
 * 待删除的目标。
 *
 * 两种来源共用一个弹框：
 * - 异常种子明细（tracker 未注册 / 数据丢失）
 * - 统计卡片的分组（同名同大小的一组**正常**种子）
 */
const deleteTarget = ref<{
  /** 要删的 hash 列表 */
  hashes: string[]
  /** 弹框里显示的数量（= hashes.length） */
  count: number
  /** 弹框主文案（说明删的是什么） */
  desc: string
  /** 危险程度提示（正常种子比异常种子更需要注意） */
  warn?: string
} | null>(null)

const dirColumns = [
  { title: '下载目录', dataIndex: 'dir', key: 'dir', ellipsis: true, minWidth: 320 },
  { title: '种子数', dataIndex: 'count', key: 'count', width: 100 },
  { title: '实际占用', dataIndex: 'bytes', key: 'bytes', width: 140 },
  { title: '总大小', dataIndex: 'total_bytes', key: 'total_bytes', width: 140 },
]

const orphanColumns = [
  { title: '条目名', dataIndex: 'name', key: 'name', ellipsis: true, minWidth: 320 },
  { title: '占用', dataIndex: 'bytes', key: 'bytes', width: 140 },
  { title: '文件数', dataIndex: 'file_count', key: 'file_count', width: 100 },
]

//: 孤儿明细里勾选的条目（按 path 选中——path 唯一）
const selectedOrphanPaths = ref<string[]>([])
/** 孤儿删除确认弹框 */
const orphanDeleteModalOpen = ref(false)
/** 孤儿删除中 */
const orphanDeleting = ref(false)

const scannedDirColumns = [
  { title: '扫描目录', dataIndex: 'scan_dir', key: 'scan_dir', ellipsis: true, minWidth: 320 },
  { title: '顶层条目', dataIndex: 'total_count', key: 'total_count', width: 100 },
  { title: '有主', dataIndex: 'owned_count', key: 'owned_count', width: 90 },
  { title: '孤儿', dataIndex: 'orphan_count', key: 'orphan_count', width: 90 },
  { title: '其中空目录', dataIndex: 'empty_dir_count', key: 'empty_dir_count', width: 110 },
  { title: '孤儿占用', dataIndex: 'orphan_bytes', key: 'orphan_bytes', width: 140 },
]

/** 孤儿文件统计（定时作业写入，页面只读 DB） */
const orphanSummary = ref<CrossSeedOrphanSummary | null>(null)
const orphanLoading = ref(false)

async function loadDownloaders() {
  try {
    downloaders.value = await api.get<CrossSeedDownloader[]>('/cross-seed-monitor/downloaders')
    if (downloaders.value.length) {
      // 保持已选中的 tab（刷新后不跳回第一个）
      if (selectedId.value == null || !downloaders.value.some(d => d.id === selectedId.value)) {
        selectedId.value = downloaders.value[0].id
      }
    } else {
      selectedId.value = null
    }
  } catch (e) {
    message.error('加载下载器失败：' + errMsg(e))
  }
}

//: 请求序列号 — 防止慢响应覆盖新响应（切 tab 时旧请求可能后到）
let detailSeq = 0

async function loadSnapshotList() {
  loading.value = true
  try {
    const data = await api.get<CrossSeedSnapshotList>('/cross-seed-monitor/snapshots')
    pendingList.value = data.pending
    // 头部信息直接来自列表响应，避免再逐个拉详情
    headers.value = new Map(data.snapshots.map(h => [h.downloader_id, h]))
  } catch (e) {
    message.error('加载辅种快照失败：' + errMsg(e))
  } finally {
    loading.value = false
  }
}

/** 加载孤儿文件统计。 */
async function loadOrphans() {
  orphanLoading.value = true
  try {
    orphanSummary.value = await api.get<CrossSeedOrphanSummary>(
      '/cross-seed-monitor/orphans',
    )
  } catch (e) {
    message.error('加载孤儿文件统计失败：' + errMsg(e))
  } finally {
    orphanLoading.value = false
  }
}

/** 当前 tab 的孤儿统计（该下载器的）。 */
const currentOrphan = computed(() => {
  const id = selectedId.value
  if (id == null || !orphanSummary.value) return null
  return orphanSummary.value.downloaders.find(d => d.downloader_id === id) ?? null
})

/** 快照头部（列表响应带来，避免逐 tab 请求） */
const headers = ref<Map<number, CrossSeedSnapshotHeader>>(new Map())

async function loadDetail(id: number) {
  const seq = ++detailSeq
  detailLoading.value = true
  snapshot.value = null
  try {
    const result = await api.get<CrossSeedSnapshot>(
      `/cross-seed-monitor/downloaders/${id}/snapshot`
    )
    if (seq !== detailSeq) return  // 已有更新的请求，丢弃
    snapshot.value = result
  } catch (e) {
    if (seq !== detailSeq) return
    // 404 = 还没抓过快照（等待定时作业 / 手动刷新）
    snapshot.value = null
  } finally {
    if (seq === detailSeq) detailLoading.value = false
  }
}

//: 手动刷新的 generation — 防止旧请求覆盖新一轮状态
let refreshSeq = 0

async function refreshNow() {
  const id = selectedId.value
  if (id == null) return
  const seq = ++refreshSeq
  refreshing.value = true
  try {
    const result = await api.post<CrossSeedSnapshot>(
      `/cross-seed-monitor/downloaders/${id}/refresh`
    )
    if (seq !== refreshSeq) return
    snapshot.value = result
    message.success(`已刷新：${result.total_torrents} 个种子，${result.anomaly_count} 个异常`)
    await loadSnapshotList()
  } catch (e) {
    if (seq !== refreshSeq) return
    message.error('刷新失败：' + errMsg(e))
  } finally {
    if (seq === refreshSeq) refreshing.value = false
  }
}

watch(selectedId, (id) => {
  // 换 tab → 清掉上一个 tab 的勾选（hash 属于不同下载器）
  selectedAnomalyHashes.value = []
  if (id != null) loadDetail(id)
})

/** 异常明细的勾选变化。 */
function onAnomalySelectionChange(keys: (string | number)[]) {
  selectedAnomalyHashes.value = keys.map(String)
}

/** 全选当前 tab 的全部异常（跨分页）。 */
function selectAllAnomalies() {
  selectedAnomalyHashes.value = (snapshot.value?.anomalies ?? []).map((a) => a.hash)
}

/** 打开删除确认弹框（异常种子明细）。 */
function openDeleteModal() {
  if (!selectedAnomalyHashes.value.length) return
  deleteTarget.value = {
    hashes: [...selectedAnomalyHashes.value],
    count: selectedAnomalyHashes.value.length,
    desc: '个异常种子（tracker 未注册 / 本地数据丢失）',
  }
  deleteModalOpen.value = true
}

/** 打开删除确认弹框（统计卡片的分组）。 */
function openDeleteGroupModal(row: CrossSeedStatsRow) {
  const hashes = row.hashes ?? []
  if (!hashes.length) return
  deleteTarget.value = {
    hashes: [...hashes],
    count: hashes.length,
    desc: `组「${row.name}」的 ${hashes.length} 个种子（同名同大小）`,
    warn: '这些是正常做种的种子，删除后不再做种。',
  }
  deleteModalOpen.value = true
}

/** 执行批量删除。 */
async function doDelete(deleteData: boolean) {
  const id = selectedId.value
  const target = deleteTarget.value
  if (id == null || !target || target.hashes.length === 0) return
  deleteModalOpen.value = false
  deleting.value = true
  try {
    const r = await api.post<BatchOpResult>(
      `/cross-seed-monitor/downloaders/${id}/remove-torrents`,
      { hashes: target.hashes, delete_data: deleteData },
    )
    const what = deleteData ? '任务 + 磁盘数据' : '任务'
    if (r.failed > 0) {
      message.warning(
        `已删除${what} ${r.succeeded} 个，失败 ${r.failed} 个` +
          (r.errors?.length ? `：${r.errors[0]}` : ''),
      )
    } else {
      message.success(`已删除${what} ${r.succeeded} 个`)
    }
    selectedAnomalyHashes.value = []
    deleteTarget.value = null
    // 重新抓取快照 → UI 反映最新状态（异常数、占用、目录分布都会变）
    await refreshNow()
  } catch (e) {
    message.error('删除失败：' + errMsg(e))
  } finally {
    deleting.value = false
  }
}

/** 打开孤儿删除确认弹框。 */
function openOrphanDeleteModal() {
  if (!selectedOrphanPaths.value.length) return
  orphanDeleteModalOpen.value = true
}

/** 执行孤儿文件删除（**不可逆**）。 */
async function doDeleteOrphans() {
  orphanDeleteModalOpen.value = false
  orphanDeleting.value = true
  try {
    const r = await api.post<CrossSeedOrphanDeleteResult>(
      '/cross-seed-monitor/orphans/delete',
      { paths: selectedOrphanPaths.value },
    )
    const parts: string[] = []
    if (r.succeeded > 0) parts.push(`已删除 ${r.succeeded} 个（${fmtBytes(r.freed_bytes)}）`)
    if (r.skipped > 0) parts.push(`跳过 ${r.skipped} 个（复核时已有任务）`)
    if (r.failed > 0) parts.push(`失败 ${r.failed} 个`)
    if (r.warning) {
      message.warning(r.warning)
    }
    if (r.failed > 0) {
      message.warning(parts.join('，'))
    } else {
      message.success(parts.join('，') || '没有可删除的条目')
    }
    // 后端删除后**已重扫**并把最新统计带回来了——直接用，省一次请求。
    // （孤儿统计本来是每天一次的作业快照，不重扫页面会一直显示旧数字）
    if (r.orphans) {
      orphanSummary.value = r.orphans
    } else {
      // 后端重扫失败（rescan_error）→ 退回自己拉一次
      if (r.rescan_error) {
        message.warning('重扫失败，统计可能不是最新：' + r.rescan_error)
      }
      await loadOrphans()
    }
    selectedOrphanPaths.value = []
  } catch (e) {
    message.error('删除失败：' + errMsg(e))
  } finally {
    orphanDeleting.value = false
  }
}

function countColor(c: number) {
  if (c >= 5) return 'red'
  if (c >= 3) return 'orange'
  return 'default'
}

/** 异常类型的中文标签。 */
const ANOMALY_LABELS: Record<string, string> = {
  tracker_not_registered: 'Tracker 未注册',
  no_data_found: '数据丢失',
}

function anomalyLabel(t: string): string {
  return ANOMALY_LABELS[t] ?? t
}

/** 当前 tab 的快照头部（可能还没有快照）。 */
const currentHeader = computed(() =>
  selectedId.value == null ? undefined : headers.value.get(selectedId.value),
)

/** 当前 tab 是否有快照。 */
const hasSnapshot = computed(() => currentHeader.value !== undefined)

/** 磁盘已用字节数（总量未知时为 null）。 */
const diskUsedBytes = computed<number | null>(() => {
  const s = snapshot.value
  if (!s || s.disk_total_bytes === null || s.disk_free_bytes === null) return null
  return Math.max(s.disk_total_bytes - s.disk_free_bytes, 0)
})

/** 磁盘已用占比（0–1）。总量未知时为 null——**不画进度条**。 */
const diskUsagePercent = computed<number | null>(() => {
  const s = snapshot.value
  if (!s || !s.disk_total_bytes) return null
  const used = diskUsedBytes.value ?? 0
  return Math.min(used / s.disk_total_bytes, 1)
})

/** 孤儿占用占扫描目录的比例（0–1）。 */
const orphanRatio = computed<number | null>(() => {
  const o = currentOrphan.value
  if (!o || !o.total_scan_bytes) return null
  return o.orphan_bytes / o.total_scan_bytes
})

/** 快照的新鲜度文案。 */
function freshnessText(scannedAt: string): string {
  if (!scannedAt) return '—'
  const d = new Date(scannedAt.replace(' ', 'T') + '+08:00')
  const diff = (Date.now() - d.getTime()) / 1000
  if (diff < 60) return `${Math.floor(diff)} 秒前`
  if (diff < 3600) return `${Math.floor(diff / 60)} 分前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
}

onMounted(async () => {
  await loadDownloaders()
  await Promise.all([loadSnapshotList(), loadOrphans()])
  if (selectedId.value != null) {
    await loadDetail(selectedId.value)
  }
})
</script>

<template>
  <div>
    <header class="page-header">
      <div>
        <div class="page-header__eyebrow">辅种 · CROSS-SEED</div>
        <h1 class="page-header__title">辅种下载器统计</h1>
        <div class="page-header__sub">
          按下载器分 Tab, 展示 (名称, 大小) 相同的种子分组 · 定时抓取落库, 异常自动暂停
        </div>
      </div>
      <div class="page-header__meta">
        <a-button @click="loadSnapshotList" :loading="loading">刷新列表</a-button>
        <a-button
          type="primary"
          @click="refreshNow"
          :loading="refreshing"
          :disabled="selectedId == null"
        >
          立即抓取
        </a-button>
      </div>
    </header>

    <hr class="divider-hairline" style="margin: 0" />

    <section class="section">
    <a-empty
      v-if="!downloaders.length && !loading"
      description="暂无 is_cross_seed 标记的下载器。在「下载器」页将需要的实例勾选 is_cross_seed 后, 回到此页查看统计。"
    />
    <a-tabs
      v-else
      v-model:active-key="selectedId"
      class="content-tabs"
    >
      <a-tab-pane v-for="dl in downloaders" :key="dl.id">
        <template #tab>
          {{ dl.name }} ({{ dl.type }})
          <a-badge
            v-if="currentHeader && currentHeader.downloader_id === dl.id && currentHeader.anomaly_count"
            :count="currentHeader.anomaly_count"
            :number-style="{ backgroundColor: '#fa8c16' }"
          />
        </template>

        <!-- 加载中（不是空状态） -->
        <div v-if="detailLoading" class="loading-row">
          <span class="mono dim">加载快照...</span>
        </div>

        <!-- 还没抓过快照 -->
        <a-alert
          v-else-if="!hasSnapshot"
          type="info"
          show-icon
          message="等待首次抓取"
          description="辅种数据由定时作业抓取后落库。若一直没有数据，请检查调度中心的「辅种巡查」作业是否启用，或点右上角「立即抓取」。"
        />

        <div v-else-if="snapshot">
          <div class="summary-bar">
            <span>抓取于 {{ snapshot.scanned_at }}（{{ freshnessText(snapshot.scanned_at) }}）</span>
            <span class="dot">·</span>
            <span>共 {{ snapshot.total_torrents }} 种子</span>
            <span class="dot">·</span>
            <span>{{ snapshot.unique_count }} 个唯一分组</span>
            <span class="dot">·</span>
            <span>{{ snapshot.inflight_count }} 个在途</span>
            <span class="dot">·</span>
            <a-tag v-if="snapshot.anomaly_count" color="orange">
              巡查发现 {{ snapshot.anomaly_count }} 个异常
              <template v-if="snapshot.anomaly_count - snapshot.paused_count - snapshot.pause_failed_count > 0">
                （{{ snapshot.anomaly_count - snapshot.paused_count - snapshot.pause_failed_count }} 个此前已暂停）
              </template>
            </a-tag>
            <a-tag v-else color="green">无异常</a-tag>
          </div>

          <!-- 空间占用 -->
          <section class="space-block">
            <div class="label-caps" style="margin-bottom: var(--sp-2)">
              空间占用 · SPACE
            </div>
            <div class="space-tiles">
              <div class="space-tile">
                <div class="space-tile__label">辅种实际占用</div>
                <div class="space-tile__value">{{ fmtBytes(snapshot.used_bytes) }}</div>
                <div class="space-tile__hint">磁盘上实际占用的字节数</div>
              </div>
              <div class="space-tile">
                <div class="space-tile__label">辅种总大小</div>
                <div class="space-tile__value">{{ fmtBytes(snapshot.total_size_bytes) }}</div>
                <div class="space-tile__hint">种子理论总大小</div>
              </div>
              <div class="space-tile">
                <div class="space-tile__label">磁盘剩余</div>
                <div class="space-tile__value">
                  {{ snapshot.disk_free_bytes === null ? '—' : fmtBytes(snapshot.disk_free_bytes) }}
                </div>
                <div class="space-tile__hint">
                  {{ snapshot.disk_total_bytes === null ? '该下载器不提供总量' : '所在分区可用空间' }}
                </div>
              </div>
              <div class="space-tile">
                <div class="space-tile__label">磁盘总量</div>
                <div class="space-tile__value">
                  {{ snapshot.disk_total_bytes === null ? '—' : fmtBytes(snapshot.disk_total_bytes) }}
                </div>
                <div class="space-tile__hint">
                  {{ snapshot.disk_total_bytes === null ? 'qBittorrent 不提供总量' : '所在分区总容量' }}
                </div>
              </div>
            </div>

            <!-- 占用进度条（总量未知时不画——画出来会误导成「磁盘已满」） -->
            <div v-if="diskUsagePercent !== null" class="space-progress">
              <div class="space-progress__header">
                <span>磁盘已用 {{ fmtBytes(diskUsedBytes) }}</span>
                <span class="dim">{{ (diskUsagePercent * 100).toFixed(1) }}%</span>
              </div>
              <a-progress :percent="diskUsagePercent * 100" :show-info="false" />
            </div>

            <!-- 按目录分布 -->
            <a-table
              v-if="snapshot.by_dir.length"
              :columns="dirColumns"
              :data-source="snapshot.by_dir"
              :row-key="(r: CrossSeedDirUsage) => r.dir"
              :pagination="false"
              size="small"
              style="margin-top: var(--sp-3)"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'dir'">
                  <a-tooltip :title="(record as CrossSeedDirUsage).dir">
                    <span class="name-cell mono">{{ (record as CrossSeedDirUsage).dir }}</span>
                  </a-tooltip>
                </template>
                <template v-else-if="column.key === 'bytes'">
                  {{ fmtBytes((record as CrossSeedDirUsage).bytes) }}
                </template>
                <template v-else-if="column.key === 'total_bytes'">
                  {{ fmtBytes((record as CrossSeedDirUsage).total_bytes) }}
                </template>
              </template>
            </a-table>
          </section>

          <!-- 孤儿文件（磁盘有、任务没了） -->
          <section class="space-block">
            <div class="label-caps" style="margin-bottom: var(--sp-2)">
              孤儿文件 · ORPHANS
            </div>

            <!-- 加载中 -->
            <div v-if="orphanLoading" class="loading-row">
              <span class="mono dim">加载孤儿统计...</span>
            </div>

            <!-- 扫描不可用（未挂载 / 失败）-->
            <a-alert
              v-else-if="currentOrphan?.error"
              type="warning"
              show-icon
              message="孤儿扫描不可用"
              :description="currentOrphan.error"
            />

            <!-- 还没扫过 -->
            <a-alert
              v-else-if="!currentOrphan"
              type="info"
              show-icon
              message="尚未扫描孤儿文件"
              description="孤儿文件由定时作业「辅种孤儿文件扫描」每天扫一次盘得出（需把辅种站的下载目录只读挂载到容器）。"
            />

            <!-- 有数据 -->
            <template v-else>
              <div class="space-tiles">
                <div class="space-tile">
                  <div class="space-tile__label">孤儿条目</div>
                  <div class="space-tile__value">{{ currentOrphan.orphan_count }}</div>
                  <div class="space-tile__hint">磁盘有、下载器已无对应任务</div>
                </div>
                <div class="space-tile">
                  <div class="space-tile__label">孤儿占用</div>
                  <div class="space-tile__value">{{ fmtBytes(currentOrphan.orphan_bytes) }}</div>
                  <div class="space-tile__hint">
                    扫描于 {{ currentOrphan.scanned_at }}
                  </div>
                </div>
                <div class="space-tile">
                  <div class="space-tile__label">其中空目录</div>
                  <div class="space-tile__value">{{ currentOrphan.empty_dir_count }}</div>
                  <div class="space-tile__hint">0 字节残留，不占空间</div>
                </div>
                <div class="space-tile">
                  <div class="space-tile__label">占扫描目录</div>
                  <div class="space-tile__value">
                    {{ orphanRatio === null ? '—' : `${(orphanRatio * 100).toFixed(2)}%` }}
                  </div>
                  <div class="space-tile__hint">
                    共 {{ fmtBytes(currentOrphan.total_scan_bytes) }} /
                    {{ currentOrphan.total_scan_count }} 个顶层条目
                  </div>
                </div>
              </div>

              <!-- 逐目录统计（多路径配置时每个目录一行） -->
              <a-table
                v-if="currentOrphan.scanned_dirs.length > 1"
                :columns="scannedDirColumns"
                :data-source="currentOrphan.scanned_dirs"
                :row-key="(r: CrossSeedScannedDir) => r.scan_dir"
                :pagination="false"
                size="small"
                style="margin-top: var(--sp-3)"
              >
                <template #bodyCell="{ column, record }">
                  <template v-if="column.key === 'scan_dir'">
                    <span class="name-cell mono">{{ (record as CrossSeedScannedDir).scan_dir }}</span>
                  </template>
                  <template v-else-if="column.key === 'orphan_bytes'">
                    {{ fmtBytes((record as CrossSeedScannedDir).orphan_bytes) }}
                  </template>
                </template>
              </a-table>

              <a-alert
                v-if="currentOrphan.orphan_count"
                type="warning"
                show-icon
                message="这些文件不再被做种，却一直占着空间。删除前请确认——删除不可恢复。"
                style="margin-top: var(--sp-3)"
              />

              <a-collapse v-if="currentOrphan.orphans.length" style="margin-top: var(--sp-3)">
                <a-collapse-panel key="orphans">
                  <template #header>
                    <span class="anomaly-header">
                      孤儿明细（{{ currentOrphan.orphans.length }}）
                    </span>
                  </template>
                  <div class="orphan-toolbar">
                    <a-button
                      size="small"
                      @click="selectedOrphanPaths = currentOrphan.orphans.map(o => o.path)"
                    >
                      全选（{{ currentOrphan.orphans.length }}）
                    </a-button>
                    <a-button
                      size="small"
                      danger
                      :disabled="!selectedOrphanPaths.length"
                      @click="openOrphanDeleteModal"
                    >
                      删除选中（{{ selectedOrphanPaths.length }}）
                    </a-button>
                  </div>
                  <a-table
                    :columns="orphanColumns"
                    :data-source="currentOrphan.orphans"
                    :row-key="(r: CrossSeedOrphan) => r.path"
                    :row-selection="{
                      selectedRowKeys: selectedOrphanPaths,
                      onChange: (keys: (string | number)[]) => {
                        selectedOrphanPaths = keys.map(String)
                      },
                    }"
                    :pagination="{ pageSize: 20, showSizeChanger: true }"
                    size="small"
                  >
                    <template #bodyCell="{ column, record }">
                      <template v-if="column.key === 'name'">
                        <a-tooltip :title="(record as CrossSeedOrphan).path">
                          <span class="name-cell">{{ (record as CrossSeedOrphan).name }}</span>
                        </a-tooltip>
                      </template>
                      <template v-else-if="column.key === 'bytes'">
                        {{ fmtBytes((record as CrossSeedOrphan).bytes) }}
                      </template>
                    </template>
                  </a-table>
                </a-collapse-panel>
              </a-collapse>
            </template>
          </section>

          <a-alert
            type="info"
            show-icon
            message="按原始名称 + 大小精确分组, 不做归一化 (集数/分辨率/制作组差异都算不同)"
            style="margin-bottom: var(--sp-3)"
          />

          <!-- 异常明细 -->
          <a-collapse
            v-if="snapshot.anomalies.length"
            style="margin-bottom: var(--sp-3)"
          >
            <a-collapse-panel key="anomalies">
              <template #header>
                <span class="anomaly-header">
                  异常种子明细（{{ snapshot.anomalies.length }}）
                  <span class="dim">— tracker 未注册 / 本地数据丢失</span>
                </span>
              </template>

              <!-- 批量删除工具条 -->
              <div class="anomaly-toolbar">
                <a-button
                  size="small"
                  @click="selectAllAnomalies"
                >
                  全选（{{ snapshot.anomalies.length }}）
                </a-button>
                <a-button
                  size="small"
                  type="danger"
                  :disabled="selectedAnomalyHashes.length === 0"
                  :loading="deleting"
                  @click="openDeleteModal"
                >
                  删除选中（{{ selectedAnomalyHashes.length }}）
                </a-button>
                <span v-if="selectedAnomalyHashes.length" class="dim">
                  已选 {{ selectedAnomalyHashes.length }} 个异常种子
                </span>
              </div>

              <a-table
                :columns="anomalyColumns"
                :data-source="snapshot.anomalies"
                :row-key="(r: CrossSeedAnomaly) => r.hash"
                :row-selection="{
                  selectedRowKeys: selectedAnomalyHashes,
                  onChange: onAnomalySelectionChange,
                  preserveSelectedRowKeys: false,
                }"
                :pagination="{ pageSize: 20, showSizeChanger: true }"
                size="small"
              >
                <template #bodyCell="{ column, record }">
                  <template v-if="column.key === 'name'">
                    <a-tooltip :title="(record as CrossSeedAnomaly).name">
                      <span class="name-cell">{{ (record as CrossSeedAnomaly).name }}</span>
                    </a-tooltip>
                  </template>
                  <template v-else-if="column.key === 'hash'">
                    <span class="mono">{{ (record as CrossSeedAnomaly).hash }}</span>
                  </template>
                  <template v-else-if="column.key === 'anomaly_type'">
                    <a-tag color="orange">
                      {{ anomalyLabel((record as CrossSeedAnomaly).anomaly_type) }}
                    </a-tag>
                  </template>
                  <template v-else-if="column.key === 'error_string'">
                    <span class="dim">
                      {{ (record as CrossSeedAnomaly).error_string || '—' }}
                    </span>
                  </template>
                  <template v-else-if="column.key === 'paused'">
                    <a-tag v-if="(record as CrossSeedAnomaly).paused" color="default">
                      已暂停
                    </a-tag>
                    <a-tag v-else color="red">待暂停</a-tag>
                  </template>
                </template>
              </a-table>
            </a-collapse-panel>
          </a-collapse>

          <a-table
            :columns="statsColumns"
            :data-source="snapshot.stats"
            :row-key="(r: CrossSeedStatsRow) => r.name + '|' + r.size_gb"
            :pagination="{ pageSize: 50, showSizeChanger: true }"
            :default-sort-order="{ columnKey: 'count', order: 'descend' }"
            size="middle"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'name'">
                <a-tooltip :title="(record as CrossSeedStatsRow).name">
                  <span class="name-cell">{{ (record as CrossSeedStatsRow).name }}</span>
                </a-tooltip>
              </template>
              <template v-else-if="column.key === 'size_gb'">
                {{ (record as CrossSeedStatsRow).size_gb }} GB
              </template>
              <template v-else-if="column.key === 'count'">
                <a-tag :color="countColor((record as CrossSeedStatsRow).count)">
                  {{ (record as CrossSeedStatsRow).count }}
                </a-tag>
              </template>
              <template v-else-if="column.key === 'actions'">
                <a-tooltip
                  v-if="!(record as CrossSeedStatsRow).hashes?.length"
                  title="该快照没有存完整 hash（升级前抓的），需重新抓取后才能删除"
                >
                  <a-button size="small" danger disabled>删除</a-button>
                </a-tooltip>
                <a-button
                  v-else
                  size="small"
                  danger
                  :loading="deleting"
                  @click="openDeleteGroupModal(record as CrossSeedStatsRow)"
                >
                  删除
                </a-button>
              </template>
            </template>
          </a-table>
        </div>
      </a-tab-pane>
    </a-tabs>
    </section>

    <!-- 批量删除种子的确认弹框（异常种子明细 / 统计卡片分组 共用） -->
    <a-modal
      v-model:open="deleteModalOpen"
      title="删除种子"
      :footer="null"
      :mask-closable="false"
    >
      <template v-if="deleteTarget">
        <p class="modal-text">
          即将从下载器中删除选中的
          <b>{{ deleteTarget.count }}</b> {{ deleteTarget.desc }}，请选择删除方式：
        </p>
        <a-alert
          v-if="deleteTarget.warn"
          type="warning"
          show-icon
          :message="deleteTarget.warn"
          style="margin-bottom: var(--sp-3)"
        />
        <a-alert
          type="info"
          show-icon
          message="仅删任务会保留磁盘文件，但这些文件不再被做种——变成占空间的孤儿数据。要真正腾出空间，请选「删任务 + 磁盘数据」。"
          style="margin-bottom: var(--sp-3)"
        />
        <div class="modal-actions">
          <a-button @click="deleteModalOpen = false">取消</a-button>
          <a-button :loading="deleting" @click="doDelete(false)">
            仅删任务（保留文件）
          </a-button>
          <a-popconfirm
            title="磁盘数据将被永久删除，无法恢复。确认继续？"
            ok-text="确认删除"
            cancel-text="取消"
            :ok-button-props="{ danger: true }"
            @confirm="doDelete(true)"
          >
            <a-button danger :loading="deleting">
              删任务 + 磁盘数据
            </a-button>
          </a-popconfirm>
        </div>
      </template>
    </a-modal>

    <!-- 孤儿文件删除的确认弹框（**不可逆**） -->
    <a-modal
      v-model:open="orphanDeleteModalOpen"
      title="删除孤儿文件"
      :footer="null"
      :mask-closable="false"
    >
      <p class="modal-text">
        即将从磁盘上**永久删除**选中的
        <b>{{ selectedOrphanPaths.length }}</b> 个孤儿条目。
      </p>
      <a-alert
        type="warning"
        show-icon
        message="删除不可恢复。删除前系统会重新扫描一次，确认这些条目此刻仍无对应任务；若期间已有任务接管，会自动跳过。"
        style="margin-bottom: var(--sp-3)"
      />
      <a-alert
        type="info"
        show-icon
        message="孤儿 = 磁盘上有、下载器里已无对应任务的文件。请确认这些内容确实不再需要做种。"
        style="margin-bottom: var(--sp-3)"
      />
      <div class="modal-actions">
        <a-button @click="orphanDeleteModalOpen = false">取消</a-button>
        <a-popconfirm
          title="磁盘文件将被永久删除，无法恢复。确认继续？"
          ok-text="确认删除"
          cancel-text="取消"
          :ok-button-props="{ danger: true }"
          @confirm="doDeleteOrphans"
        >
          <a-button danger :loading="orphanDeleting">
            确认删除（{{ selectedOrphanPaths.length }}）
          </a-button>
        </a-popconfirm>
      </div>
    </a-modal>
  </div>
</template>

<style scoped>
.content-tabs {
  background: var(--bg-surface);
  border: 1px solid var(--border-hairline);
  border-radius: var(--r-md);
  box-shadow: var(--shadow-sm);
  padding: var(--sp-4);
  min-height: 400px;
}

.loading-row {
  padding: var(--sp-4) 0;
}

.summary-bar {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
  padding: var(--sp-2) var(--sp-3);
  background: var(--bg-inset);
  border-radius: var(--r-sm);
  font-size: var(--text-sm);
  color: var(--text-secondary);
  flex-wrap: wrap;
}

.summary-bar .dot {
  color: var(--text-tertiary);
}

.anomaly-header {
  font-weight: 500;
}

/* 异常明细的批量操作工具条 */
.anomaly-toolbar {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-2);
  flex-wrap: wrap;
}

/* 孤儿明细的批量操作工具条 */
.orphan-toolbar {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-2);
  flex-wrap: wrap;
}

.modal-text {
  color: var(--text-primary);
  line-height: 1.6;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-2);
  margin-top: var(--sp-4);
  padding-top: var(--sp-3);
  border-top: 1px solid var(--border-hairline);
}

/* 空间占用区块 */
.space-block {
  margin-bottom: var(--sp-3);
  padding: var(--sp-3);
  background: var(--bg-inset);
  border-radius: var(--r-sm);
}

.space-tiles {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--sp-3);
}

.space-tile__label {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  margin-bottom: var(--sp-1);
}

.space-tile__value {
  font-family: var(--font-mono);
  font-size: var(--text-lg);
  font-weight: 600;
  color: var(--text-primary);
  line-height: 1.3;
}

.space-tile__hint {
  font-size: var(--text-xs);
  color: var(--text-tertiary);
  margin-top: var(--sp-1);
}

.space-progress {
  margin-top: var(--sp-3);
}

.space-progress__header {
  display: flex;
  justify-content: space-between;
  font-size: var(--text-xs);
  color: var(--text-secondary);
  margin-bottom: var(--sp-1);
}

.dim {
  color: var(--text-tertiary);
}

.mono {
  font-family: var(--font-mono);
}

.text-sm {
  font-size: var(--text-sm);
}

.name-cell {
  display: inline-block;
  max-width: 460px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: middle;
}
</style>
