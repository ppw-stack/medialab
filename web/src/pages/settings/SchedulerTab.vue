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
  fetch_all_sites:     { name: 'RSS 拉取',     desc: '遍历所有启用站点拉取 RSS，写入 torrent_index 表' },
  push_free_torrents:  { name: '免费种推送',   desc: '从 torrent_index 选免费种 → 默认下载器（每 30 分钟）' },
  monitor_free_window: { name: '免费窗口监测', desc: '巡检已推免费种，临近 free_end_at 自动暂停下载器（pre_pause=10 min）' },
}

const data = ref<SchedulerInfo | null>(null)
const runningJob = ref<string | null>(null)
const cronDraft = ref<string>('')
const savingCron = ref(false)
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

// 哪个 job 的 cron popover 打开（用于行高亮），null 表示都关
const editingCron = ref<string | null>(null)
const cronPopoverOpen = ref<Record<string, boolean>>({})

function openCronEditor(job: Job) {
  editingCron.value = job.name
  cronPopoverOpen.value = { ...cronPopoverOpen.value, [job.name]: true }
  cronDraft.value = job.cron
}

function closeCronEditor() {
  editingCron.value = null
  cronPopoverOpen.value = {}
  cronDraft.value = ''
}

async function saveCron(job: Job) {
  const v = cronDraft.value.trim()
  if (!v) {
    message.warning('cron 不能为空')
    return
  }
  if (v === job.cron) {
    closeCronEditor()
    return
  }
  savingCron.value = true
  try {
    await api.post(`/scheduler/${job.name}/cron`, { cron: v })
    message.success(`${job.name} cron → ${v}`)
    closeCronEditor()
    load()
  } catch (e) {
    message.error(e instanceof Error ? e.message : '修改失败')
  } finally {
    savingCron.value = false
  }
}

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
            <a-popover
              :open="!!cronPopoverOpen[job.name]"
              trigger="click"
              placement="bottomLeft"
              :destroy-tooltip-on-hide="true"
              @update:open="(o: boolean) => { cronPopoverOpen = { ...cronPopoverOpen, [job.name]: o }; if (!o) editingCron = null }"
            >
              <template #content>
                <div class="cron-editor">
                  <div class="cron-editor__label">cron 表达式（5 字段：分 时 日 月 星期）</div>
                  <a-input
                    v-model:value="cronDraft"
                    placeholder="*/10 * * * *"
                    size="small"
                    @keydown.enter="saveCron(job)"
                  />
                  <div class="cron-editor__actions">
                    <a-button size="small" @click="closeCronEditor">取消</a-button>
                    <a-button
                      size="small"
                      type="primary"
                      :loading="savingCron"
                      @click="saveCron(job)"
                    >
                      保存
                    </a-button>
                  </div>
                </div>
              </template>
              <button
                type="button"
                class="job-item__cron"
                :class="{ 'job-item__cron--editing': editingCron === job.name }"
                :title="`点击修改 cron：${job.cron}`"
                @click.stop="openCronEditor(job)"
              >
                <span class="job-item__cron-text mono">{{ job.cron }}</span>
                <span class="job-item__cron-edit" aria-hidden="true">编辑</span>
              </button>
            </a-popover>
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
  font-size: var(--text-xs);
  color: var(--text-secondary);
  letter-spacing: 0.05em;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  padding: 3px 8px;
  border-radius: var(--r-sm, 4px);
  border: 1px solid var(--border-hairline);
  background: var(--bg-inset);
  transition: background 0.12s ease, border-color 0.12s ease, color 0.12s ease;
  font-family: inherit;
}
.job-item__cron:hover {
  background: var(--bg-hover, rgba(0, 0, 0, 0.04));
  border-color: var(--brand-300, #f4b8a3);
  color: var(--text-primary);
}
.job-item__cron:focus-visible {
  outline: 2px solid var(--brand-500, #e55d2c);
  outline-offset: 1px;
}
.job-item__cron--editing {
  background: var(--brand-50, #fef3ee);
  border-color: var(--brand-500, #e55d2c);
  color: var(--brand-700, #c84a1f);
}
.job-item__cron-text {
  font-size: var(--text-xs);
}
.job-item__cron-edit {
  font-size: 10px;
  padding: 1px 6px;
  border: 1px solid currentColor;
  border-radius: 3px;
  opacity: 0.65;
  transition: opacity 0.12s ease;
}
.job-item__cron:hover .job-item__cron-edit {
  opacity: 1;
}

.cron-editor {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  width: 280px;
}
.cron-editor__label {
  font-size: var(--text-xs);
  color: var(--text-secondary);
}
.cron-editor__actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-2);
  margin-top: var(--sp-1);
}

.job-item__actions {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  flex: 0 0 auto;
}
</style>
