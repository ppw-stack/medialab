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
  CrossSeedOrphanScanResult,
  CrossSeedOrphanSummary,
  CrossSeedScannedDir,
  CrossSeedSnapshot,
  CrossSeedSnapshotHeader,
  CrossSeedSnapshotList,
  CrossSeedStatsRow,
  DistributionRow,
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

/**
 * 内容分区的 Tab（**内层**，在下载器 Tab 之下）。
 *
 * 页面原本把 4 块内容纵向堆叠，实测 3727 px / 4.1 屏 —— 其中 2 屏是
 * 2237 行的分组表。分区后首屏只放概览（空间 + 目录分布），各块内容
 * 互不干扰（异常与孤儿各有独立空间，不会互相误点删除按钮）。
 *
 * 层级刻意是「下载器 Tab（外层，数据维度）→ 内容 Tab（内层，功能维度）」：
 * 用户的真实心智是「我要看 tr-172 这个下载器怎么样」——那是数据维度，
 * 不该被功能维度打散。
 */
type BlockTab = 'overview' | 'groups' | 'anomalies' | 'orphans'
const blockTab = ref<BlockTab>('overview')

const statsColumns = [
  { title: '原始名称', dataIndex: 'name', key: 'name', ellipsis: true, minWidth: 360, sorter: (a: CrossSeedStatsRow, b: CrossSeedStatsRow) => a.name.localeCompare(b.name) },
  { title: '大小 (GB)', dataIndex: 'size_gb', key: 'size_gb', width: 120, sorter: (a: CrossSeedStatsRow, b: CrossSeedStatsRow) => a.size_gb - b.size_gb },
  { title: '出现次数', dataIndex: 'count', key: 'count', width: 120, sorter: (a: CrossSeedStatsRow, b: CrossSeedStatsRow) => a.count - b.count },
  { title: '操作', key: 'actions', width: 100, fixed: 'right' as const },
]

/**
 * 次数分布表的列。
 *
 * ``count`` 是「出现次数」（= 一个分组被几个种子共享），按**降序**排——
 * 共享最厉害的排最前。
 */
const distColumns = [
  { title: '出现次数', dataIndex: 'count', key: 'count', width: 120, sorter: (a: DistributionRow, b: DistributionRow) => a.count - b.count },
  { title: '分组数', dataIndex: 'groups', key: 'groups', width: 120, sorter: (a: DistributionRow, b: DistributionRow) => a.groups - b.groups },
  { title: '涉及种子数', dataIndex: 'torrents', key: 'torrents', width: 140, sorter: (a: DistributionRow, b: DistributionRow) => a.torrents - b.torrents },
  { title: '涉及数据量', dataIndex: 'sizeBytes', key: 'sizeBytes', width: 160, sorter: (a: DistributionRow, b: DistributionRow) => a.sizeBytes - b.sizeBytes },
]

const anomalyColumns = [
  { title: '名称', dataIndex: 'name', key: 'name', ellipsis: true, minWidth: 260, sorter: (a: CrossSeedAnomaly, b: CrossSeedAnomaly) => a.name.localeCompare(b.name) },
  { title: 'Hash', dataIndex: 'hash', key: 'hash', width: 300, sorter: (a: CrossSeedAnomaly, b: CrossSeedAnomaly) => a.hash.localeCompare(b.hash) },
  { title: '异常类型', dataIndex: 'anomaly_type', key: 'anomaly_type', width: 180, sorter: (a: CrossSeedAnomaly, b: CrossSeedAnomaly) => a.anomaly_type.localeCompare(b.anomaly_type) },
  { title: '错误描述', dataIndex: 'error_string', key: 'error_string', ellipsis: true, minWidth: 220, sorter: (a: CrossSeedAnomaly, b: CrossSeedAnomaly) => a.error_string.localeCompare(b.error_string) },
  { title: '大小 (GB)', dataIndex: 'size_gb', key: 'size_gb', width: 110, sorter: (a: CrossSeedAnomaly, b: CrossSeedAnomaly) => a.size_gb - b.size_gb },
  { title: '状态', key: 'paused', width: 100, sorter: (a: CrossSeedAnomaly, b: CrossSeedAnomaly) => Number(a.paused) - Number(b.paused) },
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
  { title: '下载目录', dataIndex: 'dir', key: 'dir', ellipsis: true, minWidth: 320, sorter: (a: CrossSeedDirUsage, b: CrossSeedDirUsage) => a.dir.localeCompare(b.dir) },
  { title: '种子数', dataIndex: 'count', key: 'count', width: 100, sorter: (a: CrossSeedDirUsage, b: CrossSeedDirUsage) => a.count - b.count },
  { title: '声明占用', dataIndex: 'bytes', key: 'bytes', width: 140, sorter: (a: CrossSeedDirUsage, b: CrossSeedDirUsage) => a.bytes - b.bytes },
  { title: '声明总大小', dataIndex: 'total_bytes', key: 'total_bytes', width: 140, sorter: (a: CrossSeedDirUsage, b: CrossSeedDirUsage) => a.total_bytes - b.total_bytes },
]

const orphanColumns = [
  { title: '条目名', dataIndex: 'name', key: 'name', ellipsis: true, minWidth: 320, sorter: (a: CrossSeedOrphan, b: CrossSeedOrphan) => a.name.localeCompare(b.name) },
  { title: '占用', dataIndex: 'bytes', key: 'bytes', width: 140, sorter: (a: CrossSeedOrphan, b: CrossSeedOrphan) => a.bytes - b.bytes },
  { title: '文件数', dataIndex: 'file_count', key: 'file_count', width: 100, sorter: (a: CrossSeedOrphan, b: CrossSeedOrphan) => a.file_count - b.file_count },
]

//: 孤儿明细里勾选的条目（按 path 选中——path 唯一）
const selectedOrphanPaths = ref<string[]>([])
/** 孤儿删除确认弹框 */
const orphanDeleteModalOpen = ref(false)
/** 孤儿删除中 */
const orphanDeleting = ref(false)
/** 孤儿手动扫描中（实测 40–60 s） */
const orphanScanning = ref(false)

const scannedDirColumns = [
  { title: '扫描目录', dataIndex: 'scan_dir', key: 'scan_dir', ellipsis: true, minWidth: 320, sorter: (a: CrossSeedScannedDir, b: CrossSeedScannedDir) => a.scan_dir.localeCompare(b.scan_dir) },
  { title: '顶层条目', dataIndex: 'total_count', key: 'total_count', width: 100, sorter: (a: CrossSeedScannedDir, b: CrossSeedScannedDir) => a.total_count - b.total_count },
  { title: '有主', dataIndex: 'owned_count', key: 'owned_count', width: 90, sorter: (a: CrossSeedScannedDir, b: CrossSeedScannedDir) => a.owned_count - b.owned_count },
  { title: '孤儿', dataIndex: 'orphan_count', key: 'orphan_count', width: 90, sorter: (a: CrossSeedScannedDir, b: CrossSeedScannedDir) => a.orphan_count - b.orphan_count },
  { title: '其中空目录', dataIndex: 'empty_dir_count', key: 'empty_dir_count', width: 110, sorter: (a: CrossSeedScannedDir, b: CrossSeedScannedDir) => a.empty_dir_count - b.empty_dir_count },
  { title: '孤儿占用', dataIndex: 'orphan_bytes', key: 'orphan_bytes', width: 140, sorter: (a: CrossSeedScannedDir, b: CrossSeedScannedDir) => a.orphan_bytes - b.orphan_bytes },
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

/**
 * 手动扫描孤儿文件（不等 cron）。
 *
 * 孤儿扫描默认**每天一次**（cron `7 4 * * *`）——当天发现孤儿想现在清理，
 * 等不到明天。删除操作本身会触发重扫，但「只扫不删」原先没有入口。
 *
 * 耗时 40–60 s（扫盘 + 拉所有下载器 + 落库）——与「立即抓取」同一性质，
 * 按钮保持 loading 直到返回。
 */
async function scanOrphansNow() {
  orphanScanning.value = true
  try {
    const r = await api.post<CrossSeedOrphanScanResult>(
      '/cross-seed-monitor/orphans/scan',
    )
    // 响应里带上有孤儿表的新鲜统计 → 直接用，省一次请求
    if (r.orphans) {
      orphanSummary.value = r.orphans
    } else {
      await loadOrphans()
    }
    selectedOrphanPaths.value = []
    if (r.errors?.length) {
      message.warning(`扫描完成，但有 ${r.errors.length} 个下载器失败：${r.errors[0]}`)
    } else {
      message.success(r.summary || '扫描完成')
    }
  } catch (e) {
    message.error('扫描失败：' + errMsg(e))
  } finally {
    orphanScanning.value = false
  }
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
    if (r.permission_denied > 0) {
      // 权限失败：error 字段已含可行动提示（NAS 端改属主/权限或换挂载账号）
      // ——用 error 级（红色）醒目，区别于普通失败的 warning
      message.error(r.error || `${r.permission_denied} 个条目因权限不足删除失败`)
    } else if (r.failed > 0) {
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

/**
 * 分组 tab 的汇总数据（**纯前端从 ``snapshot.stats`` 算**，不改后端）。
 *
 * stats 里每行是一个 (name, size) 分组：
 * - ``count`` = 该组有几个种子（同名同大小）
 * - ``size_gb`` = 该组单份大小（GB，已 round(2)）
 *
 * 由此可算出「辅种的重复度」——同一个数据被几个种子共享。
 */
const groupStats = computed(() => {
  const stats = snapshot.value?.stats ?? []
  // size_gb 已 round(2)——按字节累加避免浮点误差，再转 GB 展示
  const GB = 1024 ** 3
  let totalTorrents = 0
  let sharedGroups = 0
  let redundantTorrents = 0
  let uniqueSizeBytes = 0
  let claimedSizeBytes = 0
  for (const s of stats) {
    const sizeBytes = Math.round(s.size_gb * GB)
    totalTorrents += s.count
    uniqueSizeBytes += sizeBytes
    claimedSizeBytes += sizeBytes * s.count
    if (s.count >= 2) {
      sharedGroups += 1
      redundantTorrents += s.count - 1
    }
  }
  const redundantRatio =
    totalTorrents > 0 ? (redundantTorrents / totalTorrents) * 100 : 0
  const avgShareCount =
    uniqueSizeBytes > 0 ? claimedSizeBytes / uniqueSizeBytes : 0
  return {
    totalGroups: stats.length,
    totalTorrents,
    sharedGroups,
    redundantTorrents,
    redundantRatio: redundantRatio.toFixed(1),
    uniqueSizeBytes,
    claimedSizeBytes,
    avgShareCount: avgShareCount.toFixed(2),
  }
})

/** 容量维度的格式化（字节 → GB/MB 自适应，沿用 ``fmtBytes``）。 */
function formatGroupSize(bytes: number): string {
  return fmtBytes(bytes)
}

/**
 * **次数分布**（用户要的核心视图）。
 *
 * 按「一个分组被多少个种子共享」聚合——即把 stats 里 count 相同的行
 * 归到一起：
 *
 * | 出现次数 | 分组数 | 涉及种子数 | 涉及数据量 |
 * |---|---|---|---|
 * | 13 | 3 | 39 | … |
 * | 10 | 3 | 30 | … |
 *
 * **为什么这个视角有用**：count=1 的组占绝大多数 → 说明辅种以「独占」
 * 为主；count 很大且组数不少 → 说明少量数据被大量站点共享（那才是
 * 真正「划算」的部分，也是 disk_rotation 优先清理的对象）。
 *
 * 与概览卡片是**互补**的：概览给总量，这里给形状（分布）。
 */
const countDistribution = computed<DistributionRow[]>(() => {
  const stats = snapshot.value?.stats ?? []
  const GB = 1024 ** 3
  /** count → 聚合桶 */
  const buckets = new Map<number, DistributionRow>()
  for (const s of stats) {
    const sizeBytes = Math.round(s.size_gb * GB)
    let b = buckets.get(s.count)
    if (!b) {
      b = { count: s.count, groups: 0, torrents: 0, sizeBytes: 0 }
      buckets.set(s.count, b)
    }
    b.groups += 1
    b.torrents += s.count
    b.sizeBytes += sizeBytes
  }
  // 按次数**降序**——「共享最厉害的」排最前，这是最想看的
  return [...buckets.values()].sort((a, b) => b.count - a.count)
})

/** 异常类型的中文标签。 */
const ANOMALY_LABELS: Record<string, string> = {
  tracker_not_registered: 'Tracker 未注册',
  no_data_found: '数据丢失',
  local_data_missing: '本地数据损坏',
  tracker_error: 'Tracker 报错',
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

/**
 * 快照的年龄（秒）；无法解析时返回 null。
 *
 * 用于判断「空间数字是不是已经过时」——辅种巡查的 cron 是
 * \`2,32 * * * *\`（每 30 分钟一次），所以超过 30 分钟就说明这份数据
 * 可能已经明显滞后（期间种子数、磁盘占用都可能变）。
 */
function snapshotAgeSeconds(scannedAt: string): number | null {
  if (!scannedAt) return null
  const d = new Date(scannedAt.replace(' ', 'T') + '+08:00')
  const ts = d.getTime()
  if (Number.isNaN(ts)) return null
  return Math.max(0, (Date.now() - ts) / 1000)
}

/**
 * 快照是否陈旧（超过一个抓取周期 = 3 小时）。
 *
 * 辅种维护作业 `cross_seed_maintenance` 的 cron 是每 3 小时一次
 * （2026-11-01 合并 scan_cross_seed / disk_rotation / scan_orphans 后，
 * 从原来的 30 分钟改成 3 小时）——阈值要跟着 cron 走，否则每次都会误报
 * 「陈旧」。
 */
const SNAPSHOT_STALE_SEC = 3 * 60 * 60

const isSnapshotStale = computed<boolean>(() => {
  const age = snapshotAgeSeconds(snapshot.value?.scanned_at ?? '')
  return age !== null && age > SNAPSHOT_STALE_SEC
})

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
          description="辅种数据由定时作业「辅种巡查」抓取后落库（默认每 30 分钟一次：2,32 * * * *）。若一直没有数据，请到「设置 → 调度作业」确认该作业已启用，或点右上角「立即抓取」。"
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

          <!-- 内容分区 Tab（内层）：下载器 Tab 是数据维度，这里是功能维度 -->
          <a-tabs v-model:active-key="blockTab" class="block-tabs" size="small">
          <a-tab-pane key="overview" tab="概览">
          <!-- 空间占用 -->
          <section class="space-block">
            <div class="space-block__header">
              <div class="label-caps">空间占用 · SPACE</div>
              <!--
                抓取时间（2026-10-26）：这 4 个数字全部来自**同一次**抓取，
                区块独立于顶部摘要——滚动下来就看不到时间了 → 在这里补上，
                并在数据陈旧时提示。
              -->
              <span class="dim mono">
                抓取于 {{ snapshot.scanned_at }}（{{ freshnessText(snapshot.scanned_at) }}）
              </span>
            </div>
            <a-alert
              v-if="isSnapshotStale"
              type="warning"
              show-icon
              message="这份空间数据已超过 30 分钟（辅种巡查每 30 分钟抓一次）——期间的种子增减与磁盘占用变化都不会反映在这里。"
              style="margin-bottom: var(--sp-2)"
            >
              <template #action>
                <a-button size="small" type="primary" :loading="refreshing" @click="refreshNow">
                  立即刷新
                </a-button>
              </template>
            </a-alert>
            <div class="space-tiles">
              <div class="space-tile">
                <div class="space-tile__label">种子声明占用</div>
                <div class="space-tile__value">{{ fmtBytes(snapshot.used_bytes) }}</div>
                <div class="space-tile__hint">共享数据重复计入，非磁盘真实占用</div>
              </div>
              <div class="space-tile">
                <div class="space-tile__label">种子总大小</div>
                <div class="space-tile__value">{{ fmtBytes(snapshot.total_size_bytes) }}</div>
                <div class="space-tile__hint">同样含共享重复计入</div>
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

            <!--
              口径说明（2026-10-26）。

              「种子声明占用」可能**大于**下面的「分区已用」——这是正常的，
              两个数字的范畴不同：

              - **种子声明占用** = 各种子上报的占用求和。辅种的本质是**同一条
                数据被多个种子共享**（实测 4646 个种子只对应 2237 份数据），
                共享的部分被重复计了 N 次。它只是辅种这一块「声明」了多少，
                **不是**磁盘真实占用。
              - **分区已用** = 磁盘总量 − 剩余，是**整个分区**的视角
                （含辅种之外的 VM / 备份 / 其它共享），这个才准。

              所以要看「磁盘还剩多少空间」，看剩余与进度条；要看「辅种声明了
              多少数据」，看上面那两个数。
            -->
            <a-alert
              type="info"
              show-icon
              message="口径说明：上面两个「种子」数字含共享数据重复计入（辅种场景下同一条数据常被多个种子共享），可能大于磁盘真实占用。磁盘实际用量看下面的「分区已用」——那是整个分区的口径，含辅种之外的数据。"
              style="margin-top: var(--sp-2)"
            />

            <!-- 占用进度条（总量未知时不画——画出来会误导成「磁盘已满」） -->
            <div v-if="diskUsagePercent !== null" class="space-progress">
              <div class="space-progress__header">
                <span>分区已用 {{ fmtBytes(diskUsedBytes) }}（含非辅种数据）</span>
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
              :default-sort-order="{ columnKey: 'bytes', order: 'descend' }"
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
          </a-tab-pane>

          <!-- 孤儿文件（磁盘有、任务没了） -->

          <a-tab-pane key="groups">
            <template #tab>分组</template>

          <!--
            分组汇总卡片（2026-11-03 加入）。

            数据**纯前端从 ``snapshot.stats`` 算**——不打任何下载器接口，
            不改后端（stats 字段早已落库 cross_seed_snapshot，前端现成可用）。

            7 个指标分两层：
            - 上层「数量维度」：分组数 / 种子数 / 共享分组 / 冗余种子 / 冗余占比
            - 下层「容量维度」：唯一数据量 / 种子声明总量

            **口径说明**：
            - 「唯一数据量」= 各分组 size 求和（一份共享数据只算一次）
            - 「种子声明总量」= 各分组 size × count 求和（共享的算 N 次）
            - 后者 ÷ 前者 = 「平均每份数据被几个种子共享」，与冗余占比互为印证
              （一个看「种子数冗余」，一个看「数据量冗余」）
            - 两者都含**共享数据重复计入**，不等于磁盘真实占用（与概览页同口径）
          -->
          <div class="group-summary">
            <div class="group-summary__row">
              <div class="space-tile">
                <div class="space-tile__label">分组总数</div>
                <div class="space-tile__value">{{ groupStats.totalGroups }}</div>
                <div class="space-tile__hint">唯一数据份数（按名称+大小分组）</div>
              </div>
              <div class="space-tile">
                <div class="space-tile__label">种子总数</div>
                <div class="space-tile__value">{{ groupStats.totalTorrents }}</div>
                <div class="space-tile__hint">所有分组 count 求和（应等于顶部摘要的「共 N 种子」）</div>
              </div>
              <div class="space-tile">
                <div class="space-tile__label">共享分组</div>
                <div class="space-tile__value">{{ groupStats.sharedGroups }}</div>
                <div class="space-tile__hint">count ≥ 2 的分组数（被多个种子共享的数据）</div>
              </div>
              <div class="space-tile">
                <div class="space-tile__label">冗余种子</div>
                <div class="space-tile__value">{{ groupStats.redundantTorrents }}</div>
                <div class="space-tile__hint">Σ (count − 1)，即「多出来的辅种个数」</div>
              </div>
              <div class="space-tile">
                <div class="space-tile__label">冗余占比</div>
                <div class="space-tile__value">{{ groupStats.redundantRatio }}%</div>
                <div class="space-tile__hint">冗余种子 ÷ 种子总数（辅种的「重复度」）</div>
              </div>
            </div>
            <div class="group-summary__row">
              <div class="space-tile">
                <div class="space-tile__label">唯一数据量</div>
                <div class="space-tile__value">{{ formatGroupSize(groupStats.uniqueSizeBytes) }}</div>
                <div class="space-tile__hint">各分组 size 求和（一份共享数据只算一次，含共享重复计入）</div>
              </div>
              <div class="space-tile">
                <div class="space-tile__label">种子声明总量</div>
                <div class="space-tile__value">{{ formatGroupSize(groupStats.claimedSizeBytes) }}</div>
                <div class="space-tile__hint">Σ size × count（共享数据算 N 次，含共享重复计入）</div>
              </div>
              <div class="space-tile">
                <div class="space-tile__label">平均共享数</div>
                <div class="space-tile__value">{{ groupStats.avgShareCount }}</div>
                <div class="space-tile__hint">种子声明总量 ÷ 唯一数据量（平均每份数据被几个种子共享）</div>
              </div>
            </div>
          </div>

          <!--
            **次数分布**（用户要的核心视图，2026-11-03 加入）。

            把明细表里 count 相同的行聚合成一行：
            「出现 13 次的有 3 个分组」「出现 10 次的有 3 个分组」……

            **为什么这个视角有用**：count=1 的组占绝大多数 → 辅种以「独占」
            为主；count 很大且组数不少 → 少量数据被大量站点共享（那才是
            真正「划算」的部分，也是 disk_rotation 优先清理的对象）。
            概览卡片给总量，这里给**形状**。
          -->
          <section v-if="countDistribution.length" class="space-block">
            <div class="space-block__header">
              <div class="label-caps">次数分布 · DISTRIBUTION</div>
              <span class="dim">
                按「一个分组被几个种子共享」聚合 · 共 {{ countDistribution.length }} 种次数
              </span>
            </div>
            <a-table
              :columns="distColumns"
              :data-source="countDistribution"
              :row-key="(r: DistributionRow) => String(r.count)"
              :pagination="false"
              size="small"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'count'">
                  <a-tag :color="countColor((record as DistributionRow).count)">
                    {{ (record as DistributionRow).count }}
                  </a-tag>
                </template>
                <template v-else-if="column.key === 'sizeBytes'">
                  {{ fmtBytes((record as DistributionRow).sizeBytes) }}
                </template>
              </template>
            </a-table>
          </section>

          <a-alert
            type="info"
            show-icon
            message="按原始名称 + 大小精确分组, 不做归一化 (集数/分辨率/制作组差异都算不同)"
            style="margin-bottom: var(--sp-3)"
          />

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
          </a-tab-pane>

          <a-tab-pane key="anomalies">
            <template #tab>
              异常
              <a-badge
                v-if="snapshot.anomalies.length"
                :count="snapshot.anomalies.length"
                :title="String(snapshot.anomalies.length)"
                :number-style="{ backgroundColor: '#fa8c16', fontSize: '11px', height: '16px', lineHeight: '16px', padding: '0 5px' }"
              />
            </template>

          <!-- 异常明细 -->
          <div v-if="snapshot.anomalies.length">
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
                :default-sort-order="{ columnKey: 'size_gb', order: 'descend' }"
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
                  <template v-else-if="column.key === 'size_gb'">
                    {{ (record as CrossSeedAnomaly).size_gb }}
                  </template>
                  <template v-else-if="column.key === 'paused'">
                    <a-tag v-if="(record as CrossSeedAnomaly).anomaly_type === 'tracker_error'" color="orange">
                      不暂停
                    </a-tag>
                    <a-tag v-else-if="(record as CrossSeedAnomaly).paused" color="default">
                      已暂停
                    </a-tag>
                    <a-tag v-else color="red">待暂停</a-tag>
                  </template>
                </template>
              </a-table>
          </div>
          </a-tab-pane>
          <a-tab-pane key="orphans">
            <template #tab>
              孤儿
              <a-badge
                v-if="currentOrphan && currentOrphan.orphan_count"
                :count="currentOrphan.orphan_count"
                :title="String(currentOrphan.orphan_count)"
                :number-style="{ backgroundColor: '#fa8c16', fontSize: '11px', height: '16px', lineHeight: '16px', padding: '0 5px' }"
              />
            </template>
          <section class="space-block">
            <div class="space-block__header">
              <div class="label-caps">孤儿文件 · ORPHANS</div>
              <!--
                手动扫描入口（2026-10-26）：孤儿扫描默认**每天一次**
                （cron 7 4 * * *）——当天发现孤儿想现在清理，等不到明天。
                耗时 40–60 s，与「立即抓取」同一性质。
              -->
              <div class="space-block__actions">
                <span v-if="currentOrphan" class="dim mono">
                  扫描于 {{ currentOrphan.scanned_at }}（{{ freshnessText(currentOrphan.scanned_at) }}）
                </span>
                <a-button
                  size="small"
                  :loading="orphanScanning"
                  @click="scanOrphansNow"
                >
                  立即扫描
                </a-button>
              </div>
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
              description="孤儿文件由定时作业每天扫一次盘得出（需把辅种站的下载目录挂载到容器）。可以点右上角「立即扫描」现在就跑一次。"
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
                  <div class="space-tile__hint">不再做种却仍占着空间</div>
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
                :default-sort-order="{ columnKey: 'orphan_bytes', order: 'descend' }"
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

              <div v-if="currentOrphan.orphans.length" style="margin-top: var(--sp-3)">
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
                    :default-sort-order="{ columnKey: 'bytes', order: 'descend' }"
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
              </div>
            </template>
          </section>
          </a-tab-pane>
          </a-tabs>
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

/*
 * 内层内容 Tab（概览 / 分组 / 异常 / 孤儿）。
 *
 * 与外层「下载器 Tab」区分开：外层是数据维度（哪个下载器），内层是功能
 * 维度（看什么）。内层用小号 tab + 更少的留白，避免与外层抢视觉层级。
 */
.block-tabs :deep(.ant-tabs-nav) {
  margin-bottom: var(--sp-3);
}

.block-tabs :deep(.ant-tabs-tab) {
  padding: 6px 0;
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

/* 空间区块的标题行：标题 + 抓取时间（同一行，靠右对齐） */
.space-block__header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--sp-2);
  margin-bottom: var(--sp-2);
  flex-wrap: wrap;
}

/* 标题行右侧的操作区（时间标记 + 按钮） */
.space-block__actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}

.space-tiles {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--sp-3);
}

/* 分组 tab 的汇总卡片：两层，每层 5 / 3 个 tile */
.group-summary {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  margin-bottom: var(--sp-3);
}

.group-summary__row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
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
