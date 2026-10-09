<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import { api } from '../api'
import { message } from 'ant-design-vue'

interface Alert {
  id: number;
  ts: string;
  level: string;
  category: string;
  info_hash: string | null;
  message: string;
  dedupe_key: string;
  acknowledged: boolean;
  muted_until: string | null;
}

interface AlertList {
  total: number;
  page: number;
  page_size: number;
  items: Alert[];
}

const levelColor: Record<string, string> = {
  critical: 'red',
  warning: 'orange',
  info: 'blue',
}

const data = ref<AlertList>({ total: 0, page: 1, page_size: 50, items: [] })
const unackedOnly = ref(false)

async function load() {
  try {
    data.value = await api.get<AlertList>(`/alerts?unacked_only=${unackedOnly.value}&page_size=100`)
  } catch { /* ignore */ }
}

watch(unackedOnly, load)
onMounted(load)

async function ack(id: number) {
  await api.post(`/alerts/${id}/ack`)
  message.success('已确认')
  load()
}

async function ackAll() {
  await api.post('/alerts/ack-all')
  message.success('已全部确认')
  load()
}

async function triggerInspect() {
  const r = await api.post<{ findings: number; critical: number; warning: number }>('/alerts/inspect')
  message.success(`巡检完成：发现 ${r.findings} 条`)
  load()
}

const columns = [
  { title: '时间', dataIndex: 'ts', key: 'ts', width: 180 },
  { title: '级别', dataIndex: 'level', key: 'level' },
  { title: '分类', dataIndex: 'category', key: 'category' },
  { title: '消息', dataIndex: 'message', key: 'message', ellipsis: true },
  { title: '状态', key: 'status' },
  { title: '操作', key: 'action' },
]
</script>

<template>
  <div>
    <a-typography-title :level="4">监控告警</a-typography-title>
    <a-space style="margin-bottom: 16px">
      <a-button @click="() => unackedOnly = !unackedOnly">
        {{ unackedOnly ? '显示全部' : '仅未确认' }}
      </a-button>
      <a-button @click="ackAll">全部确认</a-button>
      <a-button type="primary" @click="triggerInspect">手动巡检</a-button>
    </a-space>
    <a-card>
      <a-table :data-source="data.items" :columns="columns" row-key="id" :pagination="{ pageSize: 20 }">
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'ts'">
            {{ new Date(record.ts).toLocaleString() }}
          </template>
          <template v-else-if="column.key === 'level'">
            <a-tag :color="levelColor[record.level] || 'default'">{{ record.level }}</a-tag>
          </template>
          <template v-else-if="column.key === 'status'">
            <a-tag v-if="record.acknowledged" color="green">已确认</a-tag>
            <a-tag v-else color="orange">未确认</a-tag>
          </template>
          <template v-else-if="column.key === 'action'">
            <a-button v-if="!record.acknowledged" size="small" @click="ack(record.id)">确认</a-button>
          </template>
        </template>
      </a-table>
    </a-card>
  </div>
</template>
