<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import { api } from '../api'
import { message } from 'ant-design-vue'

interface DownloaderItem {
  id: number
  name: string
  type: string
  url: string
  enabled: boolean
  is_cross_seed: boolean
}

interface StatsRow {
  name: string
  size_gb: number
  count: number
  sample_hash: string
}

interface ScanResult {
  downloader_id: number
  downloader_name: string
  scanned_at: string
  total_torrents: number
  anomaly_count: number
  unique_count: number
  stats: StatsRow[]
}

const downloaders = ref<DownloaderItem[]>([])
const selectedId = ref<number | null>(null)
const scanResult = ref<ScanResult | null>(null)
const loading = ref(false)

const statsColumns = [
  { title: '原始名称', dataIndex: 'name', key: 'name', ellipsis: true, minWidth: 360 },
  { title: '大小 (GB)', dataIndex: 'size_gb', key: 'size_gb', width: 110, sorter: (a: StatsRow, b: StatsRow) => a.size_gb - b.size_gb },
  { title: '出现次数', dataIndex: 'count', key: 'count', width: 110, sorter: (a: StatsRow, b: StatsRow) => a.count - b.count },
  { title: '样本 hash', dataIndex: 'sample_hash', key: 'sample_hash', width: 120, className: 'hash-cell' },
]

async function loadDownloaders() {
  try {
    downloaders.value = await api.get<DownloaderItem[]>('/cross-seed-monitor/downloaders')
    if (downloaders.value.length) {
      selectedId.value = downloaders.value[0].id
    }
  } catch (e) {
    message.error('加载下载器失败：' + (e as Error).message)
  }
}

async function loadScan(id: number) {
  loading.value = true
  try {
    scanResult.value = await api.get<ScanResult>(
      `/cross-seed-monitor/downloaders/${id}/scan`
    )
  } catch (e) {
    message.error('扫描失败：' + (e as Error).message)
    scanResult.value = null
  } finally {
    loading.value = false
  }
}

watch(selectedId, (id) => {
  if (id != null) loadScan(id)
})

function countColor(c: number) {
  if (c >= 5) return 'red'
  if (c >= 3) return 'orange'
  return 'default'
}

onMounted(async () => {
  await loadDownloaders()
  if (selectedId.value != null) {
    await loadScan(selectedId.value)
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
          按下载器分 Tab, 展示 (名称, 大小) 相同的种子分组 · cron 每 6h 自动巡查 + 暂停异常
        </div>
      </div>
    </header>

    <a-empty
      v-if="!downloaders.length"
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
        </template>
        <div v-if="scanResult && scanResult.downloader_id === dl.id">
          <div class="summary-bar">
            <span>扫描时间: {{ scanResult.scanned_at }}</span>
            <span class="dot">·</span>
            <span>共 {{ scanResult.total_torrents }} 种子</span>
            <span class="dot">·</span>
            <span>{{ scanResult.unique_count }} 个唯一分组</span>
            <span class="dot">·</span>
            <a-tag v-if="scanResult.anomaly_count" color="orange">
              cron 巡查发现 {{ scanResult.anomaly_count }} 个异常 (已自动暂停)
            </a-tag>
            <a-tag v-else color="green">无异常</a-tag>
          </div>
          <a-alert
            type="info"
            show-icon
            message="按原始名称 + 大小精确分组, 不做归一化 (集数/分辨率/制作组差异都算不同)"
            style="margin-bottom: var(--sp-3)"
          />
          <a-table
            :columns="statsColumns"
            :data-source="scanResult.stats"
            :row-key="(r: StatsRow) => r.name + '|' + r.size_gb"
            :pagination="{ pageSize: 50, showSizeChanger: true }"
            :default-sort-order="{ columnKey: 'count', order: 'descend' }"
            size="middle"
            :loading="loading"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'name'">
                <a-tooltip :title="(record as StatsRow).name">
                  <span class="name-cell">{{ (record as StatsRow).name }}</span>
                </a-tooltip>
              </template>
              <template v-else-if="column.key === 'size_gb'">
                {{ (record as StatsRow).size_gb }} GB
              </template>
              <template v-else-if="column.key === 'count'">
                <a-tag :color="countColor((record as StatsRow).count)">
                  {{ (record as StatsRow).count }}
                </a-tag>
              </template>
              <template v-else-if="column.key === 'sample_hash'">
                <a-tooltip
                  :title="`用于去重决策参考, 完整 hash 见 tr Web UI (前 12 位)`"
                  placement="top"
                >
                  <span class="mono hash-cell">{{ (record as StatsRow).sample_hash }}</span>
                </a-tooltip>
              </template>
            </template>
          </a-table>
        </div>
      </a-tab-pane>
    </a-tabs>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: var(--sp-4);
}

.page-header__eyebrow {
  font-size: var(--text-2xs);
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--text-tertiary);
  margin-bottom: 4px;
}

.page-header__title {
  font-size: var(--text-2xl);
  font-weight: 600;
  margin: 0 0 6px;
  color: var(--text);
}

.page-header__sub {
  font-size: var(--text-sm);
  color: var(--text-secondary);
}

.content-tabs {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: var(--sp-4);
  min-height: 400px;
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

.hash-cell {
  color: var(--text-tertiary);
  font-size: var(--text-xs);
  cursor: help;
}
</style>
