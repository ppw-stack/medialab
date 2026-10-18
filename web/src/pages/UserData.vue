<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { api } from '../api'
import { message } from 'ant-design-vue'

interface UserData {
  site_id: number
  site_name: string
  adapter: string
  upload: number
  download: number
  seeding_count: number
  leeching_count: number
  last_seen_at: string
}

const data = ref<UserData[]>([])
const loading = ref(false)
const refreshingSite = ref<number | null>(null)
const refreshingAll = ref(false)

async function load() {
  loading.value = true
  try {
    data.value = await api.get<UserData[]>('/user-data')
  } catch (e) {
    message.error('加载用户数据失败：' + (e as Error).message)
  } finally {
    loading.value = false
  }
}

async function refreshOne(siteId: number) {
  refreshingSite.value = siteId
  try {
    await api.post(`/sites/${siteId}/user-data/refresh`)
    message.success('已刷新')
    await load()
  } catch (e) {
    message.error('刷新失败：' + (e as Error).message)
  } finally {
    refreshingSite.value = null
  }
}

async function refreshAll() {
  refreshingAll.value = true
  try {
    await api.post('/scheduler/refresh_user_data/run')
    message.success('已触发全局刷新（10s 后重新加载）')
    setTimeout(load, 10000)
  } catch (e) {
    message.error('触发失败：' + (e as Error).message)
  } finally {
    refreshingAll.value = false
  }
}

function fmtBytes(b: number): string {
  if (!b) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  const i = Math.floor(Math.log(b) / Math.log(1024))
  return (b / Math.pow(1024, i)).toFixed(2) + ' ' + units[i]
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
  return { total_seed, total_leech, total_up, total_dn }
})

onMounted(load)

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
  <div class="user-data-page">
    <div class="page-header">
      <h2>站点用户数据</h2>
      <div class="actions">
        <a-button @click="load" :loading="loading">刷新列表</a-button>
        <a-button type="primary" @click="refreshAll" :loading="refreshingAll">
          触发全局刷新
        </a-button>
      </div>
    </div>

    <div class="summary-cards">
      <a-card>
        <a-statistic title="做种中" :value="summary.total_seed" />
      </a-card>
      <a-card>
        <a-statistic title="下载中" :value="summary.total_leech" />
      </a-card>
      <a-card>
        <a-statistic title="总上传" :value="fmtBytes(summary.total_up)" />
      </a-card>
      <a-card>
        <a-statistic title="总下载" :value="fmtBytes(summary.total_dn)" />
      </a-card>
    </div>

    <a-table
      :data-source="data"
      :columns="columns"
      :loading="loading"
      :pagination="false"
      :scroll="{ x: 720 }"
      row-key="site_id"
      size="small"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'actions'">
          <a-button size="small" :loading="refreshingSite === record.site_id"
            @click="refreshOne(record.site_id)">
            刷新
          </a-button>
        </template>
      </template>
    </a-table>
  </div>
</template>

<style scoped>
.user-data-page { padding: 16px; }
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}
.page-header h2 { margin: 0; }
.actions { display: flex; gap: 8px; }
.summary-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
</style>
