<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { api } from '../api'

interface DashboardData {
  sites: { total: number; enabled: number }
  torrents: { by_status: Record<string, number>; by_source: Record<string, number> }
  free_seed: { in_flight: number }
  downloaders?: { total: number; enabled: number }
}

const data = ref<DashboardData | null>(null)

const statusColor: Record<string, string> = {
  active: 'green',
  paused: 'orange',
  completed: 'blue',
  aborted: 'red',
  deleted: 'gray',
  free_completed: 'green',
  free_completed_late: 'orange',
  free_missed: 'red',
}

const statusRows = computed(() =>
  data.value ? Object.entries(data.value.torrents.by_status).map(([status, count]) => ({ status, count })) : []
)

const sourceRows = computed(() =>
  data.value ? Object.entries(data.value.torrents.by_source).map(([source, count]) => ({ source, count })) : []
)

const downloadersTotal = computed(() => data.value?.downloaders?.total ?? 0)
const downloadersEnabled = computed(() => data.value?.downloaders?.enabled ?? 0)
const statusTotal = computed(() => statusRows.value.reduce((s, r) => s + r.count, 0))
const sourceTotal = computed(() => sourceRows.value.reduce((s, r) => s + r.count, 0))

onMounted(() => {
  api.get<DashboardData>('/dashboard').then((d) => data.value = d).catch(() => {})
})
</script>

<template>
  <div v-if="!data">
    <a-typography-text>加载中...</a-typography-text>
  </div>
  <div v-else>
    <a-typography-title :level="4">总览仪表盘</a-typography-title>
    <a-row :gutter="16">
      <a-col :span="6">
        <a-card>
          <a-statistic title="站点（在线 / 总数）" :value="data.sites.enabled" :suffix="`/ ${data.sites.total}`" />
        </a-card>
      </a-col>
      <a-col :span="6">
        <a-card>
          <a-statistic title="下载器（启用 / 总数）" :value="downloadersEnabled" :suffix="`/ ${downloadersTotal}`" />
        </a-card>
      </a-col>
      <a-col :span="6">
        <a-card>
          <a-statistic title="活跃任务" :value="data.torrents.by_status.active || 0" />
        </a-card>
      </a-col>
      <a-col :span="6">
        <a-card>
          <a-statistic title="暂停" :value="data.torrents.by_status.paused || 0" />
        </a-card>
      </a-col>
      <a-col :span="6">
        <a-card>
          <a-statistic title="在途免费种" :value="data.free_seed.in_flight" />
        </a-card>
      </a-col>
      <a-col :span="6">
        <a-card>
          <a-statistic title="任务总数（按状态）" :value="statusTotal" />
        </a-card>
      </a-col>
      <a-col :span="6">
        <a-card>
          <a-statistic title="任务总数（按来源）" :value="sourceTotal" />
        </a-card>
      </a-col>
    </a-row>

    <a-row :gutter="16" style="margin-top: 16px">
      <a-col :span="12">
        <a-card title="任务状态分布">
          <a-table
            :data-source="statusRows"
            row-key="status"
            :pagination="false"
            :columns="[
              { title: '状态', dataIndex: 'status', key: 'status' },
              { title: '数量', dataIndex: 'count', key: 'count' },
            ]"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'status'">
                <a-tag :color="statusColor[record.status] || 'default'">{{ record.status }}</a-tag>
              </template>
            </template>
          </a-table>
        </a-card>
      </a-col>
      <a-col :span="12">
        <a-card title="任务来源分布">
          <a-table
            :data-source="sourceRows"
            row-key="source"
            :pagination="false"
            :columns="[
              { title: '来源', dataIndex: 'source', key: 'source' },
              { title: '数量', dataIndex: 'count', key: 'count' },
            ]"
          />
        </a-card>
      </a-col>
    </a-row>
  </div>
</template>
