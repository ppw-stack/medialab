<template>
  <div class="page">
    <PageHeader
      eyebrow="媒体库 · MEDIA"
      title="迁移（光鸭 → 光鸭盘）"
      sub="边扫边迁——爬到合格文件就提交光鸭云下载"
      compact
    >
      <template #actions>
        <a-tag v-if="sourceDriver" color="blue">源：{{ sourceDriver.name }}</a-tag>
        <a-tag v-if="targetDriver" color="cyan">目标：{{ targetDriver.name }}</a-tag>
      </template>
    </PageHeader>

    <a-card size="small">
      <a-alert type="info" show-icon :closable="false" class="tip">
        <template #message>
          边扫边迁：不预先遍历整棵树，爬到合格文件就提交光鸭云下载。
          提交前会先查目标目录，<b>同名同大小直接跳过</b>，可以放心重复执行。
          光鸭云下载失败即跳过，不重试、不阻塞队列。
        </template>
      </a-alert>

      <a-alert type="warning" show-icon :closable="false" class="tip">
        <template #message>
          <b>云添加额度有限</b>（每天每账号，失败也扣）。 云下载按 URL 末段落名（<span class="mono"
            >download</span
          >），落盘后会自动<b>改名回原名</b>；
          <b>恒串行</b>提交（同目录并发会撞名）。撞到额度上限会<b>暂停</b>作业，次日重跑即可续传。
        </template>
      </a-alert>

      <a-alert v-if="form.deleteSource" type="error" show-icon :closable="false" class="tip">
        <template #message>
          <b>已开启「搬完清理源」</b>：每个文件在<b>目标侧确认落盘同名同大小</b>后，
          源端同名文件会被删除（进回收站，可恢复）。中途失败/额度耗尽时
          <b>已迁成功的会被删、未迁的保留</b>——重跑可续。
          <br />
          同名不同大小按<b>「保留大的」</b>处理：目标更大则跳过并清掉源的冗余副本；
          源更大则替换目标侧的小文件后重传。
        </template>
      </a-alert>

      <a-row :gutter="12">
        <a-col :span="12">
          <a-form layout="inline" class="driver-row">
            <a-form-item label="源存储源">
              <a-select
                v-model:value="sourceDriverId"
                :options="sourceDriverOptions"
                style="width: 220px"
                @change="onSourceDriverChange"
              />
            </a-form-item>
          </a-form>
        </a-col>
        <a-col :span="12">
          <a-form layout="inline" class="driver-row">
            <a-form-item label="目标存储源">
              <a-select
                v-model:value="targetDriverId"
                :options="targetDriverOptions"
                style="width: 220px"
                @change="onTargetDriverChange"
              />
            </a-form-item>
          </a-form>
        </a-col>
      </a-row>

      <a-row :gutter="12">
        <a-col :span="12">
          <DirBrowser
            :title="sourceBrowserTitle"
            :stack="sourceStack"
            :dirs="sourceDirs"
            :loading="loadingSource"
            @open="openSource"
            @go="goSource"
          />
        </a-col>
        <a-col :span="12">
          <DirBrowser
            title="② 光鸭目标目录"
            :stack="targetStack"
            :dirs="targetDirs"
            :loading="loadingTarget"
            @open="openTarget"
            @go="goTarget"
          />
        </a-col>
      </a-row>

      <a-divider style="margin: 12px 0" />

      <a-form layout="inline" :model="form">
        <a-form-item label="最小体积 (GB)">
          <a-input-number
            v-model:value="form.minSizeGb"
            :min="0"
            :step="0.5"
            style="width: 100px"
          />
        </a-form-item>
        <a-form-item label="最大体积 (GB)">
          <a-input-number v-model:value="form.maxSizeGb" :min="0" :step="1" style="width: 100px" />
          <span class="hint">0 = 不限</span>
        </a-form-item>
        <!--
          没有「并发数」：迁移**恒串行**（2026-11-15 决策）——并发会让云下载
          落盘名撞车（download(1)），且更快撞上云添加额度墙。
        -->
        <a-form-item label="提交间隔">
          <a-input-number
            v-model:value="form.submitIntervalSec"
            :min="0"
            :max="600"
            :step="10"
            disabled
            style="width: 90px"
          />
          <span class="hint">秒，上限 5s（额度是瓶颈，不是下载频控）</span>
          <span class="hint">恒串行提交，不支持并发</span>
        </a-form-item>
        <a-form-item label="排除原盘">
          <a-switch v-model:checked="form.excludeDisc" />
          <span class="hint">iso / m2ts / BDMV</span>
        </a-form-item>
        <a-form-item label="搬完清理源">
          <a-switch v-model:checked="form.deleteSource" />
          <span class="hint">
            {{ true ? '只删已校验成功的；进回收站可恢复' : '仅光鸭源支持' }}
          </span>
        </a-form-item>
        <a-form-item>
          <a-button type="primary" :loading="starting" :disabled="!canRun" @click="start">
            开始迁移
          </a-button>
        </a-form-item>
      </a-form>

      <div v-if="sourceError || targetError" class="err">{{ sourceError || targetError }}</div>
    </a-card>

    <a-card v-if="job" size="small" class="mt" title="进度">
      <template #extra>
        <a-space>
          <a-tag :color="stateColor(job.state)">{{ stateLabel(job.state) }}</a-tag>
          <a-button
            v-if="job.state === 'running'"
            size="small"
            danger
            :loading="cancelling"
            @click="cancel"
          >
            取消
          </a-button>
        </a-space>
      </template>

      <div class="mono truncate current block-truncate" :title="job.current_path">
        {{ job.current_path || '（等待第一个文件）' }}
      </div>

      <a-descriptions :column="4" size="small" bordered class="stats">
        <a-descriptions-item label="已遍历目录">{{ job.dirs_walked }}</a-descriptions-item>
        <a-descriptions-item label="已发现文件">{{ job.discovered }}</a-descriptions-item>
        <a-descriptions-item label="在途任务">{{ job.in_flight }}</a-descriptions-item>
        <a-descriptions-item label="已迁移体积">
          {{ formatBytes(job.migrated_bytes) }}
        </a-descriptions-item>
        <a-descriptions-item label="已清理源">
          {{ job.deleted_source ?? 0 }}
        </a-descriptions-item>

        <a-descriptions-item label="成功">{{ job.success }}</a-descriptions-item>
        <a-descriptions-item label="已存在跳过">{{ job.skipped_existing }}</a-descriptions-item>
        <a-descriptions-item label="在途跳过">{{ job.skipped_inflight }}</a-descriptions-item>
        <a-descriptions-item label="冲突">{{ job.conflicts }}</a-descriptions-item>

        <a-descriptions-item label="失败">{{ job.failed }}</a-descriptions-item>
        <a-descriptions-item label="阈值过滤">{{ job.filtered_small }}</a-descriptions-item>
        <a-descriptions-item label="超上限过滤">{{ job.filtered_large }}</a-descriptions-item>
        <a-descriptions-item label="后缀/原盘过滤">
          {{ job.filtered_ext }}
        </a-descriptions-item>
      </a-descriptions>

      <a-alert
        v-if="job.legacy_inflight"
        type="warning"
        show-icon
        :closable="false"
        class="mt-sm"
        :message="`启动时检测到 ${job.legacy_inflight} 个上一轮遗留的在途任务，本轮跳过对应文件；光鸭上实际在跑 ${job.legacy_inflight + 1} 个（恒串行）`"
      />

      <a-alert
        v-if="job.error"
        type="error"
        show-icon
        :closable="false"
        class="mt-sm"
        :message="job.error"
      />

      <!-- 在途任务实时进度 -->
      <template v-if="inFlightList.length">
        <div class="section">在途任务（{{ inFlightList.length }}）</div>
        <div v-for="t in inFlightList" :key="t.id" class="inflight">
          <div class="inflight-head">
            <span class="inflight-name" :title="t.path">{{ t.path }}</span>
            <span class="muted">{{ formatBytes(t.size) }}</span>
          </div>
        </div>
      </template>

      <template v-if="job.failed_items.length">
        <div class="section">失败明细（{{ job.failed }})</div>
        <DataTable
          :data-source="job.failed_items"
          :columns="failedColumns"
          row-key="path"
          size="small"
          :pagination="false"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'size'"> {{ record.size_gb }} GB </template>
          </template>
        </DataTable>
      </template>

      <template v-if="job.conflict_items.length">
        <div class="section">同名不同大小（{{ job.conflicts }}）—— 未覆盖、未重传</div>
        <DataTable
          :data-source="job.conflict_items"
          :columns="conflictColumns"
          row-key="path"
          size="small"
          :pagination="false"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'local'">{{ record.local_gb }} GB</template>
            <template v-else-if="column.key === 'remote'">{{ record.remote_gb }} GB</template>
          </template>
        </DataTable>
      </template>

      <div class="section">
        日志
        <span class="muted">（每 2 秒自动刷新）</span>
      </div>
      <div ref="logBox" class="logs">
        <div v-for="(line, i) in job.logs" :key="i" class="log-line">
          <span class="log-time">{{ logTime(line) }}</span>
          <span class="log-text">{{ logText(line) }}</span>
        </div>
      </div>
    </a-card>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import { message } from 'ant-design-vue';
import DirBrowser from '../../components/DirBrowser.vue';
import PageHeader from '../../components/PageHeader.vue';
import { errMsg, mediaApi as api } from '../../api';
import type { DirEntry, Driver, MigrationJob, MigrationRunInput } from '../../types';
import { formatBytes } from '../../utils/format';
import DataTable from '../../components/DataTable.vue';

const drivers = ref<Driver[]>([]);
const sourceDriverId = ref<number | undefined>(undefined);
const targetDriverId = ref<number | undefined>(undefined);

const sourceStack = ref<DirEntry[]>([]);
const targetStack = ref<DirEntry[]>([]);
const sourceDirs = ref<DirEntry[]>([]);
const targetDirs = ref<DirEntry[]>([]);
const loadingSource = ref(false);
const loadingTarget = ref(false);
const sourceError = ref('');
const targetError = ref('');

const form = reactive({
  minSizeGb: 0,
  maxSizeGb: 0,
  submitIntervalSec: 60,
  excludeDisc: true,
  deleteSource: false,
});

const job = ref<MigrationJob | null>(null);
const starting = ref(false);
const cancelling = ref(false);
const logBox = ref<HTMLElement | null>(null);

let timer: number | undefined;

const failedColumns = [
  { title: '文件', dataIndex: 'name', key: 'name', ellipsis: true },
  { title: '大小', key: 'size', width: 100 },
  { title: '原因', dataIndex: 'reason', key: 'reason', ellipsis: true },
];

const conflictColumns = [
  { title: '文件', dataIndex: 'name', key: 'name', ellipsis: true },
  { title: '源', key: 'local', width: 110 },
  { title: '光鸭', key: 'remote', width: 110 },
];

/** 源只能是光鸭账号（光鸭 → 光鸭）。 */
const sourceDriverOptions = computed(() =>
  drivers.value.filter((d) => d.type === 'guangya').map((d) => ({ value: d.id, label: d.name })),
);

/** 目标只能是光鸭盘。 */
const targetDriverOptions = computed(() =>
  drivers.value.filter((d) => d.type === 'guangya').map((d) => ({ value: d.id, label: d.name })),
);

const sourceDriver = computed(
  () => drivers.value.find((d) => d.id === sourceDriverId.value) ?? null,
);
const targetDriver = computed(
  () => drivers.value.find((d) => d.id === targetDriverId.value) ?? null,
);

const currentSource = computed(() => sourceStack.value[sourceStack.value.length - 1]);
const currentTarget = computed(() => targetStack.value[targetStack.value.length - 1]);

const sourceBrowserTitle = '① 光鸭源目录';

/** 在途任务（提交顺序） */
const inFlightList = computed(() => {
  const items = job.value?.in_flight_items ?? {};
  return Object.entries(items).map(([id, v]) => ({
    id,
    path: v.path,
    name: v.name,
    size: v.size,
  }));
});

const canRun = computed(
  () =>
    !!sourceDriver.value &&
    !!targetDriver.value &&
    sourceDriverId.value !== targetDriverId.value &&
    !!currentSource.value &&
    currentTarget.value !== undefined &&
    job.value?.state !== 'running',
);

onMounted(async () => {
  try {
    drivers.value = await api.get<Driver[]>('/drivers');
  } catch (e) {
    message.error(errMsg(e, '加载存储源失败'));
    return;
  }

  const firstGuangya = drivers.value.find((d) => d.type === 'guangya');
  sourceDriverId.value = firstGuangya?.id;
  targetDriverId.value = firstGuangya?.id;

  await resetSourceStack();
  if (sourceDriver.value) {
    await loadSource();
  }
  if (targetDriver.value) {
    targetStack.value = [{ id: '', name: '光鸭根目录' }];
    await loadTarget();
  }

  // 找回最近一次任务（刷新页面后还能看到进度）
  try {
    const jobs = await api.get<MigrationJob[]>('/migrations/jobs');
    if (jobs.length) {
      job.value = jobs[0] as MigrationJob;
      if (job.value.state === 'running') startPolling();
    }
  } catch {
    // 没有任务不算错
  }
});

/** 源目录栈复位——光鸭账号根 fileId 是空串，目录由用户逐层选。 */
async function resetSourceStack(): Promise<void> {
  const driver = sourceDriver.value;
  if (!driver) {
    sourceStack.value = [];
    return;
  }
  sourceStack.value = [{ id: '', name: '光鸭根目录' }];
}

/** 换源存储源——目录栈复位并重新加载。 */
async function onSourceDriverChange(): Promise<void> {
  await resetSourceStack();
  await loadSource();
}

/** 换目标存储源——目标目录栈复位并重新加载。 */
async function onTargetDriverChange(): Promise<void> {
  targetStack.value = [{ id: '', name: '光鸭根目录' }];
  await loadTarget();
}

onUnmounted(stopPolling);

watch(
  () => job.value?.logs.length,
  async () => {
    await nextTick();
    if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight;
  },
);

// -- 目录浏览 --

async function loadSource(): Promise<void> {
  if (!sourceDriver.value || !currentSource.value) return;
  loadingSource.value = true;
  sourceError.value = '';
  try {
    const res = await api.get<{ dirs: DirEntry[] }>(
      `/migrations/sources?driver_id=${sourceDriver.value.id}&parent_id=${encodeURIComponent(currentSource.value.id)}`,
    );
    sourceDirs.value = res.dirs;
  } catch (e) {
    sourceError.value = errMsg(e, '读取源目录失败');
    sourceDirs.value = [];
  } finally {
    loadingSource.value = false;
  }
}

async function loadTarget(): Promise<void> {
  if (!targetDriver.value || !currentTarget.value) return;
  loadingTarget.value = true;
  targetError.value = '';
  try {
    const res = await api.get<{ dirs: DirEntry[] }>(
      `/migrations/targets?driver_id=${targetDriver.value.id}&parent_id=${encodeURIComponent(currentTarget.value.id)}`,
    );
    targetDirs.value = res.dirs;
  } catch (e) {
    targetError.value = errMsg(e, '读取光鸭目录失败');
    targetDirs.value = [];
  } finally {
    loadingTarget.value = false;
  }
}

function openSource(dir: DirEntry): void {
  sourceStack.value = [...sourceStack.value, dir];
  void loadSource();
}

function goSource(index: number): void {
  sourceStack.value = sourceStack.value.slice(0, index + 1);
  void loadSource();
}

function openTarget(dir: DirEntry): void {
  targetStack.value = [...targetStack.value, dir];
  void loadTarget();
}

function goTarget(index: number): void {
  targetStack.value = targetStack.value.slice(0, index + 1);
  void loadTarget();
}

// -- 迁移 --

async function start(): Promise<void> {
  if (!sourceDriver.value || !targetDriver.value || !currentSource.value || !currentTarget.value) {
    return;
  }
  starting.value = true;
  try {
    const payload: MigrationRunInput = {
      source_driver_id: sourceDriver.value.id,
      source_cid: currentSource.value.id,
      source_path: sourceStack.value.map((c) => c.name).join('/'),
      target_driver_id: targetDriver.value.id,
      target_file_id: currentTarget.value.id,
      target_path: targetStack.value.map((c) => c.name).join('/'),
      min_size_mb: Math.round((form.minSizeGb || 0) * 1024),
      max_size_mb: Math.round((form.maxSizeGb || 0) * 1024),
      submit_interval_sec: form.submitIntervalSec,
      exclude_disc: form.excludeDisc,
      delete_source: form.deleteSource,
    };
    const res = await api.post<{ job_id: string }>('/migrations/run', payload);
    job.value = await api.get<MigrationJob>(`/migrations/jobs/${res.job_id}`);
    startPolling();
  } catch (e) {
    message.error(errMsg(e, '启动迁移失败'));
  } finally {
    starting.value = false;
  }
}

async function cancel(): Promise<void> {
  if (!job.value) return;
  cancelling.value = true;
  try {
    await api.post(`/migrations/jobs/${job.value.id}/cancel`);
    message.info('已请求取消（已提交的离线任务留在光鸭继续跑）');
  } catch (e) {
    message.error(errMsg(e, '取消失败'));
  } finally {
    cancelling.value = false;
  }
}

function startPolling(): void {
  stopPolling();
  timer = window.setInterval(() => void tick(), 2000);
}

function stopPolling(): void {
  if (timer !== undefined) {
    window.clearInterval(timer);
    timer = undefined;
  }
}

async function tick(): Promise<void> {
  if (!job.value) return;
  try {
    job.value = await api.get<MigrationJob>(`/migrations/jobs/${job.value.id}`);
    if (job.value.state !== 'running') {
      stopPolling();
      if (job.value.state === 'success') {
        message.success(`迁移结束：成功 ${job.value.success} 个，失败 ${job.value.failed} 个`);
      } else if (job.value.state === 'paused') {
        message.warning(`迁移已暂停（云添加额度耗尽）：${job.value.error ?? ''}——次日重跑即可续传`);
      }
    }
  } catch (e) {
    message.error(errMsg(e, '查询迁移进度失败'));
    stopPolling();
  }
}

// -- 展示 --

function stateLabel(state: string): string {
  switch (state) {
    case 'running':
      return '进行中';
    case 'success':
      return '已完成';
    case 'failed':
      return '失败';
    case 'cancelled':
      return '已取消';
    case 'paused':
      return '已暂停（额度耗尽）';
    default:
      return state;
  }
}

function stateColor(state: string): string {
  switch (state) {
    case 'running':
      return 'processing';
    case 'success':
      return 'success';
    case 'failed':
      return 'error';
    case 'paused':
      return 'warning';
    default:
      return 'default';
  }
}

/** 拆出日志行首的时间戳（``[00:41:37]``），没有就返回空串。 */
function logTime(line: string): string {
  const m = line.match(/^\[(\d{2}:\d{2}:\d{2})\]\s*/);
  return m ? m[1] : '';
}

/** 去掉时间戳后的正文。 */
function logText(line: string): string {
  return line.replace(/^\[\d{2}:\d{2}:\d{2}\]\s*/, '');
}
</script>

<style scoped>
.tip {
  margin-bottom: 12px;
}

.hint {
  margin-left: 6px;
  color: #999;
  font-size: 12px;
}

.mt {
  margin-top: 12px;
}

/* 源/目标存储源选择行——与下面的目录浏览器之间留点间距 */
.driver-row {
  margin-bottom: 8px;
}

.mt-sm {
  margin-top: 8px;
}

.current {
  color: #666;
  margin-bottom: 8px;
}

/* .truncate 默认 inline-block——块级容器里要占满整行才能真正截断 */
.block-truncate {
  display: block;
}

.stats {
  margin-bottom: 8px;
}

.section {
  margin: 12px 0 6px;
  font-weight: 500;
  font-size: 13px;
}

.inflight {
  margin-bottom: 6px;
  padding: 6px 8px;
  background: #fafafa;
  border: 1px solid #f0f0f0;
  border-radius: 4px;
}

.inflight-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  font-size: 12px;
  margin-bottom: 2px;
}

/* 完整路径：不截断，在分隔符处换行 */
.inflight-name {
  flex: 1;
  min-width: 0;
  color: #555;
  overflow-wrap: anywhere;
  word-break: normal;
}

.logs {
  max-height: 320px;
  overflow: auto;
  background: #fafafa;
  border: 1px solid #f0f0f0;
  border-radius: 4px;
  padding: 6px 10px;
  font-size: 12px;
  line-height: 1.7;
}

/* 每条日志独立成行 */
.log-line {
  display: flex;
  gap: 8px;
  align-items: baseline;
  padding: 1px 0;
  /* 路径在分隔符处换行，而不是硬拆字符 */
  overflow-wrap: anywhere;
  word-break: normal;
}

/* 时间戳淡一点，便于区分事件与上下文 */
.log-time {
  flex: none;
  color: #aaa;
  font-family: 'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.log-text {
  color: #444;
  min-width: 0;
}

.err {
  margin-top: 8px;
  color: #cf1322;
  font-size: 12px;
}
</style>
