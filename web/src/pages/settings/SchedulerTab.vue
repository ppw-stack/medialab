<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { api } from '../../api'
import { message } from 'ant-design-vue'

interface Job {
  name: string;
  interval_seconds: number;
  enabled: boolean;
  running: boolean;
}

interface SchedulerInfo {
  enabled: boolean;
  jobs: Job[];
  inflight: number;
  accepting_new_work: boolean;
}

const JOB_LABELS: Record<string, { name: string; desc: string }> = {
  cross_seed:     { name: '辅种拉取',     desc: 'FR-2：每小时从各站点 RSS 拉取一次（受 FR-2.2 频率下限约束）' },
  queue_replay:   { name: '队列重放',     desc: '§6：每 5 分钟消费 task_queue 中 pending 任务（崩溃恢复）' },
  inspect:        { name: '巡检',         desc: 'FR-4.1：每 15 分钟巡检全部任务 + 触发告警' },
  free_seed_poll: { name: '免费种拉取',   desc: 'FR-3：每 10 分钟捕获免费种候选 + 添加到下载器' },
  window_watch:   { name: '窗口看护',     desc: 'FR-3.4：每 60 秒检查在途免费种，按紧迫度调整' },
  cleanup_aborts: { name: '中止处理',     desc: 'M5：每 5 分钟消费 M4 中止产物走 Cleaner 管线' },
}

const data = ref<SchedulerInfo | null>(null)
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

function jobLabel(name: string) {
  return JOB_LABELS[name]
}

const columns = [
  { title: '作业', dataIndex: 'name', key: 'name' },
  { title: '说明', key: 'desc' },
  { title: '间隔', dataIndex: 'interval_seconds', key: 'interval' },
  { title: '运行中', dataIndex: 'running', key: 'running' },
  { title: '启用', dataIndex: 'enabled', key: 'enabled' },
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
        <template v-else-if="column.key === 'interval'">
          {{ record.interval_seconds >= 60 ? `${record.interval_seconds / 60}min` : `${record.interval_seconds}s` }}
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
      </template>
    </a-table>
    <a-typography-paragraph v-if="!data.enabled" type="warning" style="margin-top: 16px">
      调度器已禁用（SCHEDULER_ENABLED=false）。所有周期性作业都不会执行。
    </a-typography-paragraph>
  </a-card>
</template>
