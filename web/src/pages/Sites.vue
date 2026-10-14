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
const testingId = ref<number | null>(null)
const testResults = ref<Record<number, { status: string; detail: string }>>({})
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

async function onTestRow(s: Site) {
  if (testingId.value !== null) return
  testingId.value = s.id
  delete testResults.value[s.id]
  try {
    const r = await api.post<{ ok: boolean; status: string; detail: string; self_test: object }>(
      `/sites/${s.id}/test`,
    )
    testResults.value[s.id] = { status: r.status, detail: r.detail }
  } catch (exc) {
    message.error(exc instanceof Error ? exc.message : '测连失败')
  } finally {
    testingId.value = null
  }
}

async function onToggleEnabled(s: Site, next: boolean) {
  // 乐观更新：先改 UI，失败回滚
  const prev = s.enabled
  s.enabled = next
  try {
    await api.post('/sites/update', { id: s.id, enabled: next })
  } catch (exc) {
    s.enabled = prev
    message.error(exc instanceof Error ? exc.message : '切换失败')
  }
}

const columns = [
  { title: '名称', key: 'name' },
  { title: '适配器', key: 'adapter' },
  { title: '域名', key: 'domain' },
  { title: '凭据', key: 'credentials' },
  { title: '间隔', key: 'interval', width: 90 },
  { title: '启用', key: 'enabled', width: 80 },
  { title: '', key: 'action', width: 280 },
]
</script>

<template>
  <div>
    <header class="page-header">
      <div>
        <div class="page-header__eyebrow">站点 · SITES</div>
        <h1 class="page-header__title">站点适配器</h1>
        <div class="page-header__sub">凭据脱敏存储（AES-256-GCM）· 支持 4 类私有种子站点</div>
      </div>
      <a-button type="primary" @click="onCreate">新增站点</a-button>
    </header>

    <hr class="divider-hairline" style="margin: 0" />

    <section class="section">
      <div class="panel">
      <a-table
        :data-source="data"
        :columns="columns"
        row-key="id"
        :pagination="{ pageSize: 20 }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'name'">
            <span class="row-name">{{ record.name }}</span>
          </template>
          <template v-else-if="column.key === 'adapter'">
            <span class="mono dim">{{ record.adapter }}</span>
          </template>
          <template v-else-if="column.key === 'domain'">
            <span class="mono code">{{ record.domain || '—' }}</span>
          </template>
          <template v-else-if="column.key === 'credentials'">
            <a-space :size="4">
              <a-tag v-if="record.rss_url_masked" color="cyan">RSS</a-tag>
              <a-tag v-if="record.has_apikey" color="purple">API key</a-tag>
              <a-tag v-if="record.has_cookies" color="cyan">Cookies</a-tag>
              <a-tag v-if="!record.rss_url_masked && !record.has_apikey && !record.has_cookies">—</a-tag>
            </a-space>
          </template>
          <template v-else-if="column.key === 'interval'">
            <span class="mono">{{ record.min_interval_minutes }}m</span>
          </template>
          <template v-else-if="column.key === 'enabled'">
            <a-switch
              :checked="record.enabled"
              size="small"
              @change="(v: boolean) => onToggleEnabled(record, v)"
            />
          </template>
          <template v-else-if="column.key === 'action'">
            <a-space :size="4" align="center">
              <a-button
                size="small"
                type="link"
                :loading="testingId === record.id"
                @click="onTestRow(record)"
              >
                测连
              </a-button>
              <a-tag
                v-if="testResults[record.id]"
                :color="testResults[record.id].status === 'ok' ? 'green' : 'red'"
                size="small"
              >
                {{ testResults[record.id].status }} · {{ testResults[record.id].detail }}
              </a-tag>
              <a-button size="small" type="link" @click="onEdit(record)">编辑</a-button>
              <a-popconfirm title="确认删除？" @confirm="onDelete(record.id)">
                <a-button size="small" type="link" danger>删除</a-button>
              </a-popconfirm>
            </a-space>
          </template>
        </template>
      </a-table>
      </div>
    </section>

    <a-modal
      :open="modalOpen"
      :title="creating ? '新增站点' : `编辑站点 · ${editing?.name || ''}`"
      :width="640"
      @cancel="() => { modalOpen = false; editing = null; creating = false }"
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
    </a-modal>
  </div>
</template>

<style scoped>
.row-name {
  font-weight: 500;
}

.dim {
  color: var(--text-secondary);
}

.code {
  font-size: var(--text-xs);
  color: var(--text-tertiary);
}
</style>
