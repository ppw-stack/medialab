<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { api } from '../api'
import { message } from 'ant-design-vue'

interface Candidate {
  id: number; site_id: number; site: string; item_id: string | null;
  name: string; size: number; free_type: string; free_end_at: string | null;
  free_source: string | null; free_confidence: number | null;
  remaining_seconds: number | null; status: string; reject_reason: string | null;
  info_hash: string | null; torrent_id: number | null;
  seen_at: string; pushed_at: string | null
}
interface CandidateList { total: number; items: Candidate[] }

interface PushResult {
  candidate_id: number; success: boolean; info_hash: string | null; error: string | null
}
interface PushBatchResult {
  total: number; success: number; failed: number; results: PushResult[]
}

interface Site { id: number; name: string }

const STATUS_COLORS: Record<string, string> = {
  pending: 'blue', pushed: 'green', skipped: 'default', failed: 'red',
}

const STATUS_LABELS: Record<string, string> = {
  pending: '待推送', pushed: '已推送', skipped: '已跳过', failed: '失败',
}

const FREE_TYPE_LABELS: Record<string, string> = {
  freeleech_permanent: '永久免费',
  freeleech_timed: '限时免费',
  twoup_timed: '限时 2X',
  neutral_leech: '中性种',
  unknown: '未知',
}

const data = ref<CandidateList>({ total: 0, items: [] })
const sites = ref<Site[]>([])
const selectedIds = ref<number[]>([])
const siteFilter = ref<number | undefined>(undefined)
const statusFilter = ref<string | undefined>(undefined)
const searchKeyword = ref('')
const pulling = ref(false)
const pushing = ref(false)

async function loadSites() {
  try { sites.value = await api.get<Site[]>('/sites') } catch {}
}

async function loadData() {
  try {
    const qs = new URLSearchParams()
    if (siteFilter.value !== undefined) qs.set('site_id', String(siteFilter.value))
    if (statusFilter.value) qs.set('status', statusFilter.value)
    qs.set('page_size', '200')
    data.value = await api.get<CandidateList>(`/free-seed/candidates?${qs}`)
  } catch { /* ignore */ }
}

onMounted(() => {
  loadSites()
  loadData()
})

function formatSize(b: number): string {
  if (b < 1024 ** 2) return `${(b / 1024).toFixed(0)} KB`
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(0)} MB`
  return `${(b / 1024 ** 3).toFixed(1)} GB`
}

function formatRemaining(seconds: number | null): string {
  if (seconds === null) return '永久'
  if (seconds <= 0) return '已过期'
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  if (d > 0) return `${d}天 ${h}小时`
  if (h > 0) return `${h}小时 ${m}分`
  return `${m}分`
}

const filteredItems = computed(() => {
  let items = data.value.items
  if (searchKeyword.value) {
    const q = searchKeyword.value.toLowerCase()
    items = items.filter(
      (r) => r.name.toLowerCase().includes(q) || r.site.toLowerCase().includes(q)
    )
  }
  return items
})

async function pullSite(siteId: number) {
  pulling.value = true
  try {
    const r = await api.post<{
      site: string; status: string; pulled: number; accepted: number;
      skipped: number; failed: number; duration_ms: number; skip_reasons: Record<string, number>
    }>(`/free-seed/sites/${siteId}/pull`)
    if (r.status === 'ok' || r.status === 'empty') {
      message.success(`${r.site} 抓取完成：接受 ${r.accepted}，跳过 ${r.skipped}`)
    } else {
      message.warning(`${r.site} 抓取状态：${r.status}`)
    }
    await loadData()
  } catch (e) {
    message.error(e instanceof Error ? e.message : '抓取失败')
  } finally {
    pulling.value = false
  }
}

async function pullAll() {
  pulling.value = true
  try {
    const r = await api.post<{ total: number; site_results: any[] }>('/free-seed/pull')
    const summary = r.site_results
      .map((s) => `${s.site}: ${s.accepted}/${s.pulled}`)
      .join(', ')
    message.success(`已抓取 ${r.total} 个站点（${summary}）`)
    await loadData()
  } catch (e) {
    message.error(e instanceof Error ? e.message : '抓取失败')
  } finally {
    pulling.value = false
  }
}

async function pushOne(id: number) {
  pushing.value = true
  try {
    const r = await api.post<PushResult>(`/free-seed/candidates/${id}/push`)
    if (r.success) {
      message.success(`推送成功（${r.info_hash?.slice(0, 8)}）`)
    } else {
      message.error(r.error || '推送失败')
    }
    await loadData()
    selectedIds.value = selectedIds.value.filter((x) => x !== id)
  } catch (e) {
    message.error(e instanceof Error ? e.message : '推送失败')
  } finally {
    pushing.value = false
  }
}

async function pushBatch() {
  if (selectedIds.value.length === 0) {
    message.warning('请先勾选候选')
    return
  }
  pushing.value = true
  try {
    const r = await api.post<PushBatchResult>('/free-seed/candidates/push-batch', {
      ids: selectedIds.value,
    })
    message.success(`批量推送：成功 ${r.success}，失败 ${r.failed}`)
    selectedIds.value = []
    await loadData()
  } catch (e) {
    message.error(e instanceof Error ? e.message : '批量推送失败')
  } finally {
    pushing.value = false
  }
}

const columns = [
  { title: '种子名', key: 'name', width: 320 },
  { title: '站点', key: 'site', width: 90 },
  { title: '大小', key: 'size', width: 80 },
  { title: '免费类型', key: 'free_type', width: 110 },
  { title: '剩余/截止', key: 'remaining', width: 110 },
  { title: '状态', key: 'status', width: 100 },
  { title: '操作', key: 'action', width: 100 },
]
</script>

<template>
  <div>
    <a-typography-title :level="4">免费种候选池</a-typography-title>

    <a-card :title="`候选（${data.total}）`" style="margin-bottom: 16px">
      <template #extra>
        <a-space>
          <a-select
            v-model:value="siteFilter"
            placeholder="按站点筛选"
            allow-clear
            style="width: 160px"
            :options="sites.map((s) => ({ value: s.id, label: s.name }))"
            @change="loadData"
          />
          <a-select
            v-model:value="statusFilter"
            placeholder="按状态筛选"
            allow-clear
            style="width: 120px"
            :options="Object.entries(STATUS_LABELS).map(([v, l]) => ({ value: v, label: l }))"
            @change="loadData"
          />
          <a-input-search
            v-model:value="searchKeyword"
            placeholder="搜索名称/站点"
            style="width: 200px"
            allow-clear
          />
          <a-button :loading="pulling" @click="pullAll">全部抓取</a-button>
          <a-button
            type="primary"
            :loading="pushing"
            :disabled="selectedIds.length === 0"
            @click="pushBatch"
          >
            批量推送 ({{ selectedIds.length }})
          </a-button>
        </a-space>
      </template>
      <a-empty v-if="sites.length === 0" description="请先在「站点」页面配置站点" />
      <a-space v-else wrap style="margin-bottom: 16px">
        <a-button
          v-for="s in sites"
          :key="s.id"
          :loading="pulling"
          @click="pullSite(s.id)"
        >
          抓取 {{ s.name }}
        </a-button>
      </a-space>

      <a-table
        :data-source="filteredItems"
        :columns="columns"
        row-key="id"
        :pagination="{ pageSize: 50, showSizeChanger: true }"
        :row-selection="{
          selectedRowKeys: selectedIds,
          onChange: (keys: number[]) => { selectedIds = keys },
          getCheckboxProps: (record: Candidate) => ({
            disabled: record.status !== 'pending',
          }),
        }"
        size="middle"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'name'">
            <div>
              <a style="font-weight: 500">{{ record.name }}</a>
              <a-typography-text
                v-if="record.reject_reason"
                type="secondary"
                code
                style="font-size: 11px; margin-left: 8px"
              >
                {{ record.reject_reason }}
              </a-typography-text>
            </div>
          </template>
          <template v-else-if="column.key === 'site'">
            <a-tag>{{ record.site }}</a-tag>
          </template>
          <template v-else-if="column.key === 'size'">
            {{ formatSize(record.size) }}
          </template>
          <template v-else-if="column.key === 'free_type'">
            <a-tag :color="record.free_type === 'freeleech_permanent' ? 'green' : 'purple'">
              {{ FREE_TYPE_LABELS[record.free_type] || record.free_type }}
            </a-tag>
          </template>
          <template v-else-if="column.key === 'remaining'">
            {{ record.free_type === 'freeleech_permanent' ? '永久' : formatRemaining(record.remaining_seconds) }}
          </template>
          <template v-else-if="column.key === 'status'">
            <a-tag :color="STATUS_COLORS[record.status] || 'default'">
              {{ STATUS_LABELS[record.status] || record.status }}
            </a-tag>
          </template>
          <template v-else-if="column.key === 'action'">
            <a-button
              v-if="record.status === 'pending'"
              size="small"
              type="primary"
              :loading="pushing"
              @click="pushOne(record.id)"
            >
              推送
            </a-button>
            <span v-else-if="record.status === 'pushed'" style="color: rgba(255,255,255,0.45)">
              ✓
            </span>
          </template>
        </template>
      </a-table>
    </a-card>
  </div>
</template>