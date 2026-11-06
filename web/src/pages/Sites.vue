<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { api, errMsg } from '../api'
import type {
  AdapterInfo,
  FeedTestResult,
  Site,
  SiteTestAllResult,
} from '../types'
import {
  formatBytes as fmtBytes,
  formatBonus as fmtBonus,
  formatBonusTooltip as bonusTooltip,
} from '../utils/format'
import { message } from 'ant-design-vue'
import type { FormInstance } from 'ant-design-vue'
import DataTable from '../components/DataTable.vue'
import PageHeader from '../components/PageHeader.vue'

// 硬编码 fallback（后端 /sites/adapters 不可用时使用）
const ADAPTERS_FALLBACK: { value: string; label: string; domain: string }[] = [
  { value: 'mteam', label: 'M-team', domain: 'm-team.io' },
  { value: 'hhanclub', label: 'HHanClub', domain: 'hhanclub.net' },
  { value: 'hdsky', label: 'HDSky', domain: 'hdsky.me' },
  { value: 'ourbits', label: 'OurBits', domain: 'ourbits.club' },
  { value: 'audiences', label: 'Audiences', domain: 'audiences.me' },
  { value: 'springsunday', label: 'Springsunday', domain: 'springsunday.net' },
  { value: 'hdhome', label: 'HDHome', domain: 'hdhome.org' },
  { value: 'hddolby', label: 'HDDolby', domain: 'hddolby.com' },
  { value: 'soulvoice', label: '聆音Club', domain: 'pt.soulvoice.club' },
  { value: 'hdwing', label: 'HDWinG', domain: 'hdwing.org' },
]

const ADAPTERS = ref<{ value: string; label: string; domain: string }[]>(ADAPTERS_FALLBACK)

function domainFor(adapter: string): string {
  return ADAPTERS.value.find((a) => a.value === adapter)?.domain ?? ''
}

async function loadAdapters() {
  try {
    const list = await api.get<AdapterInfo[]>('/sites/adapters')
    if (list.length > 0) {
      ADAPTERS.value = list.map((a) => ({ value: a.name, label: a.label, domain: a.domain }))
    }
  } catch { /* keep fallback */ }
}

const data = ref<Site[]>([])
const editing = ref<Site | null>(null)
const creating = ref(false)
const testingId = ref<number | null>(null)
const testingAll = ref(false)
const testResults = ref<Record<number, { status: string; detail: string }>>({})
const modalOpen = ref(false)
const formRef = ref<FormInstance>()

// ---- 用户数据汇总（跨站合计）----
const refreshingUserData = ref(false)

/**
 * 跨站合计：做种数 / 上传量 / 下载量 / 时魔。
 *
 * 数据源是**站点列表本身**（`GET /sites` 已带出每站的 user_data 快照）——
 * 不额外发请求。
 */
const userDataSummary = computed(() => {
  const withData = data.value.filter((s) => s.has_data)
  return {
    total_seed: withData.reduce((s, d) => s + d.seeding_count, 0),
    total_up: withData.reduce((s, d) => s + d.upload, 0),
    total_dn: withData.reduce((s, d) => s + d.download, 0),
    total_bonus: withData.reduce((s, d) => s + (d.hourly_bonus ?? 0), 0),
    capped: withData.filter((d) => d.hourly_bonus_capped === true).length,
    site_count: withData.length,
  }
})

const summaryTiles = computed(() => [
  { label: '做种中', value: String(userDataSummary.value.total_seed), hint: '跨站合计' },
  { label: '总上传', value: fmtBytes(userDataSummary.value.total_up), hint: '累计' },
  { label: '总下载', value: fmtBytes(userDataSummary.value.total_dn), hint: '累计' },
  {
    label: '时魔合计/小时',
    value: userDataSummary.value.total_bonus.toFixed(1),
    hint: userDataSummary.value.capped > 0
      ? `${userDataSummary.value.capped} 站已封顶`
      : '已封顶站越少越优先推',
  },
])

/**
 * 触发全局刷新（跑 `refresh_user_data` 作业重抓所有站点）。
 *
 * **POST 一返回后台作业就调度了**，数据要等作业跑完才更新——故延迟
 * 10s 再重新加载，期间按钮持续 loading（避免用户以为已结束却没动静）。
 */
let userDataTimer: ReturnType<typeof setTimeout> | null = null

async function refreshAllUserData() {
  if (refreshingUserData.value) return
  refreshingUserData.value = true
  try {
    await api.post('/scheduler/refresh_user_data/run')
    message.success('已触发全局刷新，正在重新加载…')
    if (userDataTimer) clearTimeout(userDataTimer)
    userDataTimer = setTimeout(async () => {
      try {
        await load()
      } finally {
        refreshingUserData.value = false
      }
    }, 10000)
  } catch (exc) {
    message.error(exc instanceof Error ? exc.message : '触发失败')
    refreshingUserData.value = false
  }
}

onUnmounted(() => {
  if (userDataTimer) {
    clearTimeout(userDataTimer)
    userDataTimer = null
  }
})

const formState = reactive<Record<string, unknown>>({})

async function load() {
  await Promise.all([loadSites(), loadAdapters()])
}

async function loadSites() {
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
  try {
    await api.post('/sites/delete', { id })
    message.success('已删除')
    load()
  } catch (err) {
    message.error(`删除失败：${errMsg(err)}`)
  }
}

async function onSave() {
  try {
    // 前端先校验（name 必填 + 合法字符），再交后端 422 兜底
    await formRef.value?.validate()
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
    const r = await api.post<{ reachable: boolean; logged_in: boolean; detail: string }>(
      `/sites/${s.id}/test`,
    )
    testResults.value[s.id] = {
      status: r.reachable ? 'ok' : 'error',
      detail: r.detail,
    }
  } catch (exc) {
    message.error(exc instanceof Error ? exc.message : '测连失败')
  } finally {
    testingId.value = null
  }
}

async function onTestAll() {
  if (testingAll.value) return
  testingAll.value = true
  // 清旧结果（让圆点在测连过程中复位）
  testResults.value = {}
  try {
    const r = await api.post<SiteTestAllResult>('/sites/test-all')
    for (const item of r.results) {
      testResults.value[item.site_id] = {
        status: item.reachable ? 'ok' : 'error',
        detail: item.detail,
      }
    }
    const { total, reachable, failed } = r.summary
    if (failed > 0) {
      message.warning(`共 ${total} 站，可达 ${reachable}，失败 ${failed}`)
    } else {
      message.success(`共 ${total} 站全部可达`)
    }
  } catch (exc) {
    message.error(exc instanceof Error ? exc.message : '全部测连失败')
  } finally {
    testingAll.value = false
  }
}

// ---- 拉取测试（feed-test）----
const feedTestId = ref<number | null>(null)
const feedTestModalOpen = ref(false)
const feedTestResult = ref<FeedTestResult | null>(null)

async function onFeedTest(s: Site) {
  feedTestId.value = s.id
  feedTestResult.value = null
  feedTestModalOpen.value = true
  try {
    feedTestResult.value = await api.get<FeedTestResult>(`/sites/${s.id}/feed-test`)
  } catch (exc) {
    feedTestResult.value = {
      ok: false,
      status: 'request_error',
      item_count: 0,
      duration_ms: 0,
      sample_titles: [],
      error: exc instanceof Error ? exc.message : '请求失败',
    }
  } finally {
    feedTestId.value = null
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
  { title: '上传/下载', key: 'traffic', width: 170 },
  { title: '做种', key: 'torrents', width: 80 },
  { title: '时魔/小时', key: 'bonus', width: 150 },
  { title: '启用', key: 'enabled', width: 80 },
  { title: '', key: 'action', width: 280 },
]
</script>

<template>
  <div>
    <PageHeader
      eyebrow="站点 · SITES"
      title="站点适配器"
      sub="凭据脱敏展示 · 支持 9 类私有种子站点"
    >
      <template #actions>
        <a-space>
          <a-button :loading="refreshingUserData" @click="refreshAllUserData">
            刷新用户数据
          </a-button>
          <a-button :loading="testingAll" @click="onTestAll">全部测连</a-button>
          <a-button type="primary" @click="onCreate">新增站点</a-button>
        </a-space>
      </template>
    </PageHeader>

    <hr class="divider-hairline" style="margin: 0" />

    <section class="section">
      <div class="label-caps" style="margin-bottom: var(--sp-3)">汇总 · SUMMARY</div>
      <div class="summary-grid">
        <div v-for="t in summaryTiles" :key="t.label" class="stat-tile">
          <div class="stat-tile__label">{{ t.label }}</div>
          <div class="stat-tile__value">{{ t.value }}</div>
          <div class="stat-tile__hint">{{ t.hint }}</div>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="panel">
      <DataTable
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
              <a-tag v-if="record.rss_url_masked" color="cyan">HTML</a-tag>
              <a-tag v-if="record.has_apikey" color="purple">API key</a-tag>
              <a-tag v-if="record.has_cookies" color="cyan">Cookies</a-tag>
              <a-tag v-if="!record.rss_url_masked && !record.has_apikey && !record.has_cookies">—</a-tag>
            </a-space>
          </template>
          <template v-else-if="column.key === 'traffic'">
            <span v-if="record.has_data" class="mono">
              {{ fmtBytes(record.upload) }} / {{ fmtBytes(record.download) }}
            </span>
            <span v-else class="muted">—</span>
          </template>
          <template v-else-if="column.key === 'torrents'">
            <span v-if="record.has_data" class="mono">
              {{ record.seeding_count }}
            </span>
            <span v-else class="muted">—</span>
          </template>
          <template v-else-if="column.key === 'bonus'">
            <a-tooltip v-if="record.has_data" :title="bonusTooltip(record)">
              <a-space :size="4">
                <span class="mono">{{ fmtBonus(record.hourly_bonus, record.hourly_bonus_unit) }}</span>
                <a-tag v-if="record.hourly_bonus_capped === true" color="orange">已封顶</a-tag>
              </a-space>
            </a-tooltip>
            <span v-else class="muted">—</span>
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
              <a-tooltip v-if="testResults[record.id]" :title="`${testResults[record.id].status} · ${testResults[record.id].detail}`">
                <span
                  class="test-result-dot"
                  :class="`test-result-dot--${testResults[record.id].status}`"
                />
              </a-tooltip>
              <a-button
                size="small"
                type="link"
                :loading="feedTestId === record.id"
                @click="onFeedTest(record)"
              >
                拉取测试
              </a-button>
              <a-button size="small" type="link" @click="onEdit(record)">编辑</a-button>
              <a-popconfirm
                title="确认删除该站点？"
                description="该站点的拉取索引、用户数据快照与推送队列记录将一并删除，不可恢复。"
                ok-text="删除"
                cancel-text="取消"
                ok-type="danger"
                @confirm="onDelete(record.id)"
              >
                <a-button size="small" type="link" danger>删除</a-button>
              </a-popconfirm>
            </a-space>
          </template>
        </template>
      </DataTable>
      </div>
    </section>

    <a-modal
      :open="feedTestModalOpen"
      title="拉取测试"
      :footer="null"
      @cancel="feedTestModalOpen = false"
    >
      <a-spin :spinning="feedTestResult === null">
        <template v-if="feedTestResult">
          <a-descriptions :column="1" bordered size="small">
            <a-descriptions-item label="结果">
              <a-tag :color="feedTestResult.ok ? 'green' : 'red'">
                {{ feedTestResult.ok ? '成功' : '失败' }}
              </a-tag>
              <span class="mono">{{ feedTestResult.status }}</span>
            </a-descriptions-item>
            <a-descriptions-item label="拉取条数">{{ feedTestResult.item_count }}</a-descriptions-item>
            <a-descriptions-item label="耗时">{{ feedTestResult.duration_ms }}ms</a-descriptions-item>
            <a-descriptions-item v-if="feedTestResult.error" label="错误">
              <span style="color: var(--accent-red)">{{ feedTestResult.error }}</span>
            </a-descriptions-item>
          </a-descriptions>
          <template v-if="feedTestResult.sample_titles.length">
            <div class="label-caps" style="margin: var(--sp-3) 0 var(--sp-2)">样本标题（前 5 条）</div>
            <ul class="feed-test-samples">
              <li v-for="(t, i) in feedTestResult.sample_titles" :key="i">{{ t }}</li>
            </ul>
          </template>
        </template>
      </a-spin>
    </a-modal>

    <a-modal
      :open="modalOpen"
      :title="creating ? '新增站点' : `编辑站点 · ${editing?.name || ''}`"
      :width="640"
      @cancel="() => { modalOpen = false; editing = null; creating = false }"
      @ok="onSave"
    >
      <a-form ref="formRef" :model="formState" layout="vertical">
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

        <a-form-item label="HTML URL（含 passkey，留空=不修改）" name="rss_url">
          <a-input-password
            v-model:value="formState.rss_url"
            placeholder="https://example.com/torrents.php?inclbookmarked=0&..."
          />
        </a-form-item>
        <a-form-item label="API Key（m-team 等，留空=不修改）" name="apikey">
          <a-input-password
            v-model:value="formState.apikey"
            :placeholder="editing && editing.has_apikey ? '已配置（留空保留）' : '在 m-team 控制台 → API 令牌获取'"
            autocomplete="new-password"
          />
        </a-form-item>
        <a-form-item label="Cookies（NexusPHP 站点，留空=不修改）" name="cookies">
          <a-input-password
            v-model:value="formState.cookies"
            :placeholder="editing && editing.has_cookies ? '已配置（留空保留）' : '浏览器 DevTools → Application → Cookies'"
            autocomplete="new-password"
          />
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

.test-result-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  cursor: help;
}
.test-result-dot--ok {
  background: var(--status-green, #22c55e);
  box-shadow: 0 0 6px rgba(34, 197, 94, 0.4);
}
.test-result-dot--error {
  background: var(--status-red, #ef4444);
  box-shadow: 0 0 6px rgba(239, 68, 68, 0.4);
}

.feed-test-samples {
  margin: 0;
  padding-left: 1.2em;
  font-size: var(--text-xs);
  color: var(--text-secondary);
}
.feed-test-samples li {
  margin: 2px 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 汇总卡网格（页面结构与设计 token 已在 global.css 统一定义） */
.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: var(--sp-3);
}
</style>
