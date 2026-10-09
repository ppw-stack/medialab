<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../api'
import { message } from 'ant-design-vue'

interface FreeSeedCard {
  id: number; name: string; site: string; size: number;
  free_type: string | null; free_end_at: string | null;
  free_source: string | null; free_confidence: number | null;
  remaining_seconds: number | null; urgency: string;
  next_inspect_seconds: number; progress: number; downloaded: number;
  dlspeed: number; feasibility: string; eta_hours: number | null;
  seconds_to_abort: number | null; abort_at: string | null;
  paused_reason: string | null; status: string;
}

interface FreeSeedList { total: number; items: FreeSeedCard[] }
interface OutcomeSummary {
  window_completed: number; late_completed: number; missed: number; aborted: number;
  window_completion_rate: number | null;
}

const urgencyColor: Record<string, string> = {
  normal: 'green', notice: 'blue', approaching: 'gold',
  urgent: 'volcano', critical: 'magenta', expired: 'red', permanent: 'green',
}

const freeTypeLabel: Record<string, string> = {
  freeleech_timed: '限时免费', freeleech_permanent: '永久免费',
  twoup_timed: '限时 2X', neutral_leech: '中性种', unknown: '未知',
}

function formatRemaining(seconds: number | null): string {
  if (seconds === null) return '永久'
  if (seconds <= 0) return '已过期'
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  if (d > 0) return `${d}天 ${h}小时`
  if (h > 0) return `${h}小时 ${m}分`
  const s = Math.floor(seconds % 60)
  if (m > 0) return `${m}分 ${s}秒`
  return `${s}秒`
}

function formatSize(b: number): string {
  if (b < 1024 ** 2) return `${(b / 1024).toFixed(0)} KB`
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(0)} MB`
  return `${(b / 1024 ** 3).toFixed(1)} GB`
}

const data = ref<FreeSeedList>({ total: 0, items: [] })
const summary = ref<OutcomeSummary | null>(null)
const urgencyFilter = ref<string | undefined>(undefined)
const typeFilter = ref<string | undefined>(undefined)
const q = ref('')

let timer: ReturnType<typeof setInterval> | null = null

async function loadData() {
  try {
    data.value = await api.get<FreeSeedList>('/free-seed?in_flight_only=true&page_size=200')
  } catch { /* ignore */ }
  try {
    summary.value = await api.get<OutcomeSummary>('/free-seed/summary')
  } catch { /* ignore */ }
}

onMounted(() => {
  loadData()
  timer = setInterval(loadData, 10000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})

async function abort(id: number) {
  try {
    await api.post(`/free-seed/${id}/abort`, { reason: 'manual' })
    message.success('已提交中止')
    loadData()
  } catch (e) {
    message.error(e instanceof Error ? e.message : '中止失败')
  }
}

const urgencyOrder: Record<string, number> = {
  expired: 0, critical: 1, urgent: 2, approaching: 3, notice: 4, normal: 5, permanent: 6,
}

const sortedItems = computed(() => {
  let items = data.value.items
  if (urgencyFilter.value) items = items.filter((r) => r.urgency === urgencyFilter.value)
  if (typeFilter.value) items = items.filter((r) => r.free_type === typeFilter.value)
  if (q.value) {
    const ql = q.value.toLowerCase()
    items = items.filter((r) => r.name.toLowerCase().includes(ql) || r.site.toLowerCase().includes(ql))
  }
  return [...items].sort((a, b) => (urgencyOrder[a.urgency] ?? 9) - (urgencyOrder[b.urgency] ?? 9))
})

async function manualPull() {
  await api.post('/free-seed/pull')
  loadData()
}

async function manualWatch() {
  await api.post('/free-seed/watch')
  message.success('看护已执行')
}

const columns = [
  { title: '种子名', dataIndex: 'name', key: 'name', ellipsis: true },
  { title: '类型', dataIndex: 'free_type', key: 'free_type' },
  { title: '窗口来源', dataIndex: 'free_source', key: 'src' },
  { title: '紧迫度', key: 'urgency' },
  { title: '进度', key: 'progress' },
  { title: '大小', dataIndex: 'size', key: 'size', width: 80 },
  { title: '距中止', key: 'abort' },
  { title: '状态', dataIndex: 'status', key: 'status' },
  { title: '操作', key: 'action' },
]
</script>

<template>
  <div>
    <a-typography-title :level="4">免费种管理</a-typography-title>

    <a-space v-if="summary" size="middle" wrap style="margin-bottom: 16px">
      <a-card size="small"><b>窗口内完成：</b>{{ summary.window_completed }}</a-card>
      <a-card size="small"><b>边界超期：</b>{{ summary.late_completed }}</a-card>
      <a-card size="small"><b>看护失效：</b>{{ summary.missed }}</a-card>
      <a-card size="small"><b>已中止：</b>{{ summary.aborted }}</a-card>
      <a-card size="small">
        <b>窗口内完成率：</b>
        {{ summary.window_completion_rate !== null ? `${(summary.window_completion_rate * 100).toFixed(1)}%` : '-' }}
      </a-card>
    </a-space>

    <a-card :title="`在途任务（${data.total}）`">
      <template #extra>
        <a-space>
          <a-select
            allow-clear
            placeholder="紧迫度"
            style="width: 110px"
            :value="urgencyFilter"
            @change="(v: string) => urgencyFilter = v"
            :options="[
              { value: 'critical', label: '🔴 临界' },
              { value: 'urgent', label: '🟠 紧急' },
              { value: 'approaching', label: '🟡 临近' },
              { value: 'notice', label: '🔵 注意' },
              { value: 'normal', label: '🟢 正常' },
              { value: 'permanent', label: '♾️ 永久' },
            ]"
          />
          <a-select
            allow-clear
            placeholder="免费类型"
            style="width: 130px"
            :value="typeFilter"
            @change="(v: string) => typeFilter = v"
            :options="Object.entries(freeTypeLabel).map(([v, l]) => ({ value: v, label: l }))"
          />
          <a-input-search
            allow-clear
            placeholder="搜索名称/站点"
            style="width: 200px"
            @change="(e: Event) => q = (e.target as HTMLInputElement).value"
          />
          <a-button @click="manualPull">手动抓取</a-button>
          <a-button @click="manualWatch">手动看护</a-button>
        </a-space>
      </template>
      <a-empty v-if="sortedItems.length === 0" description="暂无在途免费种" />
      <a-table
        v-else
        :data-source="sortedItems"
        :columns="columns"
        row-key="id"
        :pagination="false"
        size="middle"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'name'">
            <div>
              <span style="font-weight: 500">{{ record.name }}</span>
              <a-typography-text type="secondary" style="font-size: 11px">{{ record.site }}</a-typography-text>
            </div>
          </template>
          <template v-else-if="column.key === 'free_type'">
            <a-tag v-if="record.free_type" :color="record.free_type === 'freeleech_permanent' ? 'green' : 'purple'">
              {{ freeTypeLabel[record.free_type] || record.free_type }}
            </a-tag>
            <span v-else>-</span>
          </template>
          <template v-else-if="column.key === 'src'">
            <a-tooltip v-if="record.free_source" title="confidence 越低越需关注">
              <a-tag color="blue">{{ record.free_source }}</a-tag>
            </a-tooltip>
            <span v-else>-</span>
          </template>
          <template v-else-if="column.key === 'urgency'">
            <a-tag :color="urgencyColor[record.urgency] || 'default'">
              {{ record.urgency === 'permanent' ? '永久' : formatRemaining(record.remaining_seconds) }}
            </a-tag>
          </template>
          <template v-else-if="column.key === 'progress'">
            <div style="width: 140px">
              <a-progress
                :percent="Math.round(record.progress * 100)"
                size="small"
                :status="record.feasibility === 'ok' ? 'normal' : record.feasibility === 'critical' ? 'exception' : 'active'"
              />
              <a-typography-text type="secondary" style="font-size: 12px">
                {{ record.feasibility === 'ok' ? '✅' : record.feasibility === 'critical' ? '⚠️ 临界' : '❌' }}
                {{ record.eta_hours ? ` ${record.eta_hours}h` : '' }}
              </a-typography-text>
            </div>
          </template>
          <template v-else-if="column.key === 'size'">
            {{ formatSize(record.size) }}
          </template>
          <template v-else-if="column.key === 'abort'">
            {{ record.seconds_to_abort !== null ? formatRemaining(record.seconds_to_abort) : '-' }}
          </template>
          <template v-else-if="column.key === 'status'">
            <a-tag color="default">{{ record.status }}</a-tag>
          </template>
          <template v-else-if="column.key === 'action'">
            <a-button size="small" danger @click="abort(record.id)">中止</a-button>
          </template>
        </template>
      </a-table>
    </a-card>
  </div>
</template>
