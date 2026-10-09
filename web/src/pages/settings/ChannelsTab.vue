<script setup lang="ts">
import { ref, reactive, onMounted, computed, nextTick } from 'vue'
import { api } from '../../api'
import { message } from 'ant-design-vue'

interface Channel {
  id: number; name: string; type: string; enabled: boolean; min_level: string;
  config: Record<string, unknown>; has_secret: boolean;
  fail_count: number; breaker_until: string | null; last_ok_at: string | null; last_error: string | null;
}

const channelTypeHint: Record<string, { name: string; configFields: string[]; needsSecret: boolean }> = {
  wecom:      { name: '企业微信群机器人', configFields: [], needsSecret: true },
  serverchan: { name: 'Server 酱',       configFields: [], needsSecret: true },
  telegram:   { name: 'Telegram Bot',    configFields: ['chat_id'], needsSecret: true },
  email:      { name: '邮件 SMTP',       configFields: ['host', 'port', 'username', 'from', 'to'], needsSecret: true },
  bark:       { name: 'Bark (iOS)',      configFields: ['host'], needsSecret: true },
  webhook:    { name: 'Webhook',         configFields: ['url'], needsSecret: false },
}

const LEVELS = [
  { value: 'info', label: '🔵 info' },
  { value: 'warning', label: '🟡 warning' },
  { value: 'critical', label: '🔴 critical' },
]

const data = ref<Channel[]>([])
const editing = ref<Channel | null>(null)
const creating = ref(false)
const modalOpen = ref(false)
const testing = ref(false)
const saving = ref(false)
const testResult = ref<{ ok: boolean; error: string } | null>(null)

const formState = reactive<Record<string, unknown>>({
  name: '',
  type: 'webhook',
  config: '',
  secret: '',
  min_level: 'info',
  enabled: false,
})
const formRef = ref()

const currentType = computed(() => formState.type as string)
const hint = computed(() => channelTypeHint[currentType.value])

async function load() {
  try {
    data.value = await api.get<Channel[]>('/alerts/channels')
  } catch { /* ignore */ }
}

onMounted(load)

function onCreate() {
  editing.value = null
  creating.value = true
  modalOpen.value = true
  testResult.value = null
  Object.assign(formState, {
    name: '', type: 'webhook', config: '', secret: '',
    min_level: 'info', enabled: false,
  })
  nextTick(() => formRef.value?.clearValidate())
}

function onEdit(c: Channel) {
  editing.value = c
  creating.value = false
  modalOpen.value = true
  testResult.value = null
  Object.assign(formState, { ...c })
}

async function onDelete(id: number) {
  await api.post('/alerts/channels/delete', { id })
  message.success('已删除')
  load()
}

async function onTest() {
  if (!editing.value) return
  testing.value = true
  testResult.value = null
  try {
    const r = await api.post<{ ok: boolean; error: string }>(`/alerts/channels/${editing.value.id}/test`)
    testResult.value = r
    if (r.ok) message.success('渠道测试成功')
    else message.error(`测试失败：${r.error}`)
  } finally {
    testing.value = false
  }
  load()
}

async function onSave() {
  if (saving.value) return
  saving.value = true
  try {
    if (creating.value) {
      const payload = { ...formState, config: typeof formState.config === 'string' ? JSON.parse(formState.config || '{}') : formState.config }
      await api.post('/alerts/channels', payload)
      message.success('渠道已创建')
    } else if (editing.value) {
      await api.post('/alerts/channels/update', { id: editing.value.id, ...formState })
      message.success('渠道已更新')
    }
    closeModal()
    load()
  } catch (e) {
    message.error(e instanceof Error ? e.message : '保存失败')
  } finally {
    saving.value = false
  }
}

function closeModal() {
  modalOpen.value = false
  editing.value = null
  creating.value = false
  testResult.value = null
  Object.assign(formState, {
    name: '', type: 'webhook', config: '', secret: '',
    min_level: 'info', enabled: false,
  })
}

function channelName(type: string): string {
  return channelTypeHint[type]?.name || type
}

const columns = [
  { title: '名称', dataIndex: 'name', key: 'name' },
  { title: '类型', dataIndex: 'type', key: 'type' },
  { title: '启用', dataIndex: 'enabled', key: 'enabled' },
  { title: '最低级别', dataIndex: 'min_level', key: 'min_level' },
  { title: '熔断', key: 'breaker' },
  { title: '状态', key: 'status' },
  { title: '操作', key: 'action' },
]
</script>

<template>
  <a-card title="通知渠道">
    <template #extra>
      <a-button type="primary" @click="onCreate">新增渠道</a-button>
    </template>
    <a-table :data-source="data" :columns="columns" row-key="id" :pagination="false">
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'type'">
          <a-tag color="blue">{{ channelName(record.type) }}</a-tag>
        </template>
        <template v-else-if="column.key === 'enabled'">
          <a-tag :color="record.enabled ? 'green' : 'default'">{{ record.enabled ? '是' : '否' }}</a-tag>
        </template>
        <template v-else-if="column.key === 'breaker'">
          <a-tag
            v-if="record.breaker_until && new Date(record.breaker_until) > new Date()"
            color="red"
          >熔断中（{{ record.fail_count }} 失败）</a-tag>
          <a-tag v-else color="green">正常</a-tag>
        </template>
        <template v-else-if="column.key === 'status'">
          <a-tooltip v-if="record.last_error" :title="record.last_error">
            <a-tag color="red">异常</a-tag>
          </a-tooltip>
          <a-tag v-else color="green">正常</a-tag>
        </template>
        <template v-else-if="column.key === 'action'">
          <a-space>
            <a-button size="small" @click="onEdit(record)">编辑</a-button>
            <a-popconfirm title="确认删除？" @confirm="onDelete(record.id)">
              <a-button size="small" danger>删除</a-button>
            </a-popconfirm>
          </a-space>
        </template>
      </template>
    </a-table>

    <a-modal
      :open="modalOpen"
      :title="creating ? '新增渠道' : `编辑渠道 - ${editing?.name || ''}`"
      :width="640"
      :confirm-loading="saving"
      @cancel="closeModal"
      @ok="onSave"
    >
      <a-form ref="formRef" layout="vertical">
        <a-form-item label="名称" name="name" :rules="[{ required: true }]">
          <a-input v-model:value="formState.name" />
        </a-form-item>
        <a-form-item label="类型" name="type" :rules="[{ required: true }]">
          <a-select v-model:value="formState.type" :disabled="!creating">
            <a-select-option
              v-for="[v, h] in Object.entries(channelTypeHint)"
              :key="v"
              :value="v"
            >{{ h.name }}</a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item
          v-if="hint && hint.configFields.length > 0"
          label="配置 (JSON)"
          name="config"
          :tooltip="`必填字段：${hint.configFields.join(', ')}`"
        >
          <a-textarea
            :rows="4"
            :placeholder="JSON.stringify(Object.fromEntries(hint.configFields.map((f) => [f, ''])), null, 2)"
            @change="(e: Event) => {
              try {
                formState.config = JSON.parse((e.target as HTMLTextAreaElement).value)
              } catch { /* ignore until valid */ }
            }"
          />
        </a-form-item>
        <a-form-item
          :label="hint?.needsSecret ? '密钥 / Token' : '认证头（可选）'"
          name="secret"
          :tooltip="editing?.has_secret ? '已设置；留空表示不修改' : 'Webhook 模式下作为 Authorization 头值'"
        >
          <a-input-password
            v-model:value="formState.secret"
            :placeholder="editing?.has_secret ? '已设置（留空保留）' : ''"
          />
        </a-form-item>
        <a-form-item label="最低接收级别" name="min_level">
          <a-select v-model:value="formState.min_level" :options="LEVELS" />
        </a-form-item>
        <a-form-item name="enabled" label="启用">
          <a-switch v-model:checked="formState.enabled" />
        </a-form-item>
      </a-form>

      <a-card v-if="editing" size="small" style="margin-top: 16px; background: #1f1f1f">
        <a-space>
          <a-button :loading="testing" @click="onTest">发送测试消息</a-button>
          <a-tag v-if="testResult" :color="testResult.ok ? 'green' : 'red'">
            {{ testResult.ok ? '✓ 成功' : `✗ 失败：${testResult.error}` }}
          </a-tag>
        </a-space>
      </a-card>
    </a-modal>
  </a-card>
</template>
