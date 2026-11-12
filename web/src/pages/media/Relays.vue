<template>
  <div class="page">
    <PageHeader
      eyebrow="媒体库 · MEDIA"
      title="转存（光鸭 → 光鸭）"
      sub="A 账号分享、B 账号保存——服务端内部复制，不占本机带宽"
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
          走的是<b>分享 + 保存</b>：源账号建分享，目标账号保存。
          数据由光鸭服务端内部复制（实测 100MB 约 7 秒落盘），
          <b>不占本机带宽与磁盘</b>，也不吃「云添加」额度。
        </template>
      </a-alert>

      <a-alert type="warning" show-icon :closable="false" class="tip">
        <template #message>
          <b>同名不同大小不覆盖</b>——记为冲突并跳过（不覆盖、不改名）。
          目标已有同名同大小则直接跳过，故可以放心重复执行。
          建分享有频率限制，故提交需<b>错开</b>（间隔越大越稳）。
        </template>
      </a-alert>

      <a-alert v-if="form.deleteSource" type="error" show-icon :closable="false" class="tip">
        <template #message>
          <b>已开启「搬完清理源」</b>——校验通过的文件会从源账号删除
          （进回收站，非永久删除）。请确认源目录选对了。
        </template>
      </a-alert>

      <a-row :gutter="12">
        <a-col :span="12">
          <DirBrowser
            title="① 源光鸭目录"
            :stack="sourceStack"
            :dirs="sourceDirs"
            :loading="loadingSource"
            @open="openSource"
            @go="goSource"
          />
        </a-col>
        <a-col :span="12">
          <DirBrowser
            title="② 目标光鸭目录"
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
        <a-form-item label="源存储源">
          <a-select
            v-model:value="sourceDriverId"
            :options="driverOptions"
            style="width: 160px"
            @change="onSourceDriverChange"
          />
        </a-form-item>
        <a-form-item label="目标存储源">
          <a-select
            v-model:value="targetDriverId"
            :options="driverOptions"
            style="width: 160px"
            @change="onTargetDriverChange"
          />
        </a-form-item>
        <a-form-item label="最小体积 (GB)">
          <a-input-number v-model:value="form.minSizeGb" :min="0" :step="0.5" style="width: 100px" />
        </a-form-item>
        <a-form-item label="最大体积 (GB)">
          <a-input-number v-model:value="form.maxSizeGb" :min="0" :step="1" style="width: 100px" />
          <span class="hint">0 = 不限</span>
        </a-form-item>
        <a-form-item label="建分享间隔">
          <a-input-number
            v-model:value="form.submitIntervalSec"
            :min="0"
            :max="600"
            :step="10"
            style="width: 90px"
          />
          <span class="hint">秒，0 = 不限速</span>
        </a-form-item>
        <a-form-item label="排除原盘">
          <a-switch v-model:checked="form.excludeDisc" />
          <span class="hint">iso / m2ts / BDMV</span>
        </a-form-item>
        <a-form-item label="搬完清理源">
          <a-switch v-model:checked="form.deleteSource" />
          <span class="hint">删源端数据</span>
        </a-form-item>
        <a-form-item>
          <a-button type="primary" :loading="starting" :disabled="!canRun" @click="start">
            开始转存
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
        {{ job.current_path || "（等待第一个目录）" }}
      </div>

      <a-progress v-if="job.total_dirs" :percent="dirProgress" size="small" />
      <div v-if="job.total_dirs" class="muted">
        已处理 {{ job.processed_dirs }} / {{ job.total_dirs }} 个目录
        <span v-if="remainMinutes > 0">· 预计还需 {{ remainMinutes }} 分钟</span>
      </div>

      <a-descriptions :column="4" size="small" bordered class="stats">
        <a-descriptions-item label="已遍历目录">{{ job.dirs_walked }}</a-descriptions-item>
        <a-descriptions-item label="已发现文件">{{ job.discovered }}</a-descriptions-item>
        <a-descriptions-item label="已转存体积">
          {{ formatBytes(job.transferred_bytes) }}
        </a-descriptions-item>
        <a-descriptions-item label="成功">{{ job.success }}</a-descriptions-item>

        <a-descriptions-item label="已存在跳过">{{ job.skipped_existing }}</a-descriptions-item>
        <a-descriptions-item label="冲突">{{ job.conflicts }}</a-descriptions-item>
        <a-descriptions-item label="失败">{{ job.failed }}</a-descriptions-item>
        <a-descriptions-item label="阈值过滤">{{ job.filtered_small }}</a-descriptions-item>

        <a-descriptions-item label="超上限过滤">{{ job.filtered_large }}</a-descriptions-item>
        <a-descriptions-item label="后缀/原盘过滤">
          {{ job.filtered_ext }}
        </a-descriptions-item>
      </a-descriptions>

      <a-alert
        v-if="job.error"
        type="error"
        show-icon
        :closable="false"
        class="mt-sm"
        :message="job.error"
      />

      <template v-if="job.failed_items.length">
        <div class="section">失败明细（{{ job.failed }}）</div>
        <DataTable
          :data-source="job.failed_items"
          :columns="failedColumns"
          row-key="path"
          size="small"
          :pagination="false"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'size'">
              {{ record.size_gb }} GB
            </template>
          </template>
        </DataTable>
      </template>

      <template v-if="job.conflict_items.length">
        <div class="section">同名不同大小（{{ job.conflicts }}）—— 未覆盖、未改名</div>
        <DataTable
          :data-source="job.conflict_items"
          :columns="conflictColumns"
          row-key="path"
          size="small"
          :pagination="false"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'source'">{{ record.source_gb }} GB</template>
            <template v-else-if="column.key === 'target'">{{ record.target_gb }} GB</template>
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
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from "vue"
import { message } from "ant-design-vue"
import DirBrowser from "../../components/DirBrowser.vue"
import PageHeader from "../../components/PageHeader.vue"
import { errMsg, mediaApi as api } from "../../api"
import type { DirEntry, Driver, RelayJob, RelayRunInput } from "../../types"
import { formatBytes } from "../../utils/format"
import DataTable from "../../components/DataTable.vue"

const drivers = ref<Driver[]>([])
const sourceDriverId = ref<number | null>(null)
const targetDriverId = ref<number | null>(null)

const sourceStack = ref<DirEntry[]>([])
const targetStack = ref<DirEntry[]>([])
const sourceDirs = ref<DirEntry[]>([])
const targetDirs = ref<DirEntry[]>([])
const loadingSource = ref(false)
const loadingTarget = ref(false)
const sourceError = ref("")
const targetError = ref("")

const form = reactive({
  minSizeGb: 0,
  maxSizeGb: 0,
  submitIntervalSec: 15,
  excludeDisc: true,
  deleteSource: false,
})

const job = ref<RelayJob | null>(null)
const starting = ref(false)
const cancelling = ref(false)
const logBox = ref<HTMLElement | null>(null)

let timer: number | undefined

const failedColumns = [
  { title: "文件", dataIndex: "name", key: "name", ellipsis: true },
  { title: "大小", key: "size", width: 100 },
  { title: "原因", dataIndex: "reason", key: "reason", ellipsis: true },
]

const conflictColumns = [
  { title: "文件", dataIndex: "name", key: "name", ellipsis: true },
  { title: "源", key: "source", width: 110 },
  { title: "目标", key: "target", width: 110 },
]

/** 可选存储源（只列光鸭盘——转存两边都必须是光鸭）。 */
const driverOptions = computed(() =>
  drivers.value
    .filter((d) => d.type === "guangya")
    .map((d) => ({ value: d.id, label: d.name })),
)

const sourceDriver = computed(
  () => drivers.value.find((d) => d.id === sourceDriverId.value) ?? null,
)
const targetDriver = computed(
  () => drivers.value.find((d) => d.id === targetDriverId.value) ?? null,
)

const currentSource = computed(() => sourceStack.value[sourceStack.value.length - 1])
const currentTarget = computed(() => targetStack.value[targetStack.value.length - 1])

const canRun = computed(
  () =>
    !!sourceDriver.value &&
    !!targetDriver.value &&
    sourceDriver.value.id !== targetDriver.value.id &&
    !!currentSource.value &&
    currentTarget.value !== undefined &&
    job.value?.state !== "running",
)

/** 目录处理进度（%）。 */
const dirProgress = computed(() => {
  if (!job.value?.total_dirs) return 0
  return Math.round((job.value.processed_dirs / job.value.total_dirs) * 100)
})

/**
 * 预计还需多少分钟。
 *
 * 每个源目录都要建一次分享，故剩余时间 ≈ 剩余目录数 × 建分享间隔。
 * （分享上限 20 个文件/次，目录多时这个数是主要的等待来源。）
 */
const remainMinutes = computed(() => {
  if (!job.value?.total_dirs) return 0
  const left = Math.max(0, job.value.total_dirs - job.value.processed_dirs)
  return Math.ceil((left * form.submitIntervalSec) / 60)
})

onMounted(async () => {
  try {
    drivers.value = await api.get<Driver[]>("/drivers")
  } catch (e) {
    message.error(errMsg(e, "加载存储源失败"))
    return
  }

  const guangya = drivers.value.filter((d) => d.type === "guangya")
  if (guangya.length >= 2) {
    sourceDriverId.value = guangya[0]?.id ?? null
    targetDriverId.value = guangya[1]?.id ?? null
  } else if (guangya.length === 1) {
    sourceDriverId.value = guangya[0]?.id ?? null
  }
  if (sourceDriver.value) await resetSourceRoot()
  if (targetDriver.value) await resetTargetRoot()

  // 找回最近一次任务（刷新页面后还能看到进度）
  try {
    const jobs = await api.get<RelayJob[]>("/relays/jobs")
    if (jobs.length) {
      job.value = jobs[0] as RelayJob
      if (job.value.state === "running") startPolling()
    }
  } catch {
    // 没有任务不算错
  }
})

onUnmounted(stopPolling)

watch(
  () => job.value?.logs.length,
  async () => {
    await nextTick()
    if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight
  },
)

// -- 目录浏览 --

async function resetSourceRoot(): Promise<void> {
  sourceStack.value = [{ id: "", name: "光鸭根目录" }]
  await loadSource()
}

async function resetTargetRoot(): Promise<void> {
  targetStack.value = [{ id: "", name: "光鸭根目录" }]
  await loadTarget()
}

async function onSourceDriverChange(): Promise<void> {
  await resetSourceRoot()
}

async function onTargetDriverChange(): Promise<void> {
  await resetTargetRoot()
}

async function loadSource(): Promise<void> {
  if (!sourceDriver.value || !currentSource.value) return
  loadingSource.value = true
  sourceError.value = ""
  try {
    const res = await api.get<{ dirs: DirEntry[] }>(
      `/relays/dirs?driver_id=${sourceDriver.value.id}&parent_id=${encodeURIComponent(currentSource.value.id)}`,
    )
    sourceDirs.value = res.dirs
  } catch (e) {
    sourceError.value = errMsg(e, "读取源目录失败")
    sourceDirs.value = []
  } finally {
    loadingSource.value = false
  }
}

async function loadTarget(): Promise<void> {
  if (!targetDriver.value || !currentTarget.value) return
  loadingTarget.value = true
  targetError.value = ""
  try {
    const res = await api.get<{ dirs: DirEntry[] }>(
      `/relays/dirs?driver_id=${targetDriver.value.id}&parent_id=${encodeURIComponent(currentTarget.value.id)}`,
    )
    targetDirs.value = res.dirs
  } catch (e) {
    targetError.value = errMsg(e, "读取目标目录失败")
    targetDirs.value = []
  } finally {
    loadingTarget.value = false
  }
}

function openSource(dir: DirEntry): void {
  sourceStack.value = [...sourceStack.value, dir]
  void loadSource()
}

function goSource(index: number): void {
  sourceStack.value = sourceStack.value.slice(0, index + 1)
  void loadSource()
}

function openTarget(dir: DirEntry): void {
  targetStack.value = [...targetStack.value, dir]
  void loadTarget()
}

function goTarget(index: number): void {
  targetStack.value = targetStack.value.slice(0, index + 1)
  void loadTarget()
}

// -- 转存 --

async function start(): Promise<void> {
  if (!sourceDriver.value || !targetDriver.value || !currentSource.value || !currentTarget.value) {
    return
  }
  starting.value = true
  try {
    const payload: RelayRunInput = {
      source_driver_id: sourceDriver.value.id,
      source_file_id: currentSource.value.id,
      target_driver_id: targetDriver.value.id,
      target_file_id: currentTarget.value.id,
      min_size_mb: Math.round((form.minSizeGb || 0) * 1024),
      max_size_mb: Math.round((form.maxSizeGb || 0) * 1024),
      exclude_disc: form.excludeDisc,
      delete_source: form.deleteSource,
      submit_interval_sec: form.submitIntervalSec,
    }
    const res = await api.post<{ job_id: string }>("/relays/run", payload)
    job.value = await api.get<RelayJob>(`/relays/jobs/${res.job_id}`)
    startPolling()
  } catch (e) {
    message.error(errMsg(e, "启动转存失败"))
  } finally {
    starting.value = false
  }
}

async function cancel(): Promise<void> {
  if (!job.value) return
  cancelling.value = true
  try {
    await api.post(`/relays/jobs/${job.value.id}/cancel`)
    message.info("已请求取消（服务端已开始的保存任务不撤回）")
  } catch (e) {
    message.error(errMsg(e, "取消失败"))
  } finally {
    cancelling.value = false
  }
}

function startPolling(): void {
  stopPolling()
  timer = window.setInterval(() => void tick(), 2000)
}

function stopPolling(): void {
  if (timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
  }
}

async function tick(): Promise<void> {
  if (!job.value) return
  try {
    job.value = await api.get<RelayJob>(`/relays/jobs/${job.value.id}`)
    if (job.value.state !== "running") {
      stopPolling()
      if (job.value.state === "success") {
        message.success(
          `转存结束：成功 ${job.value.success} 个，冲突 ${job.value.conflicts} 个，失败 ${job.value.failed} 个`,
        )
      }
    }
  } catch (e) {
    message.error(errMsg(e, "查询转存进度失败"))
    stopPolling()
  }
}

// -- 展示 --

function stateLabel(state: string): string {
  switch (state) {
    case "running":
      return "进行中"
    case "success":
      return "已完成"
    case "failed":
      return "失败"
    case "cancelled":
      return "已取消"
    default:
      return state
  }
}

function stateColor(state: string): string {
  switch (state) {
    case "running":
      return "processing"
    case "success":
      return "success"
    case "failed":
      return "error"
    default:
      return "default"
  }
}

/** 拆出日志行首的时间戳（``[00:41:37]``），没有就返回空串。 */
function logTime(line: string): string {
  const m = line.match(/^\[(\d{2}:\d{2}:\d{2})\]\s*/)
  return m ? m[1] : ""
}

/** 去掉时间戳后的正文。 */
function logText(line: string): string {
  return line.replace(/^\[\d{2}:\d{2}:\d{2}\]\s*/, "")
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
  font-family: "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.log-text {
  color: #444;
  min-width: 0;
}

.muted {
  color: #999;
  font-size: 12px;
  margin-bottom: 8px;
}

.err {
  margin-top: 8px;
  color: #cf1322;
  font-size: 12px;
}
</style>
