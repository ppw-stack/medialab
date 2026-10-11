<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { api } from '../api'
import { message } from 'ant-design-vue'

interface Downloader {
  id: number
  name: string
  type: string
  url: string
  username: string | null
  has_password: boolean
  download_root: string | null
  is_default: boolean
  enabled: boolean
  verify_tls: boolean
  created_at: string
  updated_at: string
}

const TYPES = [
  { value: 'qbittorrent', label: 'qBittorrent', implemented: true },
  { value: 'transmission', label: 'Transmission', implemented: true },
  { value: 'deluge', label: 'Deluge', implemented: false },
  { value: 'aria2', label: 'aria2', implemented: false },
]

const data = ref<Downloader[]>([])
const editing = ref<Downloader | null>(null)
const creating = ref(false)
const testing = ref(false)
const testResult = ref<{ reachable: boolean; detail: string; capabilities?: string[]; supports_data_deletion?: boolean } | null>(null)
const modalOpen = ref(false)
const saving = ref(false)

const formState = reactive<Record<string, unknown>>({
  name: '',
  type: 'qbittorrent',
  url: '',
  username: '',
  password: '',
  download_root: '',
  is_default: false,
  enabled: true,
  verify_tls: true,
})

async function load() {
  try {
    data.value = await api.get<Downloader[]>('/downloaders')
  } catch {
    /* ignore */
  }
}

onMounted(load)

function onCreate() {
  editing.value = null
  creating.value = true
  modalOpen.value = true
  testResult.value = null
  Object.assign(formState, {
    name: '',
    type: 'qbittorrent',
    url: 'http://',
    username: 'admin',
    password: '',
    download_root: '',
    is_default: false,
    enabled: true,
    verify_tls: true,
  })
}

function onEdit(d: Downloader) {
  editing.value = d
  creating.value = false
  modalOpen.value = true
  testResult.value = null
  Object.assign(formState, {
    name: d.name,
    type: d.type,
    url: d.url,
    username: d.username || '',
    password: '',
    download_root: d.download_root || '',
    is_default: d.is_default,
    enabled: d.enabled,
    verify_tls: d.verify_tls,
  })
}

async function onDelete(id: number) {
  await api.post('/downloaders/delete', { id })
  message.success('已删除')
  load()
}

async function onSave() {
  if (saving.value) return
  saving.value = true
  try {
    const payload: Record<string, unknown> = { ...formState }
    if (!payload.password) delete payload.password
    if (!payload.username) payload.username = null
    if (!payload.download_root) payload.download_root = null
    if (creating.value) {
      await api.post('/downloaders', payload)
      message.success('已创建')
    } else if (editing.value) {
      await api.post('/downloaders/update', { id: editing.value.id, ...payload })
      message.success('已更新')
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
}

async function onTest(record?: Downloader) {
  if (testing.value) return
  const target = record ?? editing.value
  if (!target) return
  testing.value = true
  testResult.value = null
  try {
    const r = await api.post<{ reachable: boolean; detail: string; capabilities?: string[]; supports_data_deletion?: boolean }>(
      `/downloaders/${target.id}/test`,
      {},
    )
    testResult.value = {
      reachable: r.reachable,
      detail: r.detail,
      capabilities: r.capabilities,
      supports_data_deletion: r.supports_data_deletion,
    }
    if (r.reachable) {
      message.success('测连成功')
    } else {
      message.error(`测连失败：${r.detail}`)
    }
  } catch (e) {
    message.error(e instanceof Error ? e.message : '测连失败')
  } finally {
    testing.value = false
  }
}

function typeLabel(type: string): string {
  return TYPES.find((t) => t.value === type)?.label || type
}

function isImplemented(type: string): boolean {
  return TYPES.find((t) => t.value === type)?.implemented ?? false
}

const columns = [
  { title: '名称', dataIndex: 'name', key: 'name' },
  { title: '类型', dataIndex: 'type', key: 'type' },
  { title: 'URL', dataIndex: 'url', key: 'url' },
  { title: '用户名', key: 'username' },
  { title: '凭据', key: 'password' },
  { title: '存储根', key: 'download_root' },
  { title: '默认', dataIndex: 'is_default', key: 'default' },
  { title: '启用', dataIndex: 'enabled', key: 'enabled' },
  { title: '操作', key: 'action' },
]
</script>

<template>
  <div>
    <a-typography-title :level="4">下载器管理</a-typography-title>
    <a-card title="下载器实例（凭据脱敏）">
      <template #extra>
        <a-button type="primary" @click="onCreate">新增下载器</a-button>
      </template>
      <a-table :data-source="data" :columns="columns" row-key="id" :pagination="{ pageSize: 20 }">
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'name'">
            <b>{{ record.name }}</b>
          </template>
          <template v-else-if="column.key === 'type'">
            <a-tag :color="isImplemented(record.type) ? 'blue' : 'default'">
              {{ typeLabel(record.type) }}
            </a-tag>
            <a-tag v-if="!isImplemented(record.type)" color="orange">未实现</a-tag>
          </template>
          <template v-else-if="column.key === 'url'">
            <a-typography-text code style="font-size: 12px">{{ record.url }}</a-typography-text>
          </template>
          <template v-else-if="column.key === 'username'">
            {{ record.username || '—' }}
          </template>
          <template v-else-if="column.key === 'password'">
            <a-tag v-if="record.has_password" color="green">已配置</a-tag>
            <a-tag v-else>—</a-tag>
          </template>
          <template v-else-if="column.key === 'download_root'">
            <a-typography-text v-if="record.download_root" code style="font-size: 12px">
              {{ record.download_root }}
            </a-typography-text>
            <span v-else>—</span>
          </template>
          <template v-else-if="column.key === 'default'">
            <a-tag v-if="record.is_default" color="purple">★ 默认</a-tag>
            <span v-else>—</span>
          </template>
          <template v-else-if="column.key === 'enabled'">
            <a-tag v-if="record.enabled" color="green">✓</a-tag>
            <a-tag v-else>—</a-tag>
          </template>
          <template v-else-if="column.key === 'action'">
            <a-space>
              <a-button size="small" @click="onEdit(record)">编辑</a-button>
              <a-button size="small" :loading="testing" :disabled="!isImplemented(record.type)" @click="onTest(record)">测连</a-button>
              <a-popconfirm title="确认删除？" @confirm="onDelete(record.id)">
                <a-button size="small" danger>删除</a-button>
              </a-popconfirm>
            </a-space>
          </template>
        </template>
      </a-table>
    </a-card>

    <a-modal
      :open="modalOpen"
      :title="creating ? '新增下载器' : `编辑下载器 - ${editing?.name || ''}`"
      :width="640"
      :confirm-loading="saving"
      @cancel="closeModal"
      @ok="onSave"
    >
      <a-form layout="vertical">
        <a-space style="width: 100%" size="middle">
          <a-form-item
            label="名称"
            name="name"
            :rules="[{ required: true, pattern: /^[A-Za-z0-9_.-]+$/, message: '字母/数字/_/-/.' }]"
            style="flex: 1"
          >
            <a-input v-model:value="formState.name" placeholder="qbt-main" />
          </a-form-item>
          <a-form-item label="类型" name="type" style="flex: 1">
            <a-select v-model:value="formState.type">
              <a-select-option
                v-for="t in TYPES"
                :key="t.value"
                :value="t.value"
                :disabled="!t.implemented"
              >
                {{ t.label }}{{ t.implemented ? '' : '（未实现）' }}
              </a-select-option>
            </a-select>
          </a-form-item>
        </a-space>
        <a-form-item label="URL（含协议与端口）" name="url" :rules="[{ required: true }]">
          <a-input v-model:value="formState.url" placeholder="http://qbittorrent:8080" />
        </a-form-item>
        <a-space style="width: 100%" size="middle">
          <a-form-item label="用户名" name="username" style="flex: 1">
            <a-input v-model:value="formState.username" />
          </a-form-item>
          <a-form-item
            :label="editing?.has_password ? '密码（留空保留）' : '密码'"
            name="password"
            style="flex: 1"
          >
            <a-input-password v-model:value="formState.password" autocomplete="new-password" />
          </a-form-item>
        </a-space>
        <a-form-item
          label="下载根目录（路径校验基准；空 = 禁用数据删除）"
          name="download_root"
        >
          <a-input v-model:value="formState.download_root" placeholder="/downloads" />
        </a-form-item>
        <a-form-item label="设为默认下载器" name="is_default" tooltip="辅种和免费种会自动投到默认下载器">
          <a-switch v-model:checked="formState.is_default" />
        </a-form-item>
        <a-form-item label="校验 TLS 证书" name="verify_tls" tooltip="自签名证书场景下关闭">
          <a-switch v-model:checked="formState.verify_tls" />
        </a-form-item>
        <a-form-item name="enabled" label="启用">
          <a-switch v-model:checked="formState.enabled" />
        </a-form-item>
      </a-form>

      <a-card v-if="editing" size="small" style="margin-top: 16px; background: #1f1f1f">
        <a-space direction="vertical" style="width: 100%">
          <a-space>
            <a-button :loading="testing" @click="onTest()">测连（实际适配器 ping）</a-button>
            <a-tag v-if="testResult" :color="testResult.reachable ? 'green' : 'red'">
              {{ testResult.reachable ? '✓ 已连通' : `✗ ${testResult.detail}` }}
            </a-tag>
            <a-tag v-if="testResult?.supports_data_deletion" color="purple">支持数据删除</a-tag>
          </a-space>
          <a-typography-text v-if="testResult?.capabilities" type="secondary" style="font-size: 12px">
            能力：{{ testResult.capabilities.join(', ') }}
          </a-typography-text>
        </a-space>
      </a-card>
    </a-modal>
  </div>
</template>
