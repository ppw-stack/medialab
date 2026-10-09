<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { api } from '../api'
import { message } from 'ant-design-vue'

interface Rule {
  id: number;
  name: string;
  enabled: boolean;
  priority: number;
  when: Record<string, unknown>;
  dry_run_done: boolean;
  created_at: string;
}

const rules = ref<Rule[]>([])
const modalOpen = ref(false)
const formState = reactive({
  name: '',
  when: '{"ratio_gte": 3.0}',
  enabled: true,
  priority: 100,
})

async function loadRules() {
  try {
    rules.value = await api.get<Rule[]>('/cleanup/rules')
  } catch { /* ignore */ }
}

onMounted(loadRules)

async function dryRun(id: number) {
  try {
    const r = await api.post<{ matched: number }>(`/cleanup/rules/${id}/dry-run`)
    message.success(`干跑匹配 ${r.matched} 个任务`)
    loadRules()
  } catch (e) {
    message.error(e instanceof Error ? e.message : '失败')
  }
}

async function runRule(id: number) {
  try {
    const r = await api.post<{
      candidates_executed: number;
      freed_space: number;
      pending_confirmation: number;
    }>(`/cleanup/rules/${id}/run`)
    if (r.pending_confirmation) {
      message.warning('超阈值，已进入二次确认队列')
    } else {
      message.success(`已清理 ${r.candidates_executed} 个任务`)
    }
    loadRules()
  } catch (e) {
    message.error(e instanceof Error ? e.message : '失败')
  }
}

function closeModal() {
  modalOpen.value = false
  formState.name = ''
  formState.when = '{"ratio_gte": 3.0}'
}

async function createRule() {
  const whenStr = formState.when || '{}'
  try {
    const when = JSON.parse(whenStr)
    await api.post('/cleanup/rules', {
      name: formState.name,
      when,
      enabled: formState.enabled,
      priority: formState.priority,
    })
    message.success('规则已创建')
    modalOpen.value = false
    Object.assign(formState, { name: '', when: '{"ratio_gte": 3.0}', enabled: true, priority: 100 })
    loadRules()
  } catch {
    message.error('JSON 格式错误')
  }
}

const columns = [
  { title: '名称', dataIndex: 'name', key: 'name' },
  { title: '优先级', dataIndex: 'priority', key: 'priority' },
  { title: '启用', dataIndex: 'enabled', key: 'enabled' },
  { title: '干跑', dataIndex: 'dry_run_done', key: 'dry_run_done' },
  { title: '条件', dataIndex: 'when', key: 'when', ellipsis: true },
  { title: '操作', key: 'action' },
]
</script>

<template>
  <div>
    <a-typography-title :level="4">清理中心</a-typography-title>
    <a-card title="清理规则">
      <template #extra>
        <a-button type="primary" @click="modalOpen = true">新增规则</a-button>
      </template>
      <a-table :data-source="rules" :columns="columns" row-key="id" :pagination="false">
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'enabled'">
            <a-tag :color="record.enabled ? 'green' : 'default'">{{ record.enabled ? '是' : '否' }}</a-tag>
          </template>
          <template v-else-if="column.key === 'dry_run_done'">
            <a-tag :color="record.dry_run_done ? 'blue' : 'orange'">{{ record.dry_run_done ? '已完成' : '待执行' }}</a-tag>
          </template>
          <template v-else-if="column.key === 'when'">
            {{ JSON.stringify(record.when) }}
          </template>
          <template v-else-if="column.key === 'action'">
            <a-space>
              <a-button size="small" @click="dryRun(record.id)">干跑</a-button>
              <a-button size="small" type="primary" @click="runRule(record.id)">执行</a-button>
            </a-space>
          </template>
        </template>
      </a-table>
    </a-card>

    <a-modal
      title="新增清理规则"
      :open="modalOpen"
      @ok="createRule"
      @cancel="closeModal"
    >
      <a-form layout="vertical">
        <a-form-item name="name" label="规则名" :rules="[{ required: true }]">
          <a-input v-model:value="formState.name" placeholder="如：高种比清理" />
        </a-form-item>
        <a-form-item name="when" label="条件 (JSON)">
          <a-textarea
            v-model:value="formState.when"
            :rows="4"
            placeholder='{"ratio_gte": 3.0, "seeding_days_gte": 30}'
          />
        </a-form-item>
        <a-form-item name="priority" label="优先级">
          <a-input-number v-model:value="formState.priority" />
        </a-form-item>
        <a-form-item name="enabled" label="启用">
          <a-switch v-model:checked="formState.enabled" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>
