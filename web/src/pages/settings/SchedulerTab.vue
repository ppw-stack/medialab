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

interface JobRunItem {
  id: number;
  job_name: string;
  trigger: string;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  ok: boolean;
  error: string | null;
  result: Record<string, unknown> | null;
}

interface JobRunPage {
  total: number;
  items: JobRunItem[];
}

interface RuntimeParamSpec {
  key: string;
  label: string;
  min: number;
  max: number;
  default: number;
}

interface RuntimeParams {
  params: Record<string, number>;
  specs: RuntimeParamSpec[];
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
const runtimeParams = ref<RuntimeParams | null>(null)
const runtimeDraft = ref<Record<string, number>>({})
const savingRuntime = ref(false)
let timer: ReturnType<typeof setInterval> | null = null

const runsByJob = ref<Record<string, JobRunItem[]>>({})
const historyModalOpen = ref(false)
const historyJobName = ref<string>('')
const historyAll = ref<JobRunItem[]>([])

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

async function openHistoryModal(name: string) {
  historyJobName.value = name
  try {
    const page = await api.get<JobRunPage>(`/scheduler/runs?job_name=${name}&limit=100`)
    historyAll.value = page.items
  } catch {
    historyAll.value = runsByJob.value[name] ?? []
  }
  historyModalOpen.value = true
}

function formatRunTime(s: string): string {
  const d = new Date(s)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function formatRunFull(s: string): string {
  const d = new Date(s)
  const Y = d.getFullYear()
  const M = String(d.getMonth() + 1).padStart(2, '0')
  const D = String(d.getDate()).padStart(2, '0')
  const h = String(d.getHours()).padStart(2, '0')
  const m = String(d.getMinutes()).padStart(2, '0')
  const s2 = String(d.getSeconds()).padStart(2, '0')
  return `${Y}-${M}-${D} ${h}:${m}:${s2}`
}

async function load() {
  try {
    data.value = await api.get<SchedulerInfo>('/scheduler')
  } catch { /* ignore */ }
  try {
    runtimeParams.value = await api.get<RuntimeParams>('/scheduler/runtime-params')
    // 仅在未编辑时同步服务端最新值（避免覆盖用户正在输入的内容）
    if (!savingRuntime.value) {
      runtimeDraft.value = { ...runtimeParams.value.params }
    }
  } catch { /* ignore */ }
}

onMounted(() => {
  load()
  loadRuns()
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

function isRuntimeDirty(): boolean {
  if (!runtimeParams.value) return false
  for (const [k, v] of Object.entries(runtimeDraft.value)) {
    if (runtimeParams.value.params[k] !== v) return true
  }
  return false
}

async function saveRuntimeParams() {
  if (!runtimeParams.value) return
  if (!isRuntimeDirty()) return
  savingRuntime.value = true
  try {
    const updates: Record<string, number> = {}
    for (const spec of runtimeParams.value.specs) {
      const draft = runtimeDraft.value[spec.key]
      if (draft !== runtimeParams.value.params[spec.key]) {
        updates[spec.key] = draft
      }
    }
    const r = await api.put<RuntimeParams>('/scheduler/runtime-params', updates)
    runtimeParams.value = r
    runtimeDraft.value = { ...r.params }
    message.success('运行参数已保存，下次作业生效')
  } catch (e) {
    message.error(e instanceof Error ? e.message : '保存失败')
  } finally {
    savingRuntime.value = false
  }
}

function resetRuntimeDraft() {
  if (runtimeParams.value) {
    runtimeDraft.value = { ...runtimeParams.value.params }
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

function jobLabel(name: string) {
  return JOB_LABELS[name]
}

const historyColumns = [
  { title: '开始时间', key: 'started_at', dataIndex: 'started_at', width: 170 },
  { title: '触发', key: 'trigger', dataIndex: 'trigger', width: 80 },
  { title: '时长', key: 'duration_ms', dataIndex: 'duration_ms', width: 100 },
  { title: '状态', key: 'ok', dataIndex: 'ok', width: 80 },
  { title: '错误', key: 'error', dataIndex: 'error', ellipsis: true },
  { title: '结果', key: 'result', width: 80 },
]
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

        <a-modal
          v-model:open="historyModalOpen"
          :title="`${historyJobName} 执行历史`"
          :footer="null"
          width="760px"
        >
          <a-table
            :data-source="historyAll"
            :pagination="{ pageSize: 10, showSizeChanger: false }"
            row-key="id"
            size="small"
            :columns="historyColumns"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'started_at'">
                <span class="mono dim">{{ formatRunFull(record.started_at) }}</span>
              </template>
              <template v-else-if="column.key === 'ok'">
                <a-tag :color="record.ok ? 'green' : 'red'">
                  {{ record.ok ? 'OK' : 'FAIL' }}
                </a-tag>
              </template>
              <template v-else-if="column.key === 'duration_ms'">
                <span class="mono dim">{{ record.duration_ms ?? '—' }} ms</span>
              </template>
              <template v-else-if="column.key === 'error'">
                <span v-if="record.error" class="error-text">{{ record.error }}</span>
                <span v-else class="dim">—</span>
              </template>
              <template v-else-if="column.key === 'result'">
                <a-popover trigger="click" placement="left">
                  <template #content>
                    <pre class="result-json">{{ JSON.stringify(record.result, null, 2) }}</pre>
                  </template>
                  <a-button
                    v-if="record.result"
                    type="link"
                    size="small"
                    style="padding: 0"
                  >
                    查看
                  </a-button>
                  <span v-else class="dim">—</span>
                </a-popover>
              </template>
            </template>
          </a-table>
        </a-modal>

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
            <a-button type="link" size="small" @click="openHistoryModal(job.name)">
              查看全部 →
            </a-button>
          </div>
         </div>
       </div>

       <div v-if="runtimeParams" class="runtime-params">
         <div class="runtime-params__head">
           <span class="label-caps">作业运行参数</span>
           <span class="dim mono" style="margin-left: var(--sp-2)">
             下次作业生效 · DB 持久化
           </span>
         </div>
         <div class="runtime-params__grid">
           <div
             v-for="spec in runtimeParams.specs"
             :key="spec.key"
             class="runtime-params__row"
           >
             <label class="runtime-params__label">
               {{ spec.label }}
               <span class="dim mono runtime-params__hint">
                 范围 {{ spec.min }}–{{ spec.max }}
               </span>
             </label>
             <a-input-number
               v-model:value="runtimeDraft[spec.key]"
               :min="spec.min"
               :max="spec.max"
               :step="1"
               size="small"
               style="width: 140px"
             />
             <span class="dim mono runtime-params__default">
               默认 {{ spec.default }}
             </span>
           </div>
         </div>
         <div class="runtime-params__actions">
           <a-button
             size="small"
             :disabled="!isRuntimeDirty() || savingRuntime"
             @click="resetRuntimeDraft"
           >
             还原
           </a-button>
           <a-button
             size="small"
             type="primary"
             :loading="savingRuntime"
             :disabled="!isRuntimeDirty()"
             @click="saveRuntimeParams"
           >
             保存
           </a-button>
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

.runtime-params {
  margin-top: var(--sp-4);
  border: 1px solid var(--border-hairline);
  border-radius: var(--r-md);
  background: var(--bg-surface);
  padding: var(--sp-3) var(--sp-4);
}
.runtime-params__head {
  margin-bottom: var(--sp-3);
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
}
.runtime-params__grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: var(--sp-3);
  padding: var(--sp-2) 0;
}
.runtime-params__row {
  display: grid;
  grid-template-columns: 1fr auto auto;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-2) 0;
  border-bottom: 1px dashed var(--border-hairline);
}
.runtime-params__row:last-child {
  border-bottom: 0;
}
.runtime-params__label {
  display: flex;
  flex-direction: column;
  font-size: var(--text-sm);
  color: var(--text-primary);
  font-weight: 500;
  gap: 2px;
}
.runtime-params__hint {
  font-size: var(--text-xs);
  font-weight: 400;
}
.runtime-params__default {
  font-size: var(--text-xs);
  white-space: nowrap;
}
.runtime-params__actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-2);
  margin-top: var(--sp-2);
  padding-top: var(--sp-2);
  border-top: 1px solid var(--border-hairline);
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

.error-text {
  color: var(--error-text, #f5222d);
  font-family: var(--font-mono, monospace);
  font-size: var(--text-xs);
}

.result-json {
  max-width: 480px;
  max-height: 360px;
  overflow: auto;
  font-size: var(--text-xs);
  margin: 0;
}
</style>
