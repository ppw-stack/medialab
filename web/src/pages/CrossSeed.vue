<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { api } from '../api'
import { message, Modal } from 'ant-design-vue'

interface DownloaderItem {
  id: number
  name: string
  type: string
  url: string
  enabled: boolean
  is_cross_seed: boolean
}

interface AnomalyItem {
  id: number
  hash: string
  name: string
  size_gb: number
  status: number
  anomaly_type: 'tracker_not_registered' | 'no_data_found'
  error_code: number
  error_string: string
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
  anomalies: AnomalyItem[]
  stats: StatsRow[]
}

const downloaders = ref<DownloaderItem[]>([])
const selectedId = ref<number | null>(null)
const scanResult = ref<ScanResult | null>(null)
const loading = ref(false)
const activeTab = ref<'anomalies' | 'stats' | 'downloaders'>('anomalies')
const selectedHashes = ref<string[]>([])
const scanning = ref(false)

const anomalyColumns = [
  { title: '类型', dataIndex: 'anomaly_type', key: 'anomaly_type', width: 140 },
  { title: '名称', dataIndex: 'name', key: 'name', ellipsis: true },
  { title: '大小', dataIndex: 'size_gb', key: 'size_gb', width: 100 },
  { title: '状态', dataIndex: 'status', key: 'status', width: 80 },
  { title: '错误信息', dataIndex: 'error_string', key: 'error_string', ellipsis: true },
]

const statsColumns = [
  { title: '原始名称', dataIndex: 'name', key: 'name', width: 480 },
  { title: '大小 (GB)', dataIndex: 'size_gb', key: 'size_gb', width: 120, sorter: (a: StatsRow, b: StatsRow) => a.size_gb - b.size_gb },
  { title: '出现次数', dataIndex: 'count', key: 'count', width: 120, sorter: (a: StatsRow, b: StatsRow) => a.count - b.count },
  { title: '样本 hash', dataIndex: 'sample_hash', key: 'sample_hash', width: 160 },
]

const downloaderColumns = [
  { title: 'ID', dataIndex: 'id', key: 'id', width: 60 },
  { title: '名称', dataIndex: 'name', key: 'name' },
  { title: '类型', dataIndex: 'type', key: 'type', width: 120 },
  { title: 'URL', dataIndex: 'url', key: 'url', ellipsis: true },
  { title: '状态', key: 'enabled', width: 80 },
]

const selectedDownloader = computed(() =>
  downloaders.value.find((d) => d.id === selectedId.value) || null
)

async function loadDownloaders() {
  try {
    downloaders.value = await api.get<DownloaderItem[]>('/cross-seed-monitor/downloaders')
    if (downloaders.value.length && selectedId.value == null) {
      selectedId.value = downloaders.value[0].id
      await loadScan()
    }
  } catch (e) {
    message.error('加载下载器失败：' + (e as Error).message)
  }
}

async function loadScan() {
  if (!selectedId.value) return
  loading.value = true
  try {
    scanResult.value = await api.get<ScanResult>(
      `/cross-seed-monitor/downloaders/${selectedId.value}/scan`
    )
    selectedHashes.value = []
  } catch (e) {
    message.error('扫描失败：' + (e as Error).message)
  } finally {
    loading.value = false
  }
}

async function scanAll() {
  scanning.value = true
  try {
    const r = await api.post<{ scanned_downloaders: number; results: ScanResult[] }>(
      '/cross-seed-monitor/downloaders/scan-all'
    )
    message.success(`已扫描 ${r.scanned_downloaders} 个下载器`)
    // 刷新当前选中的
    if (selectedId.value) {
      await loadScan()
    }
  } catch (e) {
    message.error('扫描失败：' + (e as Error).message)
  } finally {
    scanning.value = false
  }
}

async function doStop() {
  if (!selectedId.value || !selectedHashes.value.length) return
  const dl = selectedDownloader.value
  if (!dl) return

  Modal.confirm({
    title: '二次确认',
    content: `即将暂停 ${dl.name} 的 ${selectedHashes.value.length} 个异常种子。
暂停后数据文件保留, 可在下载器 Web UI 手动 resume。`,
    okText: '确认暂停',
    cancelText: '取消',
    okButtonProps: { danger: true },
    onOk: async () => {
      try {
        const r = await api.post<{ stopped: number; failed: number }>(
          `/cross-seed-monitor/downloaders/${selectedId.value}/stop`,
          {
            hashes: selectedHashes.value,
            confirm: true,
          }
        )
        message.success(`已暂停 ${r.stopped} 个, 失败 ${r.failed} 个`)
        await loadScan()
      } catch (e) {
        message.error('暂停失败：' + (e as Error).message)
      }
    },
  })
}

function typeLabel(t: string) {
  return t === 'no_data_found' ? '数据丢失' : 'tracker 未注册'
}

function typeColor(t: string) {
  return t === 'no_data_found' ? 'red' : 'orange'
}

function countColor(c: number) {
  if (c >= 5) return 'red'
  if (c >= 3) return 'orange'
  return 'default'
}

onMounted(loadDownloaders)
</script>

<template>
  <div>
    <header class="page-header">
      <div>
        <div class="page-header__eyebrow">辅种 · CROSS-SEED</div>
        <h1 class="page-header__title">辅种下载器巡查</h1>
        <div class="page-header__sub">
          实时检测 tracker 未注册 / 数据丢失两类异常 → 自动暂停 (不删)
        </div>
      </div>
      <div class="page-header__actions">
        <a-space>
          <a-button @click="loadScan" :loading="loading" :disabled="!selectedId">
            刷新当前
          </a-button>
          <a-button @click="scanAll" :loading="scanning" type="primary" ghost>
            扫描全部
          </a-button>
        </a-space>
      </div>
    </header>

    <div class="filter-bar">
      <a-form layout="inline">
        <a-form-item label="下载器">
          <a-select
            v-model:value="selectedId"
            placeholder="选择 is_cross_seed 标记的下载器"
            style="width: 320px"
            :options="downloaders.map((d) => ({ value: d.id, label: d.name + ' (' + d.type + ')' }))"
            @change="loadScan"
          />
        </a-form-item>
        <a-form-item v-if="scanResult">
          <span class="text-secondary">
            最后扫描: {{ scanResult.scanned_at }} / 共 {{ scanResult.total_torrents }} 种子
          </span>
        </a-form-item>
      </a-form>
    </div>

    <a-tabs v-model:active-key="activeTab" class="content-tabs">
      <!-- 异常 Tab -->
      <a-tab-pane key="anomalies">
        <template #tab>
          巡查 ({{ scanResult?.anomaly_count ?? 0 }})
        </template>
        <div v-if="scanResult?.anomalies.length">
          <div class="bulk-actions">
            <span>已选 {{ selectedHashes.length }} 个</span>
            <a-button
              type="primary"
              danger
              :disabled="!selectedHashes.length"
              @click="doStop"
            >
              暂停选中
            </a-button>
          </div>
          <a-table
            :columns="anomalyColumns"
            :data-source="scanResult.anomalies"
            :row-key="(r: AnomalyItem) => r.hash"
            :row-selection="{ selectedRowKeys: selectedHashes, onChange: (keys: any[]) => selectedHashes = keys as string[] }"
            :pagination="{ pageSize: 50, showSizeChanger: true }"
            size="middle"
            :loading="loading"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'anomaly_type'">
                <a-tag :color="typeColor((record as AnomalyItem).anomaly_type)">
                  {{ typeLabel((record as AnomalyItem).anomaly_type) }}
                </a-tag>
              </template>
              <template v-else-if="column.key === 'size_gb'">
                {{ (record as AnomalyItem).size_gb }} GB
              </template>
              <template v-else-if="column.key === 'status'">
                {{ (record as AnomalyItem).status }}
              </template>
              <template v-else-if="column.key === 'error_string'">
                <a-tooltip :title="(record as AnomalyItem).error_string">
                  <span class="mono text-sm">{{ (record as AnomalyItem).error_string }}</span>
                </a-tooltip>
              </template>
            </template>
          </a-table>
        </div>
        <a-empty v-else-if="scanResult" description="本次扫描无异常" />
        <a-empty v-else description="请选择下载器" />
      </a-tab-pane>

      <!-- 统计 Tab -->
      <a-tab-pane key="stats">
        <template #tab>
          分类 ({{ scanResult?.unique_count ?? 0 }} 组)
        </template>
        <a-alert
          v-if="scanResult"
          type="info"
          show-icon
          message="按原始名称 + 大小精确分组, 不做归一化 (集数/分辨率/制作组差异都算不同)"
          style="margin-bottom: var(--sp-3)"
        />
        <a-table
          v-if="scanResult"
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
              <span class="mono text-sm">{{ (record as StatsRow).sample_hash }}</span>
            </template>
          </template>
        </a-table>
        <a-empty v-else description="请选择下载器" />
      </a-tab-pane>

      <!-- 下载器 Tab -->
      <a-tab-pane key="downloaders">
        <template #tab>
          下载器 ({{ downloaders.length }})
        </template>
        <a-table
          :columns="downloaderColumns"
          :data-source="downloaders"
          :row-key="(d: DownloaderItem) => d.id"
          :pagination="false"
          size="middle"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'enabled'">
              <a-tag :color="(record as DownloaderItem).enabled ? 'green' : 'default'">
                {{ (record as DownloaderItem).enabled ? '启用' : '禁用' }}
              </a-tag>
            </template>
            <template v-else-if="column.key === 'type'">
              <a-tag color="blue">{{ (record as DownloaderItem).type }}</a-tag>
            </template>
          </template>
        </a-table>
        <a-alert
          v-if="!downloaders.length"
          type="warning"
          message="无辅种下载器"
          description="在「下载器」页将需要的实例勾选 is_cross_seed, 然后回到此页。"
          show-icon
        />
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

.filter-bar {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: var(--sp-3) var(--sp-4);
  margin-bottom: var(--sp-4);
}

.content-tabs {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: var(--sp-4);
  min-height: 400px;
}

.bulk-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  margin-bottom: var(--sp-3);
  padding: var(--sp-2) var(--sp-3);
  background: var(--bg-inset);
  border-radius: var(--r-sm);
}

.text-secondary {
  color: var(--text-secondary);
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
