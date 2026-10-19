<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { api } from '../api'
import { message } from 'ant-design-vue'
import HistoryStats from './HistoryStats.vue'
import JobStepList, { type JobStep } from './JobStepList.vue'

interface RunItem {
  id: number
  job_name: string
  trigger: string
  started_at: string
  finished_at: string | null
  duration_ms: number | null
  ok: boolean
  error: string | null
  result: Record<string, unknown> | null
}

const props = defineProps<{
  open: boolean
  jobName: string
  jobLabel: string
}>()

const emit = defineEmits<{
  (e: 'update:open', v: boolean): void
}>()

const runs = ref<RunItem[]>([])
const loading = ref(false)
const selectedRun = ref<RunItem | null>(null)

watch(
  () => props.open,
  async (o) => {
    if (o) {
      selectedRun.value = null
      await load()
    }
  }
)

async function load() {
  loading.value = true
  try {
    const r = await api.get<{ items: RunItem[] }>(
      `/scheduler/runs?job_name=${props.jobName}&limit=100`
    )
    runs.value = r.items
  } catch (e) {
    message.error('加载历史失败：' + (e as Error).message)
  } finally {
    loading.value = false
  }
}

// ---------------------------------------------------------- 步骤提取
function getSteps(r: RunItem | null): JobStep[] {
  if (!r?.result) return []
  const steps = (r.result as Record<string, unknown>).steps
  if (!Array.isArray(steps)) return []
  return steps.filter(
    (s): s is JobStep =>
      !!s && typeof s === 'object' && typeof (s as JobStep).name === 'string'
  )
}

function formatResult(r: unknown): string {
  if (!r || typeof r !== 'object') return ''
  const obj = r as Record<string, unknown>
  const out: string[] = []
  // 人话摘要优先（后端是人话来源）
  const summary = obj.summary
  if (typeof summary === 'string' && summary) {
    out.push(`摘要: ${summary}`)
  }
  for (const [k, v] of Object.entries(obj)) {
    if (k === 'steps' || k === 'summary') continue
    out.push(`${k}: ${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
  }
  return out.join('\n')
}

// ---------------------------------------------------------- 顶部统计
const stats = computed(() => {
  const list = runs.value
  if (!list.length) return null
  const okCount = list.filter((r) => r.ok).length
  const durations = list
    .map((r) => r.duration_ms)
    .filter((d): d is number => typeof d === 'number')
  const avgMs = durations.length
    ? Math.round(durations.reduce((a, b) => a + b, 0) / durations.length)
    : 0
  const maxMs = durations.length ? Math.max(...durations) : 0
  // 最近一次的结果摘要：优先后端 summary，兜底取最后一步 detail
  const latest = list[0]
  const latestSteps = getSteps(latest)
  const rawSummary = latest?.result?.summary
  const summary =
    typeof rawSummary === 'string' && rawSummary
      ? rawSummary
      : latestSteps.length
        ? latestSteps[latestSteps.length - 1].detail || latestSteps[latestSteps.length - 1].name
        : latest?.error
          ? '失败'
          : '—'
  return {
    total: list.length,
    okCount,
    okPct: Math.round((okCount / list.length) * 100),
    avgMs,
    maxMs,
    summary,
  }
})

const columns = [
  { title: '开始时间', dataIndex: 'started_at', key: 'started_at', width: 175 },
  { title: '触发', dataIndex: 'trigger', key: 'trigger', width: 70 },
  { title: '耗时', dataIndex: 'duration_ms', key: 'duration_ms', width: 95 },
  { title: '状态', dataIndex: 'ok', key: 'ok', width: 80 },
  { title: '步骤', key: 'steps', width: 90 },
  { title: '错误', dataIndex: 'error', key: 'error', ellipsis: true },
]

/** Antd Table custom-row: 给 <tr> 绑 click 事件（比 @rowClick 可靠）。 */
function customRow(record: RunItem) {
  return {
    onClick: () => {
      selectedRun.value = record
    },
  }
}
</script>

<template>
  <a-modal
    :open="open"
    :title="`运行历史 · ${jobLabel} (${jobName})`"
    :footer="null"
    width="960px"
    @cancel="emit('update:open', false)"
  >
    <!-- 顶部统计 -->
    <HistoryStats
      v-if="stats"
      :ok-pct="stats.okPct"
      :ok-count="stats.okCount"
      :total="stats.total"
      :avg-ms="stats.avgMs"
      :max-ms="stats.maxMs"
      :summary="stats.summary"
    />

    <div class="history-toolbar">
      <span class="dim mono">最近 100 条 · 点击行查看步骤</span>
      <a-button size="small" :loading="loading" @click="load">刷新</a-button>
    </div>

    <div class="history-grid">
      <div class="history-list">
        <a-table
          :columns="columns"
          :data-source="runs"
          row-key="id"
          size="small"
          :loading="loading"
          :pagination="{ pageSize: 15, showSizeChanger: false }"
          :row-class-name="(r: RunItem) => selectedRun?.id === r.id ? 'row-selected' : ''"
          :custom-row="customRow"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'started_at'">
              <span class="mono dim">{{ record.started_at }}</span>
            </template>
            <template v-else-if="column.key === 'duration_ms'">
              <span class="mono dim">{{ record.duration_ms ?? '—' }} ms</span>
            </template>
            <template v-else-if="column.key === 'ok'">
              <a-tag :color="record.ok ? 'green' : 'red'" size="small">
                {{ record.ok ? 'OK' : 'FAIL' }}
              </a-tag>
            </template>
            <template v-else-if="column.key === 'steps'">
              <JobStepList
                v-if="getSteps(record).length"
                :steps="getSteps(record)"
                compact
              />
              <span v-else class="dim">—</span>
            </template>
            <template v-else-if="column.key === 'error'">
              <span v-if="record.error" class="error-text">{{ record.error }}</span>
              <span v-else class="dim">—</span>
            </template>
          </template>
        </a-table>
      </div>

      <div class="history-detail">
        <div v-if="!selectedRun" class="empty-detail">
          <span class="dim">点击左侧行查看详情</span>
        </div>
        <div v-else>
          <div class="detail-row">
            <span class="detail-label">开始</span>
            <span class="mono">{{ selectedRun.started_at }}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">结束</span>
            <span class="mono">{{ selectedRun.finished_at ?? '—' }}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">耗时</span>
            <span class="mono">{{ selectedRun.duration_ms ?? '—' }} ms</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">触发</span>
            <span class="mono">{{ selectedRun.trigger }}</span>
          </div>

          <!-- 步骤卡片 -->
          <div v-if="getSteps(selectedRun).length" class="detail-section">
            <div class="detail-label">任务步骤</div>
            <JobStepList :steps="getSteps(selectedRun)" />
          </div>
          <div v-else-if="selectedRun.result" class="detail-section">
            <div class="detail-label">结果</div>
            <pre class="result-pre">{{ formatResult(selectedRun.result) }}</pre>
          </div>

          <div v-if="selectedRun.error" class="detail-row detail-row--error">
            <span class="detail-label">错误</span>
            <span class="error-text">{{ selectedRun.error }}</span>
          </div>
        </div>
      </div>
    </div>
  </a-modal>
</template>

<style scoped>
.dim { color: var(--text-tertiary); }
.mono { font-family: var(--font-mono, monospace); }

.history-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--sp-2);
  font-size: var(--text-xs);
}

.history-grid {
  display: grid;
  grid-template-columns: 1.25fr 1fr;
  gap: var(--sp-3);
  min-height: 420px;
}

.history-list :deep(.row-selected) {
  background: var(--brand-50, #fef3ee);
}
.history-list :deep(.row-selected:hover > td) {
  background: var(--brand-50, #fef3ee) !important;
}

.history-detail {
  background: var(--bg-inset);
  border-radius: var(--r-md);
  padding: var(--sp-3);
  font-size: var(--text-sm);
  overflow: auto;
  max-height: 520px;
}
.empty-detail {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  min-height: 320px;
}
.detail-row {
  display: flex;
  gap: var(--sp-2);
  margin-bottom: var(--sp-2);
  align-items: baseline;
  flex-wrap: wrap;
}
.detail-row--error {
  padding: var(--sp-2);
  background: var(--error-bg-subtle, rgba(245, 34, 45, 0.04));
  border-radius: var(--r-sm);
  margin-top: var(--sp-2);
}
.detail-label {
  font-weight: 500;
  color: var(--text-secondary);
  min-width: 50px;
  font-size: var(--text-xs);
  letter-spacing: 0.05em;
  text-transform: uppercase;
}
.detail-section {
  margin-top: var(--sp-3);
  padding-top: var(--sp-3);
  border-top: 1px solid var(--border-hairline);
}

.result-pre {
  font-family: var(--font-mono, monospace);
  font-size: var(--text-xs);
  white-space: pre-wrap;
  word-break: break-all;
  margin: var(--sp-2) 0 0;
  padding: var(--sp-2);
  background: var(--bg-surface);
  border-radius: var(--r-sm);
  max-height: 200px;
  overflow: auto;
}
.error-text {
  color: var(--error-text, #f5222d);
  font-family: var(--font-mono, monospace);
  font-size: var(--text-xs);
}
</style>
