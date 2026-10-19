<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { api } from '../api'
import { message } from 'ant-design-vue'

interface JobStep {
  name: string
  status: 'ok' | 'warn' | 'skip' | 'fail'
  count: number | null
  detail: string | null
}

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

function statusIcon(s: string): string {
  switch (s) {
    case 'ok': return '✓'
    case 'warn': return '⚠'
    case 'fail': return '✕'
    default: return '○'
  }
}

function stepClass(s: string): string {
  return `step--${s}`
}

function formatResult(r: unknown): string {
  if (!r || typeof r !== 'object') return ''
  const out: string[] = []
  for (const [k, v] of Object.entries(r as Record<string, unknown>)) {
    if (k === 'steps') continue
    out.push(`${k}: ${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
  }
  return out.join('\n')
}

// ---------------------------------------------------------- 顶部统计
const stats = computed(() => {
  const list = runs.value
  if (!list.length) return null
  const okCount = list.filter((r) => r.ok).length
  const failCount = list.length - okCount
  const durations = list
    .map((r) => r.duration_ms)
    .filter((d): d is number => typeof d === 'number')
  const avgMs = durations.length
    ? Math.round(durations.reduce((a, b) => a + b, 0) / durations.length)
    : 0
  const maxMs = durations.length ? Math.max(...durations) : 0
  // 最近一次的结果摘要
  const latest = list[0]
  const latestSteps = getSteps(latest)
  const summary = latestSteps.length
    ? latestSteps[latestSteps.length - 1].detail || latestSteps[latestSteps.length - 1].name
    : latest?.error
      ? '失败'
      : '—'
  return {
    total: list.length,
    okCount,
    failCount,
    okPct: Math.round((okCount / list.length) * 100),
    avgMs,
    maxMs,
    summary,
    stepCount: latestSteps.length,
  }
})

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60000)}m${Math.round((ms % 60000) / 1000)}s`
}

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
    <div v-if="stats" class="history-stats">
      <span class="stat-pill" :class="stats.failCount > 0 ? 'stat-pill--warn' : 'stat-pill--ok'">
        {{ stats.okPct }}% ({{ stats.okCount }}/{{ stats.total }})
      </span>
      <span class="stat-pill">
        平均 {{ formatDuration(stats.avgMs) }}
      </span>
      <span class="stat-pill">
        最长 {{ formatDuration(stats.maxMs) }}
      </span>
      <span class="stat-pill stat-pill--muted">
        最近 {{ stats.summary }}
      </span>
    </div>

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
              <span v-if="getSteps(record).length" class="step-dots">
                <span
                  v-for="(s, i) in getSteps(record)"
                  :key="i"
                  class="step-dot"
                  :class="stepClass(s.status)"
                  :title="`${s.name} — ${s.detail ?? ''}`"
                />
              </span>
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
            <div class="steps-list">
              <div
                v-for="(s, i) in getSteps(selectedRun)"
                :key="i"
                class="step-card"
                :class="stepClass(s.status)"
              >
                <span class="step-icon">{{ statusIcon(s.status) }}</span>
                <span class="step-name">{{ s.name }}</span>
                <span v-if="s.count != null" class="step-count mono">{{ s.count }}</span>
                <span v-if="s.detail" class="step-detail">{{ s.detail }}</span>
              </div>
            </div>
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

.history-stats {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
}
.stat-pill {
  display: inline-flex;
  align-items: center;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: var(--text-xs);
  font-family: var(--font-mono, monospace);
  border: 1px solid var(--border-hairline);
  background: var(--bg-inset);
  color: var(--text-secondary);
}
.stat-pill--ok {
  border-color: var(--success-border, rgba(82, 196, 26, 0.5));
  color: var(--success-text, #52c41a);
}
.stat-pill--warn {
  border-color: var(--error-border, rgba(245, 34, 45, 0.5));
  color: var(--error-text, #f5222d);
}
.stat-pill--muted {
  color: var(--text-tertiary);
}

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

/* 步骤卡片 */
.steps-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  margin-top: var(--sp-2);
}
.step-card {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  padding: var(--sp-2);
  border-left: 3px solid var(--text-tertiary);
  border-radius: 0 var(--r-sm) var(--r-sm) 0;
  background: var(--bg-surface);
  font-size: var(--text-xs);
}
.step--ok { border-left-color: var(--success-text, #52c41a); }
.step--warn { border-left-color: var(--warning-text, #d97706); }
.step--skip { border-left-color: var(--text-tertiary); opacity: 0.7; }
.step--fail { border-left-color: var(--error-text, #f5222d); }

.step-icon {
  font-weight: 700;
  width: 14px;
  flex: 0 0 14px;
  text-align: center;
}
.step--ok .step-icon { color: var(--success-text, #52c41a); }
.step--warn .step-icon { color: var(--warning-text, #d97706); }
.step--skip .step-icon { color: var(--text-tertiary); }
.step--fail .step-icon { color: var(--error-text, #f5222d); }

.step-name {
  font-weight: 500;
  color: var(--text);
  flex: 0 0 auto;
}
.step-count {
  color: var(--text-secondary);
  font-weight: 600;
}
.step-detail {
  color: var(--text-tertiary);
  margin-left: auto;
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 50%;
}

/* 列表里的步骤小圆点 */
.step-dots {
  display: inline-flex;
  gap: 3px;
  align-items: center;
}
.step-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  display: inline-block;
  background: var(--text-tertiary);
}
.step-dot.step--ok { background: var(--success-text, #52c41a); }
.step-dot.step--warn { background: var(--warning-text, #d97706); }
.step-dot.step--skip { background: var(--border-hairline); }
.step-dot.step--fail { background: var(--error-text, #f5222d); }

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
