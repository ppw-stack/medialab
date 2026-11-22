<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue';
import { api, errMsg } from '../api';
import type { FeedTestResult, Site, SiteRuleInfo, SiteTestAllResult } from '../types';
import { formatBytes as fmtBytes } from '../utils/format';
import { message } from 'ant-design-vue';
import type { FormInstance } from 'ant-design-vue/es/form';
import DataTable from '../components/DataTable.vue';
import PageHeader from '../components/PageHeader.vue';

//: 规则包站点目录（通用适配器加站时选择；后端 /sites/rules）
const RULES = ref<SiteRuleInfo[]>([]);
const selectedRule = ref<string | null>(null);

function hostOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url.replace(/^https?:\/\//, '').replace(/\/$/, '');
  }
}

const ruleOptions = computed(() =>
  RULES.value.map((r) => ({
    value: r.id,
    label: `${r.name} · ${hostOf(r.domain)}${r.userdata_schema ? '' : '（仅搜索）'}`,
  })),
);

async function loadRules() {
  try {
    RULES.value = await api.get<SiteRuleInfo[]>('/sites/rules');
  } catch {
    /* keep empty */
  }
}

/**
 * 选中规则：自动填名称与域名。
 *
 * 名称用规则 ID（后端要求 `^[a-zA-Z0-9_-]+$`；规则名是中文，不能直接用）。
 */
function onRuleChange(ruleId: string) {
  selectedRule.value = ruleId;
  const rule = RULES.value.find((r) => r.id === ruleId);
  if (!rule) return;
  formState.name = rule.id;
  formState.domain = hostOf(rule.domain);
}

const data = ref<Site[]>([]);
const editing = ref<Site | null>(null);
const creating = ref(false);
const testingId = ref<number | null>(null);
const testingAll = ref(false);
const testResults = ref<Record<number, { status: string; detail: string }>>({});
const modalOpen = ref(false);
const formRef = ref<FormInstance>();

const formState = reactive<Record<string, unknown>>({});

async function load() {
  await Promise.all([loadSites(), loadRules()]);
}

async function loadSites() {
  try {
    data.value = await api.get<Site[]>('/sites');
  } catch {
    /* ignore */
  }
}

onMounted(() => {
  void load();
  // 打开页面就做一次全局测连（顺带刷新可达站点的用户数据）。
  // 这是 `refresh_user_data` 周期作业（已于 2026-11-22 移除）的替代：
  // 用户数据只在「有人看」时刷新，不做无人值守的周期请求。
  void onTestAll({ auto: true });
});

function onCreate() {
  editing.value = null;
  creating.value = true;
  modalOpen.value = true;
  selectedRule.value = null;
  Object.assign(formState, {
    adapter: 'generic',
    enabled: true,
    name: '',
    domain: '',
    rss_url: '',
    apikey: '',
    cookies: '',
  });
}

function onEdit(s: Site) {
  editing.value = s;
  creating.value = false;
  modalOpen.value = true;
  Object.assign(formState, {
    ...s,
    rss_url: '',
    apikey: '',
    cookies: '',
  });
}

async function onDelete(id: number) {
  try {
    await api.post('/sites/delete', { id });
    message.success('已删除');
    void load();
  } catch (err) {
    message.error(`删除失败：${errMsg(err)}`);
  }
}

async function onSave() {
  try {
    // 前端先校验（name 必填 + 合法字符），再交后端 422 兜底
    await formRef.value?.validate();
    if (creating.value) {
      await api.post('/sites', formState);
      message.success('已创建');
    } else if (editing.value) {
      await api.post('/sites/update', { id: editing.value.id, ...formState });
      message.success('已更新');
    }
    creating.value = false;
    editing.value = null;
    modalOpen.value = false;
    void load();
  } catch (exc) {
    message.error(exc instanceof Error ? exc.message : '保存失败');
  }
}

async function onTestRow(s: Site) {
  if (testingId.value !== null) return;
  testingId.value = s.id;
  // Reflect.deleteProperty 不触发 no-dynamic-delete（删动态键）
  Reflect.deleteProperty(testResults.value, s.id);
  try {
    const r = await api.post<{ reachable: boolean; logged_in: boolean; detail: string }>(
      `/sites/${String(s.id)}/test`,
    );
    let detail = r.detail;
    // 测连通过后顺手刷新用户数据（合并为一个动作，失败不影响测连结果）
    if (r.reachable && r.logged_in) {
      try {
        const ud = await api.post<{ ok: boolean }>(`/sites/${String(s.id)}/user-data/refresh`);
        detail = `${detail} · 用户数据刷新${ud.ok ? '成功' : '失败'}`;
        if (ud.ok) await loadSites();
      } catch (exc) {
        detail = `${detail} · 用户数据刷新失败：${errMsg(exc)}`;
      }
    }
    testResults.value[s.id] = {
      status: testStatus(r.reachable, r.logged_in),
      detail,
    };
  } catch (exc) {
    message.error(exc instanceof Error ? exc.message : '测连失败');
  } finally {
    testingId.value = null;
  }
}

/**
 * 测连结果三态。
 *
 * ⚠️ **不能只看 reachable**——那是「网络可达」，不是「一切正常」：
 * 凭据过期时站点仍可达（302 → 登录页，200），但**拉不到任何数据**
 * （2026-11-08 prod：ourbits 凭据过期 10 天，面板测连一直显示绿点，
 * 直到用户数据作业报错才暴露）。故：
 *
 * - ``ok``    = 可达且已登录
 * - ``warn``  = 可达但未登录（凭据失效 / 没配 cookie）——**黄色醒目**
 * - ``error`` = 不可达
 */
function testStatus(reachable: boolean, loggedIn: boolean): string {
  if (!reachable) return 'error';
  return loggedIn ? 'ok' : 'warn';
}

/**
 * 自动测连的冷却时间（毫秒）。
 *
 * 「打开页面自动测一次」按字面是每次进入都跑，但站点对短时间重复请求敏感
 * （后端 `_TEST_ALL_CONCURRENCY` 也是为此压到 3），来回切页面会打出成倍
 * 请求。故自动模式 5 分钟内只跑一次；**手动按钮不受限制**（用户显式点了
 * 就是要测）。
 */
const AUTO_TEST_COOLDOWN_MS = 5 * 60_000;
const AUTO_TEST_KEY = 'sites:auto-test-at';

async function onTestAll(opts: { auto?: boolean } = {}) {
  if (testingAll.value) return;
  const auto = !!opts.auto;
  if (auto) {
    const last = Number(sessionStorage.getItem(AUTO_TEST_KEY) ?? 0);
    if (Date.now() - last < AUTO_TEST_COOLDOWN_MS) return;
  }
  // 先落时间戳：并发/快速重入时后到的那个直接跳过（避免双发）
  if (auto) sessionStorage.setItem(AUTO_TEST_KEY, String(Date.now()));
  testingAll.value = true;
  // 清旧结果（让圆点在测连过程中复位）
  testResults.value = {};
  try {
    const r = await api.post<SiteTestAllResult>('/sites/test-all');
    for (const item of r.results) {
      testResults.value[item.site_id] = {
        status: testStatus(item.reachable, item.logged_in),
        detail: item.detail,
      };
    }

    // 合并用户数据：只刷新「可达且已登录」的站（测连不通过的站不必再打一次）
    const passing = r.results.filter((x) => x.reachable && x.logged_in);
    let refreshed = 0;
    let refreshFailed = 0;
    for (const item of passing) {
      try {
        const ud = await api.post<{ ok: boolean }>(
          `/sites/${String(item.site_id)}/user-data/refresh`,
        );
        if (ud.ok) refreshed += 1;
        else refreshFailed += 1;
      } catch {
        refreshFailed += 1;
      }
    }
    if (passing.length) await loadSites();
    const { total, reachable, failed } = r.summary;
    // ⚠️ summary.unauthorized = 可达但**未登录**（凭据失效）——单独提示，
    // 别混进「可达」（那会让失效的站看起来一切正常）
    const unauthorized = r.summary.unauthorized ?? 0;
    if (failed > 0 || unauthorized > 0) {
      const parts = [`共 ${String(total)} 站`, `可达 ${String(reachable)}`];
      if (failed > 0) parts.push(`失败 ${String(failed)}`);
      if (unauthorized > 0) parts.push(`未登录 ${String(unauthorized)}（凭据可能已失效）`);
      if (passing.length)
        parts.push(
          `用户数据 ${String(refreshed)} 成功${refreshFailed ? ` / ${String(refreshFailed)} 失败` : ''}`,
        );
      message.warning(parts.join('，'));
    } else if (!auto) {
      // 自动模式**静默成功**：每次打开页面弹一条「全部正常」是噪音
      message.success(
        `共 ${String(total)} 站全部可达且已登录` +
          (passing.length ? `，用户数据已刷新 ${String(refreshed)} 站` : ''),
      );
    }
  } catch (exc) {
    message.error(exc instanceof Error ? exc.message : '全部测连失败');
  } finally {
    testingAll.value = false;
  }
}

// ---- 拉取测试（feed-test）----
const feedTestId = ref<number | null>(null);
const feedTestModalOpen = ref(false);
const feedTestResult = ref<FeedTestResult | null>(null);

const feedColumns = [
  { title: '标题', dataIndex: 'title', key: 'title', ellipsis: true },
  { title: '大小', key: 'size', width: 100 },
  { title: '做种/下载', key: 'peers', width: 110 },
  { title: '免费倍率', key: 'factor', width: 100 },
  { title: '发布时间', key: 'published_at', width: 170 },
];

/** 免费倍率展示：0=免费、0.5=50%、1=正常。 */
function factorLabel(v: number | null): string {
  if (v === null) return '—';
  if (v === 0) return '免费';
  if (v < 1) return `${String(Math.round(v * 100))}%`;
  return `×${String(v)}`;
}

async function onFeedTest(s: Site) {
  feedTestId.value = s.id;
  feedTestResult.value = null;
  feedTestModalOpen.value = true;
  try {
    feedTestResult.value = await api.get<FeedTestResult>(`/sites/${String(s.id)}/feed-test`);
  } catch (exc) {
    feedTestResult.value = {
      ok: false,
      status: 'request_error',
      item_count: 0,
      duration_ms: 0,
      sample_titles: [],
      items: [],
      error: exc instanceof Error ? exc.message : '请求失败',
    };
  } finally {
    feedTestId.value = null;
  }
}

async function onToggleEnabled(s: Site, next: boolean) {
  // 乐观更新：先改 UI，失败回滚
  const prev = s.enabled;
  s.enabled = next;
  try {
    await api.post('/sites/update', { id: s.id, enabled: next });
  } catch (exc) {
    s.enabled = prev;
    message.error(exc instanceof Error ? exc.message : '切换失败');
  }
}

const columns = [
  { title: '名称', key: 'name' },
  { title: '域名', key: 'domain' },
  { title: '凭据', key: 'credentials' },
  { title: '上传/下载', key: 'traffic', width: 170 },
  { title: '做种', key: 'torrents', width: 80 },
  { title: '启用', key: 'enabled', width: 80 },
  { title: '', key: 'action', width: 280 },
];
</script>

<template>
  <div>
    <PageHeader eyebrow="站点 · SITES" title="站点适配器" sub="凭据脱敏展示 · 规则包驱动（184 站）">
      <template #actions>
        <a-space>
          <a-button :loading="testingAll" @click="onTestAll()">全部测连</a-button>
          <a-button type="primary" @click="onCreate">新增站点</a-button>
        </a-space>
      </template>
    </PageHeader>

    <hr class="divider-hairline" style="margin: 0" />

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
            <template v-else-if="column.key === 'domain'">
              <span class="mono code">{{ record.domain || '—' }}</span>
            </template>
            <template v-else-if="column.key === 'credentials'">
              <a-space :size="4">
                <a-tag v-if="record.rss_url_masked" color="cyan">HTML</a-tag>
                <a-tag v-if="record.has_apikey" color="purple">API key</a-tag>
                <a-tag v-if="record.has_cookies" color="cyan">Cookies</a-tag>
                <a-tag v-if="!record.rss_url_masked && !record.has_apikey && !record.has_cookies"
                  >—</a-tag
                >
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
                  测连/刷新
                </a-button>
                <a-tooltip
                  v-if="testResults[record.id]"
                  :title="`${testResults[record.id]?.status ?? ''} · ${testResults[record.id]?.detail ?? ''}`"
                >
                  <span
                    class="test-result-dot"
                    :class="`test-result-dot--${testResults[record.id]?.status ?? ''}`"
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
            <a-descriptions-item label="拉取条数">{{
              feedTestResult.item_count
            }}</a-descriptions-item>
            <a-descriptions-item label="耗时"
              >{{ feedTestResult.duration_ms }}ms</a-descriptions-item
            >
            <a-descriptions-item v-if="feedTestResult.error" label="错误">
              <span style="color: var(--accent-red)">{{ feedTestResult.error }}</span>
            </a-descriptions-item>
          </a-descriptions>
          <template v-if="feedTestResult.items.length">
            <div class="label-caps" style="margin: var(--sp-3) 0 var(--sp-2)">
              解析结果（共 {{ feedTestResult.item_count }} 条，显示前
              {{ feedTestResult.items.length }} 条）
            </div>
            <a-table
              :data-source="feedTestResult.items"
              :columns="feedColumns"
              row-key="title"
              size="small"
              :pagination="{ pageSize: 10, size: 'small' }"
              :scroll="{ x: 820, y: 320 }"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'size'">{{ fmtBytes(record.size) }}</template>
                <template v-else-if="column.key === 'peers'"
                  >{{ record.seeders }} / {{ record.peers }}</template
                >
                <template v-else-if="column.key === 'factor'">{{
                  factorLabel(record.download_factor)
                }}</template>
                <template v-else-if="column.key === 'published_at'">{{
                  record.published_at || '—'
                }}</template>
              </template>
            </a-table>
          </template>
        </template>
      </a-spin>
    </a-modal>

    <a-modal
      :open="modalOpen"
      :title="creating ? '新增站点' : `编辑站点 · ${editing?.name || ''}`"
      :width="640"
      @cancel="
        () => {
          modalOpen = false;
          editing = null;
          creating = false;
        }
      "
      @ok="onSave"
    >
      <a-form ref="formRef" :model="formState" layout="vertical">
        <a-form-item v-if="creating" label="站点规则（规则包 184 站）">
          <a-select
            :value="selectedRule ?? undefined"
            show-search
            option-filter-prop="label"
            :options="ruleOptions"
            placeholder="输入站点名或域名搜索"
            @change="onRuleChange"
          />
          <div style="font-size: 12px; opacity: 0.65; margin-top: 4px">
            选择后自动填入站点标识与域名；Cookie 仍需手动填写。
          </div>
        </a-form-item>
        <a-space style="width: 100%" size="middle">
          <a-form-item
            label="名称"
            name="name"
            :rules="[
              { required: true, pattern: /^[a-zA-Z0-9_-]+$/, message: '字母/数字/下划线/连字符' },
            ]"
            style="flex: 1"
          >
            <a-input v-model:value="formState.name" />
          </a-form-item>
          <a-form-item label="域名" name="domain" style="flex: 1">
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
            :placeholder="
              editing && editing.has_apikey
                ? '已配置（留空保留）'
                : '在 m-team 控制台 → API 令牌获取'
            "
            autocomplete="new-password"
          />
        </a-form-item>
        <a-form-item label="Cookies（NexusPHP 站点，留空=不修改）" name="cookies">
          <a-input-password
            v-model:value="formState.cookies"
            :placeholder="
              editing && editing.has_cookies
                ? '已配置（留空保留）'
                : '浏览器 DevTools → Application → Cookies'
            "
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
/* 可达但未登录（凭据失效）——黄色，醒目但不叫「失败」 */
.test-result-dot--warn {
  background: var(--status-amber, #f59e0b);
  box-shadow: 0 0 6px rgba(245, 158, 11, 0.4);
}
.test-result-dot--error {
  background: var(--status-red, #ef4444);
  box-shadow: 0 0 6px rgba(239, 68, 68, 0.4);
}
</style>
