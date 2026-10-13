<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { api } from '../../api'
import { message } from 'ant-design-vue'

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

const JOB_LABELS: Record<string, { name: string; desc: string }> = {
  fetch_all_sites:   { name: 'RSS 拉取',     desc: '遍历所有启用站点拉取 RSS，写入 torrent_index 表' },
  push_free_torrents: { name: '免费种推送',  desc: '从 torrent_index 选免费种 → 默认下载器（每 30 分钟）' },
}

const data = ref<SchedulerInfo | null>(null)
const runningJob = ref<string | null>(null)
let timer: ReturnType<typeof setInterval> | null = null

async function load() {
  try {
    data.value = await api.get<SchedulerInfo>('/scheduler')
  } catch { /* ignore */ }
}

onMounted(() => {
  load()
  timer = setInterval(load, 5000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
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
    message.error(e instanceof Error ? e.message : '触发失败')
  } finally {
    hide()
    runningJob.value = null
    load()
  }
}

function formatResult(r: RunResult): string {
  const v = r.result
  if (!v || typeof v !== 'object') return ''
  if ('selected' in v && 'pushed' in v) {
    const pushed = Number(v.pushed) || 0
    const failed = Number(v.failed) || 0
    const skippedDup = Number(v.skipped_already_pushed) || 0
    const skippedInvalid = Number(v.skipped_invalid_url) || 0
    const selected = Number(v.selected) || 0
    const parts: string[] = [`选中 ${selected}`, `推送 ${pushed}`]
    if (skippedDup > 0) parts.push(`重复 ${skippedDup}`)
    if (skippedInvalid > 0) parts.push(`无效URL ${skippedInvalid}`)
    if (failed > 0) parts.push(`失败 ${failed}`)
    return parts.join('，')
  }
  return ''
}

function jobLabel(name: string) {
  return JOB_LABELS[name]
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
            <div class="job-item__cron mono">{{ job.cron }}</div>
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
  background: var(--bg-elevated);
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
  font-size: var(--text-xs);
  color: var(--text-tertiary);
  letter-spacing: 0.05em;
}

.job-item__actions {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  flex: 0 0 auto;
}
</style>
