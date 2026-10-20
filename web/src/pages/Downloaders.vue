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
  save_path: string | null
  is_default: boolean
  enabled: boolean
  verify_tls: boolean
  is_cross_seed: boolean
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
const formRef = ref<{ validate: () => Promise<unknown> } | null>(null)

// 统一错误转字符串（避免 message.error 收到 [object Object]）
function errMsg(e: unknown, fallback: string): string {
  if (e && typeof e === 'object' && 'message' in e && typeof (e as { message: unknown }).message === 'string') {
    return (e as { message: string }).message
  }
  return fallback
}

const formState = reactive<Record<string, unknown>>({
  name: '',
  type: 'qbittorrent',
  url: '',
  username: '',
  password: '',
  save_path: '',
  is_default: false,
  enabled: true,
  verify_tls: true,
  is_cross_seed: false,
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
    save_path: '',
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
    save_path: d.save_path || '',
    is_default: d.is_default,
    enabled: d.enabled,
    verify_tls: d.verify_tls,
    is_cross_seed: d.is_cross_seed,
  })
}

async function onDelete(id: number) {
  try {
    await api.post('/downloaders/delete', { id })
    message.success('已删除')
    load()
  } catch (exc) {
    message.error(errMsg(exc, '删除失败'))
  }
}

async function onSave() {
  if (saving.value) return
  saving.value = true
  try {
    // 先校验表单（name 必填 + url 必填 + 名称 pattern）
    if (formRef.value) {
      await formRef.value.validate()
    }
    const payload: Record<string, unknown> = { ...formState }
    if (!payload.password) delete payload.password
    if (!payload.username) payload.username = null
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
    // validate 抛错是字段级 ValidationError（无 message，是数组）；API 抛 ApiError
    // 两者都走 errMsg 取出可读文本，避免 [object Object]
    message.error(errMsg(e, '保存失败'))
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
    message.error(errMsg(e, '测连失败'))
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
  { title: '名称', key: 'name' },
  { title: '类型', key: 'type', width: 140 },
  { title: 'URL', key: 'url' },
  { title: '保存路径', key: 'save_path', width: 200 },
  { title: '凭据', key: 'password', width: 100 },
  { title: '默认', key: 'default', width: 60 },
  { title: '辅种', key: 'cross_seed', width: 60 },
  { title: '启用', key: 'enabled', width: 60 },
  { title: '', key: 'action', width: 180 },
]
</script>

<template>
  <div>
    <header class="page-header">
      <div>
        <div class="page-header__eyebrow">下载器 · DOWNLOADERS</div>
        <h1 class="page-header__title">下载器实例</h1>
        <div class="page-header__sub">qBittorrent · Transmission 已完整适配 · 凭据 AES-256-GCM</div>
      </div>
      <a-button type="primary" @click="onCreate">新增下载器</a-button>
    </header>

    <hr class="divider-hairline" style="margin: 0" />

    <section class="section">
      <div class="panel">
      <a-table :data-source="data" :columns="columns" row-key="id" :pagination="{ pageSize: 20 }">
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'name'">
            <span class="row-name">{{ record.name }}</span>
          </template>
          <template v-else-if="column.key === 'type'">
            <a-space :size="4">
              <a-tag :color="isImplemented(record.type) ? 'blue' : 'default'">
                {{ typeLabel(record.type) }}
              </a-tag>
              <a-tag v-if="!isImplemented(record.type)" color="orange">未实现</a-tag>
            </a-space>
          </template>
          <template v-else-if="column.key === 'url'">
            <span class="mono code">{{ record.url }}</span>
          </template>
          <template v-else-if="column.key === 'save_path'">
            <span v-if="record.save_path" class="mono code">{{ record.save_path }}</span>
            <span v-else class="dim">—</span>
          </template>
          <template v-else-if="column.key === 'password'">
            <a-tag v-if="record.has_password" color="green">已配置</a-tag>
            <a-tag v-else>—</a-tag>
          </template>
          <template v-else-if="column.key === 'default'">
            <a-tag v-if="record.is_default" color="purple">★ 默认</a-tag>
            <span v-else class="dim">—</span>
          </template>
          <template v-else-if="column.key === 'cross_seed'">
            <a-tooltip v-if="record.is_cross_seed" title="标记为辅种下载器（由辅种巡查作业自动扫描并暂停异常种子）">
              <a-tag color="cyan">辅种</a-tag>
            </a-tooltip>
            <span v-else class="dim">—</span>
          </template>
          <template v-else-if="column.key === 'enabled'">
            <span v-if="record.enabled" class="status-dot status-dot--ok" />
            <span v-else class="status-dot status-dot--off" />
          </template>
          <template v-else-if="column.key === 'action'">
            <a-space :size="4">
              <a-button size="small" type="link" @click="onEdit(record)">编辑</a-button>
              <a-button size="small" type="link" :loading="testing" :disabled="!isImplemented(record.type)" @click="onTest(record)">测连</a-button>
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
      :title="creating ? '新增下载器' : `编辑下载器 · ${editing?.name || ''}`"
      :width="640"
      :confirm-loading="saving"
      @cancel="closeModal"
      @ok="onSave"
    >
      <a-form
        ref="formRef"
        layout="vertical"
        :model="formState"
      >
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
        <a-form-item
          label="保存路径（可选）"
          name="save_path"
          tooltip="推送/添加种子时传给下载器（qB savepath / TR download_dir）。填下载器容器视角的路径，如 /data/torrents；留空用下载器默认路径。"
        >
          <a-input v-model:value="formState.save_path" placeholder="/data/torrents" allow-clear />
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
        <a-form-item label="设为默认下载器" name="is_default" tooltip="辅种和免费种会自动投到默认下载器">
          <a-switch v-model:checked="formState.is_default" />
        </a-form-item>
        <a-form-item label="校验 TLS 证书" name="verify_tls" tooltip="自签名证书场景下关闭">
          <a-switch v-model:checked="formState.verify_tls" />
        </a-form-item>
        <a-form-item label="辅种下载器" name="is_cross_seed" tooltip="标记此下载器参与辅种巡查（定时作业自动扫描并暂停 tracker 未注册 / 数据丢失的种子）">
          <a-switch v-model:checked="formState.is_cross_seed" />
        </a-form-item>
        <a-form-item name="enabled" label="启用">
          <a-switch v-model:checked="formState.enabled" />
        </a-form-item>
      </a-form>

      <div v-if="editing" class="test-card">
        <a-space direction="vertical" style="width: 100%">
          <a-space>
            <a-button :loading="testing" @click="onTest()">测连（实际适配器 ping）</a-button>
            <a-tag v-if="testResult" :color="testResult.reachable ? 'green' : 'red'">
              {{ testResult.reachable ? '✓ 已连通' : `✗ ${testResult.detail}` }}
            </a-tag>
            <a-tag v-if="testResult?.supports_data_deletion" color="purple">支持数据删除</a-tag>
          </a-space>
          <span v-if="testResult?.capabilities" class="mono dim">
            能力：{{ testResult.capabilities.join(', ') }}
          </span>
        </a-space>
      </div>
    </a-modal>
  </div>
</template>

<style scoped>
.row-name {
  font-weight: 500;
}

.dim {
  color: var(--text-tertiary);
}

.code {
  font-size: var(--text-xs);
  color: var(--text-tertiary);
}

.status-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
}

.status-dot--ok {
  background: var(--status-green);
  box-shadow: 0 0 8px var(--status-green-soft);
}

.status-dot--off {
  background: var(--text-tertiary);
}

.test-card {
  margin-top: var(--sp-4);
  padding: var(--sp-3);
  background: var(--bg-surface);
  border: 1px solid var(--border-hairline);
  border-radius: var(--r-md);
}
</style>
