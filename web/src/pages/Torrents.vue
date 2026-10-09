<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { api } from '../api'

interface Torrent {
  id: number; info_hash: string; name: string; site: string; category: string | null;
  status: string; source: string; size: number; downloaded: number; uploaded: number;
  ratio: number; state: string | null; is_free: boolean;
  free_type: string | null; free_end_at: string | null; added_at: string;
  protect_until: string | null;
}

interface AuditEntry {
  id: number; ts: string; action: string; trigger_source: string;
  hash_count: number; freed_space: number; result: string; reason: string | null;
}

const STATUS_COLOR: Record<string, string> = {
  active: 'green', paused: 'orange', completed: 'blue', aborted: 'red', deleted: 'gray',
  free_completed: 'green', free_completed_late: 'orange', free_missed: 'red',
}

const SOURCE_LABEL: Record<string, string> = {
  auto_seed: '辅种',
  free_seed: '免费种',
  manual: '手动',
}

function formatSize(b: number): string {
  if (b < 1024 ** 2) return `${(b / 1024).toFixed(0)} KB`
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(0)} MB`
  if (b < 1024 ** 4) return `${(b / 1024 ** 3).toFixed(1)} GB`
  return `${(b / 1024 ** 4).toFixed(2)} TB`
}

function formatDate(d: string | null): string {
  return d ? new Date(d).toLocaleString() : '-'
}

const data = ref<{ total: number; items: Torrent[] }>({ total: 0, items: [] })
const filters = ref<{ site_id?: number; source?: string; status?: string; q?: string }>({})
const page = ref(1)
const pageSize = ref(20)
const detail = ref<Torrent | null>(null)
const audit = ref<AuditEntry[]>([])
const stats = ref<{ by_status: Record<string, number>; by_source: Record<string, number> } | null>(null)

async function load() {
  const qs = new URLSearchParams()
  if (filters.value.site_id) qs.set('site_id', String(filters.value.site_id))
  if (filters.value.source) qs.set('source', filters.value.source)
  if (filters.value.status) qs.set('status', filters.value.status)
  if (filters.value.q) qs.set('page_size', '200')
  try {
    data.value = await api.get<{ total: number; items: Torrent[] }>(`/torrents?${qs.toString()}&page_size=200`)
  } catch { /* ignore */ }
}

watch(filters, load, { deep: true })

onMounted(() => {
  load()
  api.get<{ torrents: { by_status: Record<string, number>; by_source: Record<string, number> } }>('/dashboard')
    .then((d) => stats.value = { by_status: d.torrents.by_status, by_source: d.torrents.by_source })
    .catch(() => {})
})

async function onShowDetail(t: Torrent) {
  detail.value = t
  try {
    const r = await api.get<{ items: AuditEntry[] }>('/torrents/audit?limit=50')
    audit.value = r.items.filter((a) => a.action === 'add_torrent' || a.action === 'trash' || a.action === 'remove')
  } catch {
    audit.value = []
  }
}

const filtered = computed(() => {
  if (filters.value.q) {
    const q = filters.value.q.toLowerCase()
    return data.value.items.filter((t) =>
      t.name.toLowerCase().includes(q) || t.info_hash.startsWith(filters.value.q!)
    )
  }
  return data.value.items
})

const pageItems = computed(() => {
  return filtered.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value)
})

const columns = [
  { title: '种子', dataIndex: 'name', key: 'name', ellipsis: true },
  { title: '站点', dataIndex: 'site', key: 'site' },
  { title: '来源', dataIndex: 'source', key: 'source' },
  { title: '状态', dataIndex: 'status', key: 'status' },
  { title: '免费', dataIndex: 'free_type', key: 'free' },
  { title: '大小', dataIndex: 'size', key: 'size' },
  { title: '进度', key: 'progress' },
  { title: '种比', dataIndex: 'ratio', key: 'ratio' },
  { title: '添加于', dataIndex: 'added_at', key: 'added' },
]
</script>

<template>
  <div>
    <a-typography-title :level="4">任务管理</a-typography-title>

    <a-row v-if="stats" :gutter="16" style="margin-bottom: 16px">
      <a-col :span="8">
        <a-card size="small">
          <a-statistic title="活跃任务" :value="stats.by_status.active || 0" />
        </a-card>
      </a-col>
      <a-col :span="8">
        <a-card size="small">
          <a-statistic title="暂停" :value="stats.by_status.paused || 0" />
        </a-card>
      </a-col>
      <a-col :span="8">
        <a-card size="small">
          <a-statistic title="在途免费种" :value="stats.by_source.free_seed || 0" />
        </a-card>
      </a-col>
    </a-row>

    <a-card :title="`任务列表（总计 ${data.total}）`">
      <template #extra>
        <a-space>
          <a-input-search
            placeholder="搜索名称 / hash"
            allow-clear
            style="width: 240px"
            @change="(e: Event) => filters.q = (e.target as HTMLInputElement).value"
            @search="(v: string) => filters.q = v"
          />
          <a-select
            placeholder="来源"
            allow-clear
            style="width: 120px"
            :value="filters.source"
            @change="(v: string) => filters.source = v"
            :options="[
              { value: 'auto_seed', label: '辅种' },
              { value: 'free_seed', label: '免费种' },
              { value: 'manual', label: '手动' },
            ]"
          />
          <a-select
            placeholder="状态"
            allow-clear
            style="width: 140px"
            :value="filters.status"
            @change="(v: string) => filters.status = v"
            :options="[
              { value: 'active', label: 'active' },
              { value: 'paused', label: 'paused' },
              { value: 'aborted', label: 'aborted' },
              { value: 'deleted', label: 'deleted' },
              { value: 'free_completed', label: 'free_completed' },
            ]"
          />
        </a-space>
      </template>
      <a-table
        :data-source="pageItems"
        :columns="columns"
        row-key="id"
        :pagination="{
          current: page,
          pageSize,
          total: filtered.length,
          showSizeChanger: true,
          onChange: (p: number, ps: number) => { page = p; pageSize = ps },
        }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'name'">
            <div>
              <a style="font-weight: 500" @click="onShowDetail(record)">{{ record.name }}</a>
              <a-typography-text type="secondary" code style="font-size: 11px">{{ record.info_hash.slice(0, 16) }}…</a-typography-text>
            </div>
          </template>
          <template v-else-if="column.key === 'source'">
            <a-tag :color="record.source === 'free_seed' ? 'purple' : record.source === 'auto_seed' ? 'blue' : 'default'">
              {{ SOURCE_LABEL[record.source] || record.source }}
            </a-tag>
          </template>
          <template v-else-if="column.key === 'status'">
            <a-tag :color="STATUS_COLOR[record.status] || 'default'">{{ record.status }}</a-tag>
          </template>
          <template v-else-if="column.key === 'free'">
            <span v-if="!record.is_free">-</span>
            <a-tag v-else :color="record.free_type === 'freeleech_permanent' ? 'green' : 'orange'">{{ record.free_type }}</a-tag>
          </template>
          <template v-else-if="column.key === 'size'">
            {{ formatSize(record.size) }}
          </template>
          <template v-else-if="column.key === 'progress'">
            <a-tooltip :title="`${formatSize(record.downloaded)} / ${formatSize(record.size)}`">
              {{ record.size ? Math.round(record.downloaded / record.size * 100) : 0 }}%
            </a-tooltip>
          </template>
          <template v-else-if="column.key === 'ratio'">
            {{ record.ratio.toFixed(2) }}
          </template>
          <template v-else-if="column.key === 'added'">
            {{ formatDate(record.added_at) }}
          </template>
        </template>
      </a-table>
    </a-card>

    <a-modal
      :open="detail !== null"
      @cancel="detail = null"
      :title="detail?.name || ''"
      :footer="null"
      :width="720"
    >
      <div v-if="detail">
        <a-row :gutter="[16, 8]">
          <a-col :span="8"><a-typography-text type="secondary">Hash</a-typography-text></a-col>
          <a-col :span="16"><code style="font-size: 12px">{{ detail.info_hash }}</code></a-col>
          <a-col :span="8"><a-typography-text type="secondary">站点</a-typography-text></a-col>
          <a-col :span="16">{{ detail.site }}</a-col>
          <a-col :span="8"><a-typography-text type="secondary">分类</a-typography-text></a-col>
          <a-col :span="16">{{ detail.category || '-' }}</a-col>
          <a-col :span="8"><a-typography-text type="secondary">大小 / 已下 / 种比</a-typography-text></a-col>
          <a-col :span="16">
            {{ formatSize(detail.size) }} / {{ formatSize(detail.downloaded) }} / {{ detail.ratio.toFixed(2) }}
          </a-col>
          <a-col :span="8"><a-typography-text type="secondary">来源 / 状态</a-typography-text></a-col>
          <a-col :span="16">
            <a-tag color="blue">{{ SOURCE_LABEL[detail.source] || detail.source }}</a-tag>
            <a-tag :color="STATUS_COLOR[detail.status] || 'default'">{{ detail.status }}</a-tag>
            <a-tag v-if="detail.is_free" color="purple">{{ detail.free_type }}</a-tag>
          </a-col>
          <template v-if="detail.free_end_at">
            <a-col :span="8"><a-typography-text type="secondary">免费窗口截止</a-typography-text></a-col>
            <a-col :span="16">{{ formatDate(detail.free_end_at) }}</a-col>
          </template>
          <template v-if="detail.protect_until">
            <a-col :span="8"><a-typography-text type="secondary">禁删保护截止</a-typography-text></a-col>
            <a-col :span="16">{{ formatDate(detail.protect_until) }}</a-col>
          </template>
        </a-row>

        <a-typography-title :level="5" style="margin-top: 24px">审计 (近 50 条)</a-typography-title>
        <a-table
          :data-source="audit"
          :pagination="false"
          size="small"
          row-key="id"
          :columns="[
            { title: '时间', dataIndex: 'ts', key: 'ts', width: 160 },
            { title: '动作', dataIndex: 'action', key: 'action' },
            { title: '触发源', dataIndex: 'trigger_source', key: 'ts2' },
            { title: '数量', dataIndex: 'hash_count', key: 'count', width: 60 },
            { title: '释放', dataIndex: 'freed_space', key: 'free' },
            { title: '结果', dataIndex: 'result', key: 'r' },
          ]"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'ts'">{{ formatDate(record.ts) }}</template>
            <template v-else-if="column.key === 'free'">{{ formatSize(record.freed_space) }}</template>
            <template v-else-if="column.key === 'r'">
              <a-tag :color="record.result === 'done' ? 'green' : 'red'">{{ record.result }}</a-tag>
            </template>
          </template>
        </a-table>
      </div>
    </a-modal>
  </div>
</template>
