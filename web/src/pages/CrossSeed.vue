<script setup lang="ts">
import { ref, onMounted, watch, computed } from 'vue'
import { api, errMsg } from '../api'
import type {
  CrossSeedAnomaly,
  CrossSeedDownloader,
  CrossSeedSnapshot,
  CrossSeedSnapshotHeader,
  CrossSeedSnapshotList,
  CrossSeedStatsRow,
} from '../types'
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
