<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { api } from '../api'
import { message } from 'ant-design-vue'

interface Site {
  id: number; name: string; domain: string | null; adapter: string;
  enabled: boolean;
  rss_url_masked: string;
  has_apikey: boolean;
  has_cookies: boolean;
  min_interval_minutes: number;
  created_at: string;
  updated_at: string;
}

const ADAPTERS: { value: string; label: string; domain: string }[] = [
  { value: 'mteam', label: 'M-team', domain: 'm-team.io' },
  { value: 'hhanclub', label: 'HHanClub', domain: 'hhanclub.net' },
  { value: 'hdsky', label: 'HDSky', domain: 'hdsky.me' },
  { value: 'ourbits', label: 'OurBits', domain: 'ourbits.club' },
]

function domainFor(adapter: string): string {
  return ADAPTERS.find((a) => a.value === adapter)?.domain ?? ''
}

const data = ref<Site[]>([])
const editing = ref<Site | null>(null)
const creating = ref(false)
const testing = ref(false)
const testResult = ref<{ status: string; detail: string; cap: object } | null>(null)
const modalOpen = ref(false)

const formState = reactive<Record<string, unknown>>({})

async function load() {
  try {
    data.value = await api.get<Site[]>('/sites')
  } catch { /* ignore */ }
}

onMounted(load)

function onCreate() {
  editing.value = null
  creating.value = true
  modalOpen.value = true
  testResult.value = null
  Object.assign(formState, {
    adapter: 'mteam',
    min_interval_minutes: 60,
    enabled: true,
    name: '',
    domain: '',
    rss_url: '',
    apikey: '',
    cookies: '',
  })
}

function onAdapterChange(adapter: string) {
  formState.adapter = adapter
  if (creating.value) {
    formState.domain = domainFor(adapter)
  }
}

function onEdit(s: Site) {
  editing.value = s
  creating.value = false
  modalOpen.value = true
  testResult.value = null
  Object.assign(formState, {
    ...s,
    rss_url: '',
    apikey: '',
    cookies: '',
  })
}

async function onDelete(id: number) {
  await api.post('/sites/delete', { id })
  message.success('已删除')
  load()
}

async function onSave() {
  try {
    if (creating.value) {
      await api.post('/sites', formState)
      message.success('已创建')
    } else if (editing.value) {
      await api.post('/sites/update', { id: editing.value.id, ...formState })
      message.success('已更新')
    }
    creating.value = false
    editing.value = null
    modalOpen.value = false
    load()
  } catch (exc) {
    message.error(exc instanceof Error ? exc.message : '保存失败')
  }
}

async function onTest() {
  if (!editing.value) return
  testing.value = true
  testResult.value = null
  try {
    const r = await api.post<{ ok: boolean; status: string; detail: string; self_test: object }>(
      `/sites/${editing.value.id}/test`,
    )
    testResult.value = {
      status: r.status,
      detail: r.detail,
      cap: r.self_test,
    }
  } catch (exc) {
    message.error(exc instanceof Error ? exc.message : '测连失败')
  } finally {
    testing.value = false
  }
}

const columns = [
  { title: '名称', dataIndex: 'name', key: 'name' },
  { title: '适配器', dataIndex: 'adapter', key: 'adapter' },
  { title: '域名', dataIndex: 'domain', key: 'domain' },
  { title: '凭据', key: 'credentials' },
  { title: '启用', dataIndex: 'enabled', key: 'enabled' },
  { title: '操作', key: 'action' },
]
</script>

<template>
  <div>
    <a-typography-title :level="4">站点管理</a-typography-title>
    <a-card title="站点列表（凭据脱敏）">
      <template #extra>
        <a-button type="primary" @click="onCreate">新增站点</a-button>
      </template>
      <a-table
        :data-source="data"
        :columns="columns"
        row-key="id"
        :pagination="{ pageSize: 20 }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'name'">
            <b>{{ record.name }}</b>
          </template>
          <template v-else-if="column.key === 'adapter'">
            <a-tag>{{ record.adapter }}</a-tag>
          </template>
          <template v-else-if="column.key === 'domain'">
            <a-typography-text code style="font-size: 12px">{{ record.domain || '-' }}</a-typography-text>
          </template>
          <template v-else-if="column.key === 'credentials'">
            <a-space :size="4">
              <a-tag v-if="record.rss_url_masked" color="cyan">RSS</a-tag>
              <a-tag v-if="record.has_apikey" color="purple">API key</a-tag>
              <a-tag v-if="record.has_cookies" color="cyan">Cookies</a-tag>
              <a-tag v-if="!record.rss_url_masked && !record.has_apikey && !record.has_cookies">—</a-tag>
            </a-space>
          </template>
          <template v-else-if="column.key === 'enabled'">
            <a-tag v-if="record.enabled" color="green">✓</a-tag>
            <a-tag v-else>—</a-tag>
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
    </a-card>

    <a-modal
      :open="modalOpen"
      :title="creating ? '新增站点' : `编辑站点 - ${editing?.name || ''}`"
      :width="640"
      @cancel="() => { modalOpen = false; editing = null; creating = false; testResult = null }"
      @ok="onSave"
    >
      <a-form layout="vertical">
        <a-form-item label="适配器（自动带出域名）" name="adapter" :rules="[{ required: true }]">
          <a-select
            :value="formState.adapter"
            @change="onAdapterChange"
            :options="ADAPTERS.map(a => ({ value: a.value, label: a.label }))"
          />
        </a-form-item>
        <a-space style="width: 100%" size="middle">
          <a-form-item
            label="名称"
            name="name"
            :rules="[{ required: true, pattern: /^[a-zA-Z0-9_-]+$/, message: '字母/数字/下划线/连字符' }]"
            style="flex: 1"
          >
            <a-input v-model:value="formState.name" />
          </a-form-item>
          <a-form-item
            label="域名"
            name="domain"
            style="flex: 1"
          >
            <a-input v-model:value="formState.domain" placeholder="选择适配器后自动填充" />
          </a-form-item>
        </a-space>

        <a-form-item label="RSS URL（含 passkey，留空=不修改）" name="rss_url">
          <a-input-password
            v-model:value="formState.rss_url"
            placeholder="https://example.com/rss.php?feedkey=..."
          />
        </a-form-item>
        <a-form-item label="API Key（m-team 等，留空=不修改）" name="apikey">
          <a-input-password
            v-model:value="formState.apikey"
            :placeholder="editing && (editing as any).has_apikey ? '已配置（留空保留）' : '在 m-team 控制台 → API 令牌获取'"
            autocomplete="new-password"
          />
        </a-form-item>
        <a-form-item label="Cookies（NexusPHP 站点，留空=不修改）" name="cookies">
          <a-input-password
            v-model:value="formState.cookies"
            :placeholder="editing && (editing as any).has_cookies ? '已配置（留空保留）' : '浏览器 DevTools → Application → Cookies'"
            autocomplete="new-password"
          />
        </a-form-item>

        <a-form-item label="RSS 拉取最小间隔 (分钟)" name="min_interval_minutes">
          <a-input-number v-model:value="formState.min_interval_minutes" :min="15" :max="1440" style="width: 100%" />
        </a-form-item>

        <a-form-item name="enabled" label="启用">
          <a-switch v-model:checked="formState.enabled" />
        </a-form-item>
      </a-form>

      <template v-if="editing">
        <a-card size="small" style="margin-top: 16px; background: #1f1f1f">
          <a-space>
            <a-button :loading="testing" @click="onTest">连通性测试</a-button>
            <a-tag v-if="testResult" :color="testResult.status === 'ok' ? 'green' : 'red'">
              {{ testResult.status }} | {{ testResult.detail }}
            </a-tag>
          </a-space>
        </a-card>
      </template>
    </a-modal>
  </div>
</template>
