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

const columns = [
  { title: '作业', dataIndex: 'name', key: 'name' },
  { title: '说明', key: 'desc' },
  { title: 'cron', dataIndex: 'cron', key: 'cron', width: 160 },
  { title: '运行中', dataIndex: 'running', key: 'running' },
  { title: '启用', dataIndex: 'enabled', key: 'enabled' },
  { title: '操作', key: 'actions', width: 140 },
]
</script>

<template>
  <a-card v-if="!data">
    <a-typography-text>加载中...</a-typography-text>
  </a-card>
  <a-card
    v-else
    :title="`调度器（${data.accepting_new_work ? '工作中' : '停机中'}，在途 ${data.inflight}）`"
  >
    <a-table
      :data-source="data.jobs"
      row-key="name"
      :pagination="false"
      :columns="columns"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'name'">
          <div>
            <b>{{ jobLabel(record.name)?.name || record.name }}</b>
            <a-typography-text type="secondary" style="font-size: 12px">{{ record.name }}</a-typography-text>
          </div>
        </template>
        <template v-else-if="column.key === 'desc'">
          {{ jobLabel(record.name)?.desc || '-' }}
        </template>
        <template v-else-if="column.key === 'cron'">
          <code style="font-size: 12px">{{ record.cron }}</code>
        </template>
        <template v-else-if="column.key === 'running'">
          <a-tag v-if="record.running" color="processing">是</a-tag>
          <a-tag v-else>否</a-tag>
        </template>
        <template v-else-if="column.key === 'enabled'">
          <a-switch
            :checked="record.enabled"
            @change="(e: boolean) => toggle(record.name, e)"
          />
        </template>
        <template v-else-if="column.key === 'actions'">
          <a-button
            size="small"
            :loading="runningJob === record.name"
            :disabled="!data.accepting_new_work"
            @click="runNow(record.name)"
          >
            立即执行
          </a-button>
        </template>
      </template>
    </a-table>
    <a-typography-paragraph v-if="!data.enabled" type="warning" style="margin-top: 16px">
      调度器已禁用（SCHEDULER_ENABLED=false）。所有周期性作业都不会执行。
    </a-typography-paragraph>
    <a-typography-paragraph
      v-else-if="!data.accepting_new_work"
      type="warning"
      style="margin-top: 16px"
    >
      调度器停机中，正在等待在途作业收敛。立即执行不可用。
    </a-typography-paragraph>
  </a-card>
</template>
