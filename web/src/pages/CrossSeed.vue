<script setup lang="ts">
import { ref, onMounted, watch, computed } from 'vue'
import { api, errMsg } from '../api'
import type {
  CrossSeedAnomaly,
  CrossSeedDirUsage,
  CrossSeedDownloader,
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
]

const anomalyColumns = [
  { title: '名称', dataIndex: 'name', key: 'name', ellipsis: true, minWidth: 260 },
  { title: 'Hash', dataIndex: 'hash', key: 'hash', width: 300 },
  { title: '异常类型', dataIndex: 'anomaly_type', key: 'anomaly_type', width: 180 },
  { title: '错误描述', dataIndex: 'error_string', key: 'error_string', ellipsis: true, minWidth: 220 },
  { title: '状态', key: 'paused', width: 100 },
]

const dirColumns = [
  { title: '下载目录', dataIndex: 'dir', key: 'dir', ellipsis: true, minWidth: 320 },
  { title: '种子数', dataIndex: 'count', key: 'count', width: 100 },
  { title: '实际占用', dataIndex: 'bytes', key: 'bytes', width: 140 },
  { title: '总大小', dataIndex: 'total_bytes', key: 'total_bytes', width: 140 },
]

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
  if (id != null) loadDetail(id)
})

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
  await loadSnapshotList()
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
              <a-table
                :columns="anomalyColumns"
                :data-source="snapshot.anomalies"
                :row-key="(r: CrossSeedAnomaly) => r.hash"
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
            </template>
          </a-table>
        </div>
      </a-tab-pane>
    </a-tabs>
    </section>
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
