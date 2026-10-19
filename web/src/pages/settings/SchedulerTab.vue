<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { api } from '../../api'
import { message } from 'ant-design-vue'
import SchedulerConfigModal from '../../components/SchedulerConfigModal.vue'
import SchedulerHistoryModal from '../../components/SchedulerHistoryModal.vue'

interface Job {
  name: string;
  cron: string;
  enabled: boolean;
  running: boolean;
}

interface SchedulerInfo {
  enabled: boolean;
  jobs: Job[];
  inflight: number;
  accepting_new_work: boolean;
}

interface RunResult {
  ok: boolean;
  started_at: string;
  finished_at: string;
  duration_ms: number;
  error: string;
  result?: Record<string, unknown> | null;
}

interface JobRunItem {
  id: number;
  job_name: string;
  trigger: string;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  ok: boolean;
  error: string | null;
  result: unknown;
}

interface JobRunPage {
  total: number;
  items: JobRunItem[];
}

const JOB_LABELS: Record<string, { name: string; desc: string }> = {
  fetch_all_sites:     { name: '站点拉取',     desc: '遍历所有启用站点拉取种子列表（API/HTML），写入 torrent_index 表' },
  push_free_torrents:  { name: '免费种推送',   desc: '从 torrent_index 选免费种 → 默认下载器（每 30 分钟）' },
  monitor_free_window: { name: '免费窗口监测', desc: '巡检已推免费种，临近 free_end_at 自动暂停下载器（pre_pause=10 min）' },
  refresh_user_data:   { name: '做种用量',     desc: '拉取 9 站上传/下载/做种数/下载中数 → 写入 site_user_data 表（FR-1.6 站点用户数据契约）' },
  scan_cross_seed:     { name: '辅种巡查',     desc: '扫描 is_cross_seed 下载器, 检测 tracker 未注册/数据丢失 → 自动暂停 (每 6 小时)' },
}

const data = ref<SchedulerInfo | null>(null)
const runningJob = ref<string | null>(null)

const runsByJob = ref<Record<string, JobRunItem[]>>({})

// 配置 / 历史 modal 状态
const configOpen = ref(false)
const configJobName = ref('')
const historyOpen = ref(false)
const historyJobName = ref('')

function jobLabel(name: string) {
  return JOB_LABELS[name]
}

async function loadRuns() {
  try {
    const page = await api.get<JobRunPage>('/scheduler/runs?limit=50')
    const grouped: Record<string, JobRunItem[]> = {}
    for (const r of page.items) {
      (grouped[r.job_name] ??= []).push(r)
    }
    runsByJob.value = grouped
  } catch { /* ignore */ }
}

async function load() {
  try {
    data.value = await api.get<SchedulerInfo>('/scheduler')
  } catch { /* ignore */ }
}

onMounted(() => {
  load()
  loadRuns()
  const timer = setInterval(() => {
    load()
    loadRuns()
  }, 5000)
  onUnmounted(() => clearInterval(timer))
})

async function toggle(name: string, enabled: boolean) {
  await api.post(`/scheduler/${name}/toggle?enabled=${enabled}`)
  message.success(`${name} → ${enabled ? '启用' : '暂停'}`)
  load()
}

async function runNow(name: string) {
  runningJob.value = name
  const hide = message.loading(`正在执行 ${name}...`, 0)
  try {
    const r = await api.post<RunResult>(`/scheduler/${name}/run`)
    const summary = formatResult(r)
    if (r.ok) {
      message.success(`${name} 完成：${summary}（${r.duration_ms} ms）`)
    } else {
      message.error(`${name} 失败：${summary || r.error || '未知错误'}`)
    }
  } catch (e) {
    message.error('触发失败：' + (e as Error).message)
  } finally {
    hide()
    runningJob.value = null
    load()
    loadRuns()
  }
}

function formatResult(r: RunResult): string {
  const v = r.result
  if (!v || typeof v !== 'object') return ''
  if ('selected' in v && 'pushed' in v) {
    const pushed = Number(v.pushed) || 0
    const failed = Number(v.failed) || 0
    const skippedDup = Number(v.skipped_already_pushed) || 0
    const skippedDl = Number(v.skipped_download_failed) || 0
    const skippedParse = Number(v.skipped_parse_failed) || 0
    const selected = Number(v.selected) || 0
    const parts: string[] = [`选中 ${selected}`, `推送 ${pushed}`]
    if (skippedDup > 0) parts.push(`重复 ${skippedDup}`)
    if (skippedDl > 0) parts.push(`下载失败 ${skippedDl}`)
    if (skippedParse > 0) parts.push(`解析失败 ${skippedParse}`)
    if (failed > 0) parts.push(`推送失败 ${failed}`)
    return parts.join('，')
  }
  if ('scanned' in v && 'pre_paused' in v) {
    const scanned = Number(v.scanned) || 0
    const prePaused = Number(v.pre_paused) || 0
    const matched = Number(v.matched) || 0
    const skippedUnmatched = Number(v.skipped_unmatched) || 0
    const skippedFar = Number(v.skipped_far_from_window) || 0
    const skippedCompleted = Number(v.skipped_already_completed) || 0
    const skippedPaused = Number(v.skipped_already_paused) || 0
    const failed = Number(v.failed) || 0
    const parts: string[] = [`扫描 ${scanned}`, `命中 ${matched}`, `暂停 ${prePaused}`]
    if (skippedUnmatched > 0) parts.push(`无关 ${skippedUnmatched}`)
    if (skippedFar > 0) parts.push(`窗口远 ${skippedFar}`)
    if (skippedCompleted > 0) parts.push(`已完成 ${skippedCompleted}`)
    if (skippedPaused > 0) parts.push(`已暂停 ${skippedPaused}`)
    if (failed > 0) parts.push(`失败 ${failed}`)
    return parts.join('，')
  }
  return ''
}

function openConfig(name: string) {
  configJobName.value = name
  configOpen.value = true
}

function openHistory(name: string) {
  historyJobName.value = name
  historyOpen.value = true
}

function formatRunTime(s: string): string {
  return s.length >= 16 ? s.substring(11, 16) : s
}

function formatRunFull(s: string): string {
  return s
}
</script>

<template>
  <div>
    <div v-if="!data" class="loading-row">
      <span class="mono dim">加载中...</span>
    </div>
    <div v-else>
      <div class="scheduler-head">
        <div>
          <span class="label-caps">调度器状态</span>
          <div class="scheduler-head__state">
            <span
              class="live-dot"
              :class="{ 'live-dot--idle': !data.accepting_new_work }"
            />
            <span class="mono">{{ data.accepting_new_work ? 'WORKING' : 'STOPPED' }}</span>
            <span class="dim mono" style="margin-left: var(--sp-2)">inflight {{ data.inflight }}</span>
          </div>
        </div>
      </div>

      <div class="job-list">
        <div
          v-for="job in data.jobs"
          :key="job.name"
          class="job-item"
        >
          <div class="job-item__main">
            <div class="job-item__title-row">
              <span
                class="live-dot"
                :class="{ 'live-dot--idle': !job.running }"
              />
              <span class="job-item__name">{{ jobLabel(job.name)?.name || job.name }}</span>
              <span class="job-item__id mono">{{ job.name }}</span>
            </div>
            <div class="job-item__desc">{{ jobLabel(job.name)?.desc || '—' }}</div>
            <div class="job-item__cron">
              <span class="job-item__cron-label dim mono">cron</span>
              <span class="job-item__cron-text mono">{{ job.cron }}</span>
            </div>
          </div>
          <div class="job-item__actions">
            <a-switch
              :checked="job.enabled"
              @change="(e: boolean) => toggle(job.name, e)"
            />
            <a-button
              size="small"
              type="default"
              :loading="runningJob === job.name"
              :disabled="!data.accepting_new_work"
              @click="runNow(job.name)"
            >
              立即执行 →
            </a-button>
            <a-button
              size="small"
              type="default"
              @click="openConfig(job.name)"
            >
              配置
            </a-button>
            <a-button
              size="small"
              type="default"
              @click="openHistory(job.name)"
            >
              历史
            </a-button>
          </div>
          <div v-if="runsByJob[job.name]?.length" class="job-item__runs">
            <span class="dim mono" style="font-size: var(--text-xs)">最近运行：</span>
            <a-tooltip
              v-for="r in runsByJob[job.name].slice(0, 6)"
              :key="r.id"
              :title="`${formatRunFull(r.started_at)} · ${r.duration_ms ?? 0}ms · ${r.ok ? 'OK' : 'FAIL'}${r.error ? ' · ' + r.error : ''}`"
            >
              <span class="run-chip" :class="r.ok ? 'run-chip--ok' : 'run-chip--fail'">
                <span class="run-chip__time mono">{{ formatRunTime(r.started_at) }}</span>
                <span v-if="!r.ok" class="run-chip__x">✕</span>
              </span>
            </a-tooltip>
          </div>
        </div>
      </div>

      <a-alert
        v-if="!data.enabled"
        type="warning"
        show-icon
        style="margin-top: var(--sp-4)"
        message="调度器已禁用"
        description="SCHEDULER_ENABLED=false。所有周期性作业都不会执行。"
      />
      <a-alert
        v-else-if="!data.accepting_new_work"
        type="warning"
        show-icon
        style="margin-top: var(--sp-4)"
        message="调度器停机中"
        description="正在等待在途作业收敛，立即执行暂不可用。"
      />

      <SchedulerConfigModal
        v-model:open="configOpen"
        :job-name="configJobName"
        :job-label="jobLabel(configJobName)?.name || configJobName"
        @saved="() => { load(); loadRuns() }"
      />
      <SchedulerHistoryModal
        v-model:open="historyOpen"
        :job-name="historyJobName"
        :job-label="jobLabel(historyJobName)?.name || historyJobName"
      />
    </div>
  </div>
</template>

<style scoped>
.loading-row {
  padding: var(--sp-4) 0;
}

.scheduler-head {
  margin-bottom: var(--sp-4);
}

.scheduler-head__state {
  margin-top: var(--sp-2);
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--text-md);
}

.dim {
  color: var(--text-tertiary);
}

.job-list {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border-hairline);
  border-radius: var(--r-md);
  background: var(--bg-surface);
}

.job-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--sp-3) var(--sp-4);
  gap: var(--sp-4);
  border-bottom: 1px solid var(--border-hairline);
}

.job-item:last-child {
  border-bottom: 0;
}

.job-item:hover {
  background: var(--bg-inset);
}

.job-item__main {
  flex: 1;
  min-width: 0;
}

.job-item__title-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-1);
}

.job-item__name {
  font-weight: 500;
  font-size: var(--text-md);
}

.job-item__id {
  font-size: var(--text-xs);
  color: var(--text-tertiary);
  margin-left: var(--sp-1);
}

.job-item__desc {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  margin-bottom: var(--sp-1);
  line-height: 1.5;
}

.job-item__cron {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--text-xs);
}

.job-item__cron-label {
  letter-spacing: 0.05em;
}

.job-item__cron-text {
  color: var(--text-secondary);
}

.job-item__actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex: 0 0 auto;
}

.job-item__runs {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  flex-wrap: wrap;
  padding-top: var(--sp-2);
  border-top: 1px dashed var(--border-hairline);
}

.run-chip {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 1px 6px;
  border-radius: var(--r-sm);
  font-size: var(--text-xs);
  border: 1px solid var(--border-hairline);
  background: var(--bg-surface);
  cursor: default;
}

.run-chip--ok {
  border-color: var(--success-border, rgba(82, 196, 26, 0.5));
  color: var(--success-text, #52c41a);
}

.run-chip--fail {
  border-color: var(--error-border, rgba(245, 34, 45, 0.5));
  color: var(--error-text, #f5222d);
  background: var(--error-bg-subtle, rgba(245, 34, 45, 0.04));
}

.run-chip__x {
  font-weight: 600;
}
</style>
