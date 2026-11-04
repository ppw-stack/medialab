<template>
  <div class="page">
    <PageHeader
      eyebrow="媒体库 · MEDIA"
      title="离线下载"
      sub="本地 → 光鸭——由光鸭服务器来拉，绕开本机上行带宽限制"
      compact
    >
      <template #actions>
        <a-tag v-if="targetDriver" color="cyan">目标：{{ targetDriver.name }}</a-tag>
      </template>
    </PageHeader>

    <!-- 功能未启用：说明怎么开，而不是给一个点了就报错的入口 -->
    <a-alert
      v-if="capabilities && !capabilities.enabled"
      type="warning"
      show-icon
      class="tip"
    >
      <template #message>离线下载未启用</template>
      <template #description>
        需要同时配置三项（见 <code>.env.prod</code>）：
        <br />① <code>OFFLINE_HMAC_SECRET</code> —— 访问 token 的 HMAC 密钥
        <br />② <code>OFFLINE_PUBLIC_BASE</code> —— 公网入口，如
        <code>http://home.banstack.top:8092</code>（光鸭用它拉文件）
        <br />③ <code>OFFLINE_FILES_ROOT</code> —— 要暴露的源目录
        <br />
        <b>临时使用</b>：传完把端口映射关掉即可，不必改配置——
        服务在没配密钥时**不会挂载**，文件完全不可访问。
      </template>
    </a-alert>

    <template v-else>
      <a-alert type="info" show-icon :closable="false" class="tip">
        <template #message>
          光鸭的离线下载是<b>光鸭服务器主动来拉</b>——流量方向是下行，
          本机只承担接收，上行需求 ≈ 0。
          <b>同名同大小直接跳过</b>，可以放心重复执行。
        </template>
      </a-alert>

      <a-alert type="warning" show-icon :closable="false" class="tip">
        <template #message>
          ⚠️ <b>实测速率可能远低于预期</b>：光鸭把文件切成变长的随机区间并
          <b>多节点并发</b>拉取，其中约 <b>73% 是重复请求</b>；且它大部分时间
          在<b>排队</b>而非传输。
          链路本身够快（公网入口单分片实测 17 MB/s），瓶颈在光鸭服务端，
          客户端不可控。<b>大文件尤其慢</b>（实测 2.3GB 约 65 分钟）。
        </template>
      </a-alert>

      <a-card size="small">
        <a-row :gutter="12" :wrap="true">
          <a-col :xs="24" :lg="12">
            <DirBrowser
              title="① 本地源目录"
              :stack="sourceStack"
              :dirs="sourceDirs"
              :loading="loadingSource"
              @open="openSource"
              @go="goSource"
            />
          </a-col>
          <a-col :xs="24" :lg="12">
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

        <!--
          源目录下的文件（让用户知道要传什么）。

          **跟随格式过滤实时变化**——用户在下面勾了「只传 mkv」，
          这里立刻只剩 mkv，不用提交后才发现传错了。
        -->
        <div v-if="localFiles.length" class="files">
          <div class="section">
            将要上传的文件（{{ filteredLocalFiles.length }} 个<template
              v-if="form.exts.length"
            >，已按格式过滤，原 {{ localFiles.length }} 个）</template
            >）
          </div>
          <div class="file-list">
            <span
              v-for="f in filteredLocalFiles"
              :key="f.name"
              class="file-item"
            >
              <span class="file-name">{{ f.name }}</span>
              <span class="muted mono">{{ formatBytes(f.size) }}</span>
            </span>
          </div>
          <div v-if="!filteredLocalFiles.length" class="muted" style="margin-top: 6px">
            当前格式过滤下没有匹配的文件——放宽「只传指定格式」或换个源目录。
          </div>
        </div>

        <a-divider style="margin: 12px 0" />

        <!--
          参数区：用**栅格**而非 inline 表单——inline 表单项太多会挤成
          好几行（"递归子目录"被挤到单独一行），且宽度不可控。
        -->
        <a-row :gutter="[12, 8]" :wrap="true" class="param-row" :class="{ 'exts-open': extsOpen }">
          <a-col :xs="12" :sm="8" :md="4" :lg="3">
            <div class="param">
              <div class="param-label">最小体积 (GB)</div>
              <a-input-number v-model:value="form.minSizeGb" :min="0" :step="0.5" style="width: 100%" />
            </div>
          </a-col>
          <a-col :xs="12" :sm="8" :md="4" :lg="3">
            <div class="param">
              <div class="param-label">最大体积 (GB)</div>
              <a-input-number v-model:value="form.maxSizeGb" :min="0" :step="1" style="width: 100%" />
            </div>
          </a-col>
          <a-col :xs="12" :sm="8" :md="4" :lg="3">
            <div class="param">
              <div class="param-label">并发数</div>
              <a-input-number v-model:value="form.concurrency" :min="1" :max="20" style="width: 100%" />
            </div>
          </a-col>
          <a-col :xs="12" :sm="8" :md="4" :lg="3">
            <div class="param">
              <div class="param-label">提交间隔（秒）</div>
              <a-input-number v-model:value="form.submitIntervalSec" :min="0" :max="600" :step="10" style="width: 100%" />
            </div>
          </a-col>
          <a-col :xs="12" :sm="8" :md="4" :lg="3">
            <div class="param">
              <div class="param-label">递归子目录</div>
              <a-switch v-model:checked="form.includeSubdirs" />
            </div>
          </a-col>
          <a-col :xs="12" :sm="8" :md="4" :lg="3">
            <div class="param">
              <div class="param-label">排除原盘</div>
              <a-switch v-model:checked="form.excludeDisc" />
              <span class="hint">iso / m2ts / BDMV</span>
            </div>
          </a-col>
          <a-col :xs="24" :sm="12" :md="8" :lg="6">
            <div class="param">
              <div class="param-label">
                只传指定格式
                <a-tooltip title="逗号分隔的扩展名，如 mkv,mp4。留空 = 全传。用于「只传视频，跳过 nfo / 字幕 / 海报」">
                  <QuestionCircleOutlined class="param-help" />
                </a-tooltip>
              </div>
              <a-select
                v-model:value="form.exts"
                mode="tags"
                :placeholder="EXT_PRESETS.map((e) => e.label).join(' / ') + '（留空 = 全传）'"
                style="width: 100%"
                :token-separators="[',', '，', ' ']"
                :open="extsOpen"
                @open-change="extsOpen = $event"
              />
            </div>
          </a-col>
          <a-col :xs="24" :md="6" :lg="4">
            <div class="param param--action">
              <div class="param-label">&nbsp;</div>
              <a-button type="primary" :loading="starting" :disabled="!canRun" @click="start">
                开始离线下载
              </a-button>
            </div>
          </a-col>
        </a-row>

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
          {{ job.current_path || "（等待第一个文件）" }}
        </div>

        <a-descriptions bordered :column="1" size="small" class="mt">
          <a-descriptions-item label="已发现">
            {{ job.discovered }} 个文件
          </a-descriptions-item>
          <a-descriptions-item label="成功 / 失败">
            <span style="color: #16a34a">{{ job.success }}</span> /
            <span style="color: #cf1322">{{ job.failed }}</span>
            （跳过已存在 {{ job.skipped_existing }}，冲突 {{ job.conflicts }}）
          </a-descriptions-item>
          <a-descriptions-item label="已下载">
            {{ formatBytes(job.downloaded_bytes) }}
          </a-descriptions-item>
          <a-descriptions-item label="在途">
            {{ job.in_flight }} 个任务
            <span v-if="job.in_flight_items" class="muted mono">
              （光鸭侧计数，非百分比）
            </span>
          </a-descriptions-item>
          <a-descriptions-item v-if="job.error" label="错误">
            <span class="error-text">{{ job.error }}</span>
          </a-descriptions-item>
        </a-descriptions>

        <!-- 失败明细 -->
        <template v-if="job.failed_items.length">
          <div class="section">失败明细</div>
          <DataTable
            :data-source="job.failed_items"
            :columns="failedColumns"
            row-key="path"
            size="small"
            :pagination="false"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'size'">{{ formatBytes(record.size) }}</template>
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
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from "vue"
import { message } from "ant-design-vue"
import { QuestionCircleOutlined } from "@ant-design/icons-vue"
import DirBrowser from "../../components/DirBrowser.vue"
import DataTable from "../../components/DataTable.vue"
import PageHeader from "../../components/PageHeader.vue"
import { errMsg, mediaApi as api } from "../../api"
import type {
  DirEntry,
  Driver,
  OfflineCapabilities,
  OfflineJob,
  OfflineLocalFile,
  OfflineRunInput,
} from "../../types"
import { formatBytes } from "../../utils/format"

const capabilities = ref<OfflineCapabilities | null>(null)
const drivers = ref<Driver[]>([])
const targetDriver = ref<Driver | null>(null)

// ---- 源目录浏览（相对 files_root）----

const sourceStack = ref<DirEntry[]>([])
const sourceDirs = ref<DirEntry[]>([])
const loadingSource = ref(false)
const sourceError = ref("")
const localFiles = ref<OfflineLocalFile[]>([])

// ---- 目标目录浏览（光鸭 fileId）----

const targetStack = ref<DirEntry[]>([])
const targetDirs = ref<DirEntry[]>([])
const loadingTarget = ref(false)
const targetError = ref("")

const form = reactive({
  minSizeGb: 0,
  maxSizeGb: 0,
  concurrency: 2,
  submitIntervalSec: 2,
  includeSubdirs: true,
  excludeDisc: true,
  /** 只要这些扩展名（空 = 全传） */
  exts: [] as string[],
})

/**
 * 常用格式预设（只是**占位提示**，不是限制）。
 *
 * 用 `mode="tags"` 的下拉——用户可以直接输入任意扩展名（如 `iso`、
 * `wmv`），预设只是省得一个个敲。
 */
const EXT_PRESETS = [
  { value: "mkv", label: "mkv" },
  { value: "mp4", label: "mp4" },
  { value: "ts", label: "ts" },
  { value: "m2ts", label: "m2ts" },
  { value: "avi", label: "avi" },
  { value: "mov", label: "mov" },
  { value: "flv", label: "flv" },
  { value: "wmv", label: "wmv" },
  { value: "iso", label: "iso" },
]

const job = ref<OfflineJob | null>(null)
const starting = ref(false)
const cancelling = ref(false)
const logBox = ref<HTMLElement | null>(null)
/**
 * 格式下拉的展开态。
 *
 * 手动控制展开——tags 模式的默认下拉会**盖住**「开始离线下载」按钮
 * （下拉是 absolute 定位，父级没给足够高度）。展开时把按钮那一列
 * 顶下去就没了。
 */
const extsOpen = ref(false)

let timer: number | undefined

const failedColumns = [
  { title: "文件", dataIndex: "path", key: "path", ellipsis: true },
  { title: "大小", key: "size", width: 100 },
  { title: "原因", dataIndex: "reason", key: "reason", ellipsis: true },
]

const currentSource = computed(() => sourceStack.value[sourceStack.value.length - 1])
const currentTarget = computed(() => targetStack.value[targetStack.value.length - 1])

/**
 * 按「只传指定格式」过滤后的文件预览。
 *
 * 与后端 `collect_files(exts=...)` 同一口径（扩展名小写、不含点）。
 * 后端还会过滤 0 字节 / 原盘 / 体积阈值——这里只做**格式**这一项，
 * 因为用户最关心「我勾的格式对不对」。
 */
const filteredLocalFiles = computed(() => {
  const wanted = new Set(form.exts.map((e) => e.trim().toLowerCase().replace(/^\./, "")))
  if (!wanted.size) return localFiles.value
  return localFiles.value.filter((f) => {
    const ext = f.name.includes(".") ? f.name.split(".").pop()!.toLowerCase() : ""
    return wanted.has(ext)
  })
})

const canRun = computed(
  () =>
    !!targetDriver.value &&
    !!currentSource.value &&
    currentTarget.value !== undefined &&
    job.value?.state !== "running",
)

onMounted(async () => {
  try {
    capabilities.value = await api.get<OfflineCapabilities>(
      "/offline-downloads/capabilities",
    )
  } catch (e) {
    message.error(errMsg(e, "加载离线下载配置失败"))
    return
  }
  if (!capabilities.value.enabled) return

  try {
    drivers.value = await api.get<Driver[]>("/drivers")
  } catch (e) {
    message.error(errMsg(e, "加载存储源失败"))
    return
  }
  targetDriver.value = drivers.value.find((d) => d.type === "guangya") ?? null

  // 源目录：从配置的根开始
  const root = capabilities.value.browse_root
  if (root) {
    sourceStack.value = [{ id: "", name: root }]
    await loadSource()
  }
  if (targetDriver.value) {
    targetStack.value = [{ id: "", name: "光鸭根目录" }]
    await loadTarget()
  }

  // 找回最近一次任务（刷新页面后还能看到进度）
  try {
    const jobs = await api.get<OfflineJob[]>("/offline-downloads/jobs")
    if (jobs.length) {
      job.value = jobs[0] as OfflineJob
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

// ---- 源目录 ----

async function loadSource(): Promise<void> {
  if (!currentSource.value) return
  loadingSource.value = true
  sourceError.value = ""
  try {
    const res = await api.get<{ dirs: DirEntry[] }>(
      `/offline-downloads/local-dirs?path=${encodeURIComponent(currentSource.value.id)}`,
    )
    sourceDirs.value = res.dirs ?? []
  } catch (e) {
    sourceError.value = errMsg(e, "读取本地目录失败")
    sourceDirs.value = []
  } finally {
    loadingSource.value = false
  }
  await loadLocalFiles()
}

/** 列出源目录下的文件（展示「将要传什么」）。 */
async function loadLocalFiles(): Promise<void> {
  if (!currentSource.value) {
    localFiles.value = []
    return
  }
  try {
    const res = await api.get<{ files: OfflineLocalFile[] }>(
      `/offline-downloads/local-files?path=${encodeURIComponent(currentSource.value.id)}`,
    )
    localFiles.value = res.files ?? []
  } catch {
    localFiles.value = []
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

// ---- 目标目录 ----

async function loadTarget(): Promise<void> {
  if (!targetDriver.value || !currentTarget.value) return
  loadingTarget.value = true
  targetError.value = ""
  try {
    const res = await api.get<{ dirs: DirEntry[] }>(
      `/offline-downloads/targets?driver_id=${targetDriver.value.id}&parent_id=${encodeURIComponent(currentTarget.value.id)}`,
    )
    targetDirs.value = res.dirs ?? []
  } catch (e) {
    targetError.value = errMsg(e, "读取光鸭目录失败")
    targetDirs.value = []
  } finally {
    loadingTarget.value = false
  }
}

function openTarget(dir: DirEntry): void {
  targetStack.value = [...targetStack.value, dir]
  void loadTarget()
}

function goTarget(index: number): void {
  targetStack.value = targetStack.value.slice(0, index + 1)
  void loadTarget()
}

// ---- 开跑 ----

async function start(): Promise<void> {
  if (!targetDriver.value || !currentSource.value || !currentTarget.value) return
  starting.value = true
  try {
    // 源目录用**绝对路径**提交（后端会校验它在 files_root 之下）
    const root = capabilities.value?.browse_root ?? ""
    const localRoot = currentSource.value.id
      ? `${root}/${currentSource.value.id}`.replace(/\/+/g, "/")
      : root
    const payload: OfflineRunInput = {
      target_driver_id: targetDriver.value.id,
      target_file_id: currentTarget.value.id,
      target_path: targetStack.value
        .map((c) => c.name)
        .filter((n) => n !== "光鸭根目录")
        .join("/"),
      local_root: localRoot,
      include_subdirs: form.includeSubdirs,
      min_size_mb: Math.round(form.minSizeGb * 1024),
      max_size_mb: Math.round(form.maxSizeGb * 1024),
      exclude_disc: form.excludeDisc,
      exts: form.exts.join(","),
      concurrency: form.concurrency,
      submit_interval_sec: form.submitIntervalSec,
    }
    const res = await api.post<{ job_id: string }>("/offline-downloads/run", payload)
    job.value = await api.get<OfflineJob>(`/offline-downloads/jobs/${res.job_id}`)
    startPolling()
  } catch (e) {
    message.error(errMsg(e, "启动离线下载失败"))
  } finally {
    starting.value = false
  }
}

async function cancel(): Promise<void> {
  if (!job.value) return
  cancelling.value = true
  try {
    await api.post(`/offline-downloads/jobs/${job.value.id}/cancel`)
    message.info("已请求取消（已提交的离线任务留在光鸭继续跑）")
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
    job.value = await api.get<OfflineJob>(
      `/offline-downloads/jobs/${job.value.id}`,
    )
    if (job.value.state !== "running") {
      stopPolling()
      if (job.value.state === "success") {
        message.success(
          `离线下载结束：成功 ${job.value.success} 个，失败 ${job.value.failed} 个`,
        )
      }
    }
  } catch (e) {
    message.error(errMsg(e, "查询离线下载进度失败"))
    stopPolling()
  }
}

// ---- 展示辅助 ----

function stateLabel(state: string): string {
  switch (state) {
    case "running": return "运行中"
    case "success": return "成功"
    case "failed": return "失败"
    case "cancelled": return "已取消"
    default: return state
  }
}

function stateColor(state: string): string {
  switch (state) {
    case "running": return "processing"
    case "success": return "green"
    case "failed": return "red"
    case "cancelled": return "default"
    default: return "default"
  }
}

function logTime(line: string): string {
  const m = line.match(/^\[(\d{2}:\d{2}:\d{2})\]/)
  return m ? m[1] : ""
}

function logText(line: string): string {
  return line.replace(/^\[\d{2}:\d{2}:\d{2}\]\s*/, "")
}
</script>

<style scoped>
.hint {
  color: #999;
  font-size: 12px;
  margin-left: 4px;
}

/* 参数区：标签在上、控件在下（栅格布局，不用 inline 表单） */
.param {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.param-label {
  font-size: 12px;
  color: var(--text-secondary);
}

/* 参数标签旁的问号图标 */
.param-help {
  color: var(--text-tertiary);
  margin-left: 2px;
}

.param--action {
  justify-content: flex-end;
}

/*
  格式下拉展开时需要**额外高度**——Ant 的下拉面板是 absolute 定位，
  会盖住下面的「开始离线下载」按钮。展开时给参数区加一段底部留白。
*/
.exts-open .param-row {
  padding-bottom: 200px;
}

/*
  两个目录浏览器**等高**——Ant 的 a-row 用 flex，子项默认 stretch，
  但 DirBrowser 内部的 a-card 没有高度 100%，故矮的那个不撑开。
*/
:deep(.ant-row > .ant-col > .ant-card) {
  height: 100%;
}

.tip {
  margin-bottom: 12px;
}

.section {
  margin: 12px 0 6px;
  font-weight: 500;
  font-size: 13px;
}

/* 源目录下的文件列表 */
.files {
  margin-top: 12px;
}

.file-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  max-height: 160px;
  overflow-y: auto;
}

.file-item {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  padding: 2px 8px;
  background: var(--bg-inset);
  border-radius: var(--r-sm);
  font-size: 12px;
  max-width: 100%;
  min-width: 0;
}

.file-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.current {
  font-size: 12px;
  color: #666;
  margin-bottom: 8px;
}

/* .truncate 默认 inline-block——块级容器里要占满整行才能真正截断 */
.block-truncate {
  display: block;
}

.error-text {
  color: var(--error-text, #f5222d);
  font-family: var(--font-mono, monospace);
  font-size: var(--text-xs);
}

/* 卡片之间的间距 */
.mt {
  margin-top: var(--sp-3);
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

.log-line {
  display: flex;
  gap: 8px;
  align-items: baseline;
  padding: 1px 0;
  overflow-wrap: anywhere;
  word-break: normal;
}

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

.err {
  margin-top: 8px;
  color: #cf1322;
  font-size: 12px;
}
</style>
