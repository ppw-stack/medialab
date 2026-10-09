<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { api } from '../api'
import { message } from 'ant-design-vue'

interface Site {
  id: number; name: string; domain: string; adapter: string;
  enabled: boolean;
  rss_url_masked: string;
  default_free_duration: number;
  min_buffer: number;
  buffer_ratio: number;
  clock_offset: number;
  clock_offset_probed_at: string | null;
  rescue_strategy: string;
  grab_short_window: boolean;
}

const ADAPTERS = [
  { value: 'generic', label: 'GenericSiteAdapter (推荐)' },
]

const RESCUE_STRATEGIES = [
  { value: 'notify_only', label: '仅告警（默认，最安全）' },
  { value: 'speed_up', label: '提速：临时清除下载限速' },
  { value: 'yield_then_retry', label: '让路后重试（让上传优先）' },
  { value: 'abort_now', label: '立即中止（提前止损）' },
]

function formatDuration(s: number): string {
  if (s < 3600) return `${Math.round(s / 60)}min`
  if (s < 86400) return `${(s / 3600).toFixed(1)}h`
  return `${(s / 86400).toFixed(1)}d`
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
    adapter: 'generic',
    default_free_duration: 72,
    min_buffer: 30,
    buffer_ratio: 0.10,
    min_interval_minutes: 60,
    enabled: true,
    save_path_template: '/data/torrents/{site}/{name}',
    rescue_strategy: 'notify_only',
    grab_short_window: false,
    name: '',
    domain: '',
    rss_url: '',
    max_size: undefined,
  })
}

function onEdit(s: Site) {
  editing.value = s
  creating.value = false
  modalOpen.value = true
  testResult.value = null
  Object.assign(formState, {
    ...s,
    default_free_duration: s.default_free_duration / 3600,
    min_buffer: s.min_buffer / 60,
    rss_url: '',
  })
}

async function onDelete(id: number) {
  await api.post('/sites/delete', { id })
  message.success('已删除')
  load()
}

async function onSave() {
  const v = { ...formState }
  const payload = {
    ...v,
    default_free_duration: v.default_free_duration ? `${v.default_free_duration}h` : '72h',
    min_buffer: v.min_buffer ? `${v.min_buffer}m` : '30m',
  }
  if (creating.value) {
    await api.post('/sites', payload)
    message.success('已创建')
  } else if (editing.value) {
    await api.post('/sites/update', { id: editing.value.id, ...payload })
    message.success('已更新')
  }
  creating.value = false
  editing.value = null
  modalOpen.value = false
  load()
}

async function onTest() {
  if (!editing.value) return
  testing.value = true
  testResult.value = null
  try {
    const r = await api.post<{ ok: boolean; status: string; detail: string; self_test: object }>(
      `/sites/${editing.value.id}/feed-test`,
    )
    testResult.value = {
      status: r.status,
      detail: r.detail,
      cap: r.self_test,
    }
  } finally {
    testing.value = false
  }
}

async function onClockAdjust(id: number, delta: number) {
  try {
    await api.post(`/sites/${id}/clock-offset`, { delta_seconds: delta })
    message.success(`时钟偏差调整 ${delta}s`)
    load()
  } catch (e) {
    message.error(e instanceof Error ? e.message : '失败')
  }
}

function rescueLabel(v: string): string {
  return RESCUE_STRATEGIES.find((r) => r.value === v)?.label || v
}

const columns = [
  { title: '名称', dataIndex: 'name', key: 'name' },
  { title: '适配器', dataIndex: 'adapter', key: 'adapter' },
  { title: 'RSS', dataIndex: 'rss_url_masked', key: 'rss' },
  { title: '时钟偏差', dataIndex: 'clock_offset', key: 'clock' },
  { title: '免费窗口', dataIndex: 'default_free_duration', key: 'dur' },
  { title: '缓冲', dataIndex: 'buffer_ratio', key: 'buf' },
  { title: '抢救', dataIndex: 'rescue_strategy', key: 'rs' },
  { title: '启用', dataIndex: 'enabled', key: 'enabled' },
  { title: '操作', key: 'action' },
]
</script>

<template>
  <div>
    <a-typography-title :level="4">站点管理</a-typography-title>
    <a-card
      title="FR-2.1 站点列表（凭据脱敏）"
    >
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
            <div>
              <b>{{ record.name }}</b>
              <div style="font-size: 12px; color: rgba(255,255,255,0.45)">{{ record.domain }}</div>
            </div>
          </template>
          <template v-else-if="column.key === 'adapter'">
            <a-tag>{{ record.adapter }}</a-tag>
          </template>
          <template v-else-if="column.key === 'rss'">
            <a-tooltip :title="record.rss_url_masked">
              <a-typography-text code style="font-size: 12px">{{ record.rss_url_masked || '-' }}</a-typography-text>
            </a-tooltip>
          </template>
          <template v-else-if="column.key === 'clock'">
            <a-space :size="4">
              <a-tag :color="record.clock_offset > 60 ? 'red' : record.clock_offset < -60 ? 'orange' : 'green'">
                {{ record.clock_offset > 0 ? '+' : '' }}{{ record.clock_offset }}s
              </a-tag>
              <a-tooltip title="微调（站点偏快则 -）">
                <a-button size="small" @click="onClockAdjust(record.id, -1)">−1s</a-button>
              </a-tooltip>
              <a-button size="small" @click="onClockAdjust(record.id, 1)">+1s</a-button>
            </a-space>
          </template>
          <template v-else-if="column.key === 'dur'">
            {{ formatDuration(record.default_free_duration) }}
          </template>
          <template v-else-if="column.key === 'buf'">
            {{ (record.buffer_ratio * 100).toFixed(0) }}%
          </template>
          <template v-else-if="column.key === 'rs'">
            <a-tag color="purple">{{ rescueLabel(record.rescue_strategy) }}</a-tag>
          </template>
          <template v-else-if="column.key === 'enabled'">
            <a-tag v-if="record.enabled" color="green">✓</a-tag>
            <a-tag v-else>—</a-tag>
          </template>
          <template v-else-if="column.key === 'action'">
            <a-space>
              <a-button size="small" @click="onEdit(record)">编辑</a-button>
              <a-button size="small" @click="onTest()">测试</a-button>
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
      :width="720"
      @cancel="() => { modalOpen = false; editing = null; creating = false; testResult = null }"
      @ok="onSave"
    >
      <a-form layout="vertical">
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
            :rules="[{ required: true }]"
            style="flex: 1"
          >
            <a-input v-model:value="formState.domain" placeholder="example-pt.com" />
          </a-form-item>
        </a-space>
        <a-form-item label="RSS URL（含 passkey，存库前加密）" name="rss_url">
          <a-input-password
            v-model:value="formState.rss_url"
            placeholder="https://example.com/rss.php?feedkey=..."
          />
        </a-form-item>
        <a-form-item
          label="保存路径模板"
          name="save_path_template"
          tooltip="支持占位符：{site} {name} {category} {info_hash}"
        >
          <a-input v-model:value="formState.save_path_template" />
        </a-form-item>
        <a-space style="width: 100%" size="middle">
          <a-form-item label="适配器" name="adapter" style="flex: 1">
            <a-select v-model:value="formState.adapter" :options="ADAPTERS" />
          </a-form-item>
          <a-form-item
            label="RSS 最小间隔 (分钟)"
            name="min_interval_minutes"
            tooltip="FR-2.2：下限 15 分钟"
          >
            <a-input-number v-model:value="formState.min_interval_minutes" :min="15" :max="1440" style="width: 100%" />
          </a-form-item>
          <a-form-item
            label="体积上限 (GB)"
            name="max_size"
            tooltip="辅种过滤；空=不限"
          >
            <a-input-number v-model:value="formState.max_size" :min="0" style="width: 100%" />
          </a-form-item>
        </a-space>
        <a-typography-title :level="5" style="margin-top: 8px">免费窗口（FR-3.0.1）</a-typography-title>
        <a-space style="width: 100%" size="middle">
          <a-form-item
            label="默认窗口 (小时)"
            name="default_free_duration"
            tooltip="该站无法解析窗口时的兜底值"
          >
            <a-input-number v-model:value="formState.default_free_duration" :min="1" :max="720" style="width: 100%" />
          </a-form-item>
          <a-form-item
            label="安全余量 (分钟)"
            name="min_buffer"
            tooltip="max(窗口×ratio, min_buffer)"
          >
            <a-input-number v-model:value="formState.min_buffer" :min="1" style="width: 100%" />
          </a-form-item>
          <a-form-item label="余量比例" name="buffer_ratio">
            <a-input-number v-model:value="formState.buffer_ratio" :min="0.01" :max="0.5" :step="0.01" style="width: 100%" />
          </a-form-item>
        </a-space>
        <a-form-item
          label="短窗口强制抓取"
          name="grab_short_window"
          tooltip="剩余 < 90min 时通常跳过；开启后即使短也抓"
        >
          <a-switch v-model:checked="formState.grab_short_window" />
        </a-form-item>
        <a-form-item
          label="抢救策略 (FR-3.8)"
          name="rescue_strategy"
          tooltip="到红线时执行：通知/提速/让路/立即中止"
        >
          <a-select v-model:value="formState.rescue_strategy" :options="RESCUE_STRATEGIES" />
        </a-form-item>
        <a-form-item name="enabled" label="启用">
          <a-switch v-model:checked="formState.enabled" />
        </a-form-item>
      </a-form>

      <template v-if="editing">
        <a-card size="small" style="margin-top: 16px; background: #1f1f1f">
          <a-space>
            <a-button :loading="testing" @click="onTest">RSS 测试 + 窗口解析自检</a-button>
            <a-tag v-if="testResult" :color="testResult.status === 'ok' ? 'green' : 'red'">
              {{ testResult.status }} | {{ testResult.detail }}
            </a-tag>
          </a-space>
        </a-card>
      </template>
    </a-modal>
  </div>
</template>
