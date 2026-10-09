<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { api } from '../api'
import { message } from 'ant-design-vue'
import dayjs, { type Dayjs } from 'dayjs'

interface AuditEntry {
  id: number; ts: string; action: string; trigger_source: string;
  hash_count: number; freed_space: number; result: string; reason: string | null;
}

interface CleanupLog {
  id: number; ts: string; action: string; trigger_source: string; rule_id: number | null;
  rule_name: string | null; affected_count: number; freed_space: number; result: string;
  error: string | null;
}

type Source = 'action' | 'cleanup'

const TRIGGER_COLORS: Record<string, string> = {
  schedule: 'blue', manual: 'purple', alert_linked: 'orange',
  rule: 'cyan', window_abort: 'red', space_pressure: 'magenta', report: 'default',
}

function formatSize(b: number): string {
  if (b < 1024 ** 2) return `${(b / 1024).toFixed(0)} KB`
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(0)} MB`
  return `${(b / 1024 ** 3).toFixed(1)} GB`
}

function formatDate(d: string): string {
  return new Date(d).toLocaleString()
}

const source = ref<Source>('action')
const trigger = ref<string | undefined>(undefined)
const action = ref<string | undefined>(undefined)
const q = ref('')
const range = ref<[Dayjs, Dayjs] | null>(null)
const data = ref<(AuditEntry | CleanupLog)[]>([])
const loading = ref(false)

const columns = computed(() =>
  source.value === 'cleanup'
    ? [
        { title: '时间', dataIndex: 'ts', key: 'ts', width: 160 },
        { title: '动作', dataIndex: 'action', key: 'action' },
        { title: '规则', dataIndex: 'rule_name', key: 'rule' },
        { title: '触发源', dataIndex: 'trigger_source', key: 'ts2' },
        { title: '任务数', dataIndex: 'affected_count', key: 'count' },
        { title: '释放', dataIndex: 'freed_space', key: 'free' },
        { title: '结果', dataIndex: 'result', key: 'r' },
        { title: '错误', dataIndex: 'error', key: 'err' },
      ]
    : [
        { title: '时间', dataIndex: 'ts', key: 'ts', width: 160 },
        { title: '动作', dataIndex: 'action', key: 'action' },
        { title: '触发源', dataIndex: 'trigger_source', key: 'ts2' },
        { title: '数量', dataIndex: 'hash_count', key: 'count' },
        { title: '释放', dataIndex: 'freed_space', key: 'free' },
        { title: '结果', dataIndex: 'result', key: 'r' },
        { title: '原因', dataIndex: 'reason', key: 'reason' },
      ]
)

async function load() {
  loading.value = true
  try {
    let rows: (AuditEntry | CleanupLog)[]
    if (source.value === 'cleanup') {
      rows = await api.get<CleanupLog[]>('/cleanup/logs?limit=500')
    } else {
      const qs = new URLSearchParams()
      if (trigger.value) qs.set('trigger_source', trigger.value)
      if (action.value) qs.set('action', action.value)
      qs.set('limit', '500')
      const r = await api.get<{ items: AuditEntry[] }>(`/torrents/audit?${qs.toString()}`)
      rows = r.items
    }
    let filtered = rows
    if (range.value) {
      filtered = filtered.filter((e) => {
        const t = new Date(e.ts).getTime()
        return t >= range.value![0].valueOf() && t <= range.value![1].valueOf() + 86_400_000
      })
    }
    if (q.value) {
      const ql = q.value.toLowerCase()
      filtered = filtered.filter((e) =>
        (e as { reason?: string }).reason?.toLowerCase().includes(ql) ||
        (e as { action?: string }).action?.toLowerCase().includes(ql) ||
        e.trigger_source.toLowerCase().includes(ql)
      )
    }
    data.value = filtered
  } finally {
    loading.value = false
  }
}

watch([source, trigger, action, range, q], load, { deep: true })
onMounted(load)

function exportCsv() {
  if (!data.value.length) {
    message.warning('无数据')
    return
  }
  const headers = ['ts', 'action', 'trigger_source', 'result', 'count', 'freed_space', 'rule_name', 'reason']
  const rows = data.value.map((e) => [
    e.ts,
    (e as { action: string }).action,
    e.trigger_source,
    e.result,
    (e as { hash_count?: number }).hash_count ?? (e as { affected_count?: number }).affected_count,
    e.freed_space,
    (e as { rule_name?: string }).rule_name || '',
    (e as { reason?: string }).reason || '',
  ])
  const csv = [headers, ...rows].map((r) => r.map((c) => `"${c}"`).join(',')).join('\n')
  const blob = new Blob([csv], { type: 'text/csv' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `audit_${Date.now()}.csv`
  a.click()
  URL.revokeObjectURL(url)
}

const triggerOptions = computed(() =>
  Object.entries(TRIGGER_COLORS).map(([v, c]) => ({ value: v, label: v, color: c }))
)

const dataTitle = computed(() => `${data.value.length} 条记录`)
</script>

<template>
  <div>
    <a-typography-title :level="4">审计日志</a-typography-title>
    <a-card :title="dataTitle">
      <template #extra>
        <a-space>
          <a-select
            :value="source"
            @change="(v: Source) => source = v"
            style="width: 130px"
            :options="[
              { value: 'action', label: '操作审计' },
              { value: 'cleanup', label: '清理审计' },
            ]"
          />
          <a-select
            allow-clear
            placeholder="触发源"
            style="width: 140px"
            :value="trigger"
            @change="(v: string) => trigger = v"
            :options="triggerOptions"
          />
          <a-select
            v-if="source === 'action'"
            allow-clear
            placeholder="动作"
            style="width: 140px"
            :value="action"
            @change="(v: string) => action = v"
            :options="[
              { value: 'add_torrent', label: 'add_torrent' },
              { value: 'trash', label: 'trash' },
              { value: 'remove', label: 'remove' },
            ]"
          />
          <a-range-picker
            :value="range"
            @change="(_: unknown, dateStrings: [string, string]) => {
              if (dateStrings[0] && dateStrings[1]) {
                range = [dayjs(dateStrings[0]), dayjs(dateStrings[1])] as [Dayjs, Dayjs]
              } else {
                range = null
              }
            }"
            allow-clear
          />
          <a-input-search
            allow-clear
            placeholder="搜索原因/动作"
            style="width: 180px"
            @change="(e: Event) => q = (e.target as HTMLInputElement).value"
          />
          <a-button @click="exportCsv">导出 CSV</a-button>
        </a-space>
      </template>
      <a-table
        :loading="loading"
        :data-source="data"
        :columns="columns"
        row-key="id"
        :pagination="{ pageSize: 30 }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'ts'">{{ formatDate(record.ts) }}</template>
          <template v-else-if="column.key === 'ts2'">
            <a-tag :color="TRIGGER_COLORS[record.trigger_source] || 'default'">{{ record.trigger_source }}</a-tag>
          </template>
          <template v-else-if="column.key === 'free'">{{ formatSize(record.freed_space) }}</template>
          <template v-else-if="column.key === 'rule'">{{ record.rule_name || '-' }}</template>
          <template v-else-if="column.key === 'r'">
            <a-tag
              :color="source === 'cleanup'
                ? (record.result === 'done' ? 'green' : 'red')
                : (record.result === 'ok' ? 'green' : record.result === 'error' ? 'red' : 'default')"
            >{{ record.result }}</a-tag>
          </template>
          <template v-else-if="column.key === 'err'">
            <a-tooltip v-if="record.error" :title="record.error"><a-tag color="red">有</a-tag></a-tooltip>
            <span v-else>-</span>
          </template>
        </template>
      </a-table>
    </a-card>
  </div>
</template>
