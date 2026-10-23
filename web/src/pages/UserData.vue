<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { api, errMsg } from '../api'
import type { UserData } from '../types'
import { formatBytes as fmtBytes } from '../utils/format'
import { message } from 'ant-design-vue'


const data = ref<UserData[]>([])
const loading = ref(false)
const refreshingSite = ref<number | null>(null)
const refreshingAll = ref(false)

async function load() {
  loading.value = true
  try {
    data.value = await api.get<UserData[]>('/user-data')
  } catch (e) {
    message.error('加载用户数据失败：' + errMsg(e))
  } finally {
    loading.value = false
  }
}

// refreshAll 保持 loading 态贯穿 10s 等待期 —— POST 一返回后台调度，
// 数据要等定时作业跑完才更新。期间按钮持续 loading，避免用户以为已结束却没动静。
let refreshTimer: ReturnType<typeof setTimeout> | null = null

async function refreshAll() {
  refreshingAll.value = true
  try {
    await api.post('/scheduler/refresh_user_data/run')
    message.success('已触发全局刷新，正在重新加载…')
    if (refreshTimer) clearTimeout(refreshTimer)
    refreshTimer = setTimeout(async () => {
      try {
        await load()
      } finally {
        refreshingAll.value = false
      }
    }, 10000)
  } catch (e) {
    message.error('触发失败：' + errMsg(e))
    refreshingAll.value = false
  }
}

async function refreshOne(siteId: number) {
  refreshingSite.value = siteId
  try {
    await api.post(`/sites/${siteId}/user-data/refresh`)
    message.success('已刷新')
    await load()
  } catch (e) {
    message.error('刷新失败：' + errMsg(e))
  } finally {
    refreshingSite.value = null
  }
}

function fmtAge(lastSeen: string): string {
  if (!lastSeen) return '-'
  const d = new Date(lastSeen.replace(' ', 'T') + '+08:00')
  const diff = (Date.now() - d.getTime()) / 1000
  if (diff < 60) return `${Math.floor(diff)} 秒前`
  if (diff < 3600) return `${Math.floor(diff / 60)} 分前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
}

const summary = computed(() => {
  const total_seed = data.value.reduce((s, d) => s + d.seeding_count, 0)
  const total_leech = data.value.reduce((s, d) => s + d.leeching_count, 0)
  const total_up = data.value.reduce((s, d) => s + d.upload, 0)
  const total_dn = data.value.reduce((s, d) => s + d.download, 0)
  const total_bonus = data.value.reduce((s, d) => s + (d.hourly_bonus ?? 0), 0)
  const capped = data.value.filter((d) => d.hourly_bonus_capped === true).length
  return { total_seed, total_leech, total_up, total_dn, total_bonus, capped }
})

const summaryTiles = computed(() => [
  { label: '做种中', value: String(summary.value.total_seed), hint: '跨站合计' },
  { label: '下载中', value: String(summary.value.total_leech), hint: '跨站合计' },
  { label: '总上传', value: fmtBytes(summary.value.total_up), hint: '累计' },
  { label: '总下载', value: fmtBytes(summary.value.total_dn), hint: '累计' },
  {
    label: '时魔合计/小时',
    value: summary.value.total_bonus.toFixed(1),
    hint: summary.value.capped > 0 ? `${summary.value.capped} 站已封顶` : '已封顶站越少越优先推',
  },
])

/** 时魔文案：`92.9 魔力值`；没有时魔的站显示「—」。 */
function fmtBonus(v: number | null, unit: string | null): string {
  if (v === null || v === undefined) return '—'
  return unit ? `${v.toFixed(1)} ${unit}` : v.toFixed(1)
}

/** 时魔提示：封顶状态 + 站点进度（进度是站点自己的口径）。 */
function bonusTooltip(d: UserData): string {
  if (d.hourly_bonus === null || d.hourly_bonus === undefined) return '该站没有时魔数据'
  const parts: string[] = []
  if (d.hourly_bonus_capped === true) parts.push('已达获取上限')
  else if (d.hourly_bonus_capped === false) parts.push('未达上限')
  if (d.hourly_bonus_progress !== null && d.hourly_bonus_progress !== undefined) {
    parts.push(`站点进度 ${d.hourly_bonus_progress.toFixed(1)}%`)
  }
  return parts.join('，') || '站点未提供上限信息'
}

onMounted(load)

onUnmounted(() => {
  if (refreshTimer) {
    clearTimeout(refreshTimer)
    refreshTimer = null
  }
})

const columns = computed(() => [
  { title: '站点', dataIndex: 'site_name', key: 'site_name', width: 120 },
  {
    title: '上传',
    dataIndex: 'upload',
    key: 'upload',
    width: 100,
    customRender: ({ text }: { text: number }) => fmtBytes(text),
  },
  {
    title: '下载',
    dataIndex: 'download',
    key: 'download',
    width: 100,
    customRender: ({ text }: { text: number }) => fmtBytes(text),
  },
  { title: '做种', dataIndex: 'seeding_count', key: 'seeding_count', width: 80 },
  { title: '下载中', dataIndex: 'leeching_count', key: 'leeching_count', width: 90 },
  {
    title: '时魔/小时',
    dataIndex: 'hourly_bonus',
    key: 'hourly_bonus',
    width: 150,
  },
  {
    title: '刷新于',
    dataIndex: 'last_seen_at',
    key: 'last_seen_at',
    width: 120,
    customRender: ({ text }: { text: string }) => fmtAge(text),
  },
  { title: '操作', key: 'actions', width: 100, fixed: 'right' as const },
])
</script>

<template>
  <div>
    <header class="page-header">
      <div>
        <div class="page-header__eyebrow">用户数据 · USER DATA</div>
        <h1 class="page-header__title">站点用户数据</h1>
        <div class="page-header__sub">9 站用量 / 做种 / 时魔（每小时站点积分合计口径）</div>
      </div>
      <div class="page-header__meta">
        <a-button @click="load" :loading="loading">刷新列表</a-button>
        <a-button type="primary" @click="refreshAll" :loading="refreshingAll">
          触发全局刷新
        </a-button>
      </div>
    </header>

    <hr class="divider-hairline" style="margin: 0" />

    <section class="section">
      <div class="label-caps" style="margin-bottom: var(--sp-3)">汇总 · SUMMARY</div>
      <div class="summary-grid">
        <div v-for="t in summaryTiles" :key="t.label" class="stat-tile">
          <div class="stat-tile__label">{{ t.label }}</div>
          <div class="stat-tile__value">{{ t.value }}</div>
          <div class="stat-tile__hint">{{ t.hint }}</div>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="label-caps" style="margin-bottom: var(--sp-3)">明细 · BY SITE</div>
      <a-alert
        v-if="!data.length && !loading"
        type="info"
        show-icon
        message="尚无用户数据"
        description="请先在「站点」页配置并启用站点，cron 作业（*/30）会自动抓取用户数据。"
        style="margin-bottom: var(--sp-3)"
      />
      <div class="panel" v-if="data.length || loading">
        <a-table
          :data-source="data"
          :columns="columns"
          :loading="loading"
          :pagination="false"
          :scroll="{ x: 860 }"
          row-key="site_id"
          size="small"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'hourly_bonus'">
              <span :title="bonusTooltip(record)">
                {{ fmtBonus(record.hourly_bonus, record.hourly_bonus_unit) }}
              </span>
              <a-tag v-if="record.hourly_bonus_capped === true" color="orange">已封顶</a-tag>
            </template>
            <template v-else-if="column.key === 'actions'">
              <a-button size="small" :loading="refreshingSite === record.site_id"
                @click="refreshOne(record.site_id)">
                刷新
              </a-button>
            </template>
          </template>
        </a-table>
      </div>
    </section>
  </div>
</template>

<style scoped>
/* 页面结构与设计 token 已在 global.css 的 .page-header / .section / .stat-tile / .panel
   统一定义，此处不再覆写。summary-grid 仅做响应式排列。 */
.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: var(--sp-3);
}
</style>
