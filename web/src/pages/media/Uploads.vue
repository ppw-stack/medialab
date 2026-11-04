<template>
  <div class="page">
    <PageHeader
      eyebrow="媒体库 · MEDIA"
      title="数据同步"
      sub="本地 → 网盘——监听目录、上传、同步后可删本地"
      compact
    >
      <template #actions>
        <a-button type="primary" size="small" @click="openCreate">
          新建任务
        </a-button>
      </template>
    </PageHeader>

    <a-row :gutter="16">
      <a-col :xs="24" :lg="14">
        <a-card size="small">
          <a-spin :spinning="loading">
            <a-list :data-source="tasks" size="small" :locale="{ emptyText: '暂无上传任务' }">
              <template #renderItem="{ item }">
                <a-list-item>
                  <a-space direction="vertical" :size="2" style="width: 100%">
                    <a-space>
                      <strong>{{ item.name }}</strong>
                      <a-tag :color="item.enabled ? 'green' : 'default'">
                        {{ item.enabled ? "已启用" : "已停用" }}
                      </a-tag>
                      <a-tag :color="item.listening ? 'blue' : 'orange'">
                        {{ item.listening ? "监听中" : "未监听" }}
                      </a-tag>
                    </a-space>
                    <span class="muted mono">
                      {{ item.watch_root }} → {{ item.target_path || item.target_file_id }}
                    </span>
                    <a-space :size="12" class="muted">
                      <span>已上传 {{ item.total_uploaded }}</span>
                      <span>失败 {{ item.total_failed }}</span>
                      <span>{{ formatBytes(item.total_bytes) }}</span>
                      <span v-if="item.current" class="mono">
                        正在传：{{ item.current }}
                      </span>
                    </a-space>
                  </a-space>
                  <a-space>
                    <a-button size="small" @click="openDetail(item)">详情</a-button>
                    <a-button size="small" @click="openEdit(item)">编辑</a-button>
                    <a-button size="small" @click="toggleEnabled(item)">
                      {{ item.enabled ? "停用" : "启用" }}
                    </a-button>
                    <a-popconfirm
                      title="删除这个上传任务？"
                      description="不会删除云端已有的文件。"
                      @confirm="remove(item)"
                    >
                      <a-button size="small" danger>删除</a-button>
                    </a-popconfirm>
                  </a-space>
                </a-list-item>
              </template>
            </a-list>
          </a-spin>
        </a-card>
      </a-col>

      <a-col :xs="24" :lg="10">
        <a-card size="small" title="任务详情">
          <a-empty
            v-if="!current"
            description="选择一个任务查看运行状态"
            style="margin: 24px 0"
          />
          <template v-else>
            <a-descriptions bordered :column="1" size="small">
              <a-descriptions-item label="名称">{{ current.name }}</a-descriptions-item>
              <a-descriptions-item label="监听目录">
                <span class="mono">
                  {{ current.watch_root
                  }}
                </span>
              </a-descriptions-item>
              <a-descriptions-item label="云端目标">
                {{ current.target_path || current.target_file_id }}
              </a-descriptions-item>
              <a-descriptions-item label="状态">
                <a-tag :color="current.listening ? 'blue' : 'orange'">
                  {{ current.listening ? "监听中" : "未监听" }}
                </a-tag>
                <a-tag v-if="runtime?.syncing" color="processing">同步中</a-tag>
              </a-descriptions-item>
              <a-descriptions-item label="统计">
                上传 {{ current.total_uploaded }} · 失败 {{ current.total_failed }} ·
                {{ formatBytes(current.total_bytes) }}
              </a-descriptions-item>
              <a-descriptions-item v-if="watch" label="监听事件">
                {{ watch.events ?? 0 }} 次事件 · 投递 {{ watch.dispatched ?? 0 }} ·
                过滤 {{ watch.filtered ?? 0 }}
              </a-descriptions-item>
              <a-descriptions-item v-if="runtime?.error" label="错误">
                <span class="error-text">{{ runtime.error }}</span>
              </a-descriptions-item>
              <a-descriptions-item v-if="current.runtime_error" label="任务错误">
                <span class="error-text">{{ current.runtime_error }}</span>
              </a-descriptions-item>
            </a-descriptions>

            <a-space style="margin-top: 12px">
              <a-button
                type="primary"
                size="small"
                :loading="syncing"
                @click="syncNow"
              >
                立即全量同步
              </a-button>
              <span class="muted">补偿事件流漏掉的文件</span>
            </a-space>

            <div ref="logBox" class="logs">
              <div v-for="(line, i) in runtime?.logs ?? []" :key="i" class="log-line">
                <span class="log-time">{{ logTime(line) }}</span>
                <span class="log-text">{{ logText(line) }}</span>
              </div>
            </div>

          </template>
        </a-card>
      </a-col>
    </a-row>

    <!-- 新建 / 编辑 -->
    <a-modal
      v-model:open="formOpen"
      :title="editing ? '编辑任务' : '新建上传任务'"
      :confirm-loading="saving"
      ok-text="保存"
      cancel-text="取消"
      @ok="save"
    >
      <a-form layout="vertical">
        <a-form-item label="任务名" required>
          <a-input v-model:value="form.name" placeholder="例如：PT 下载目录" />
        </a-form-item>

        <a-form-item label="目标网盘" required>
          <a-select
            v-model:value="form.driver_id"
            placeholder="选择目标网盘"
            @change="onDriverChange"
          >
            <a-select-option
              v-for="d in targetDrivers"
              :key="d.id"
              :value="d.id"
            >
              {{ d.name }}
            </a-select-option>
          </a-select>
        </a-form-item>

        <a-form-item label="监听目录（本地）" required>
          <div class="target-picker">
            <DirBrowser
              title="本地目录"
              :stack="localStack"
              :dirs="localDirs"
              :loading="loadingLocal"
              @open="openLocal"
              @go="goLocal"
            />
          </div>
          <div class="muted hint">
            已选：{{ form.watch_root || "（未选）" }}
          </div>
          <a-alert
            type="warning"
            show-icon
            style="margin-top: 8px"
            message="服务能看到的所有目录都能选"
            description="包括容器内的 /app（含明文密钥的 .env）。请只监听确实需要同步到目标网盘的目录。"
          />
        </a-form-item>

        <a-form-item label="云端目标目录" required>
          <div class="target-picker">
            <DirBrowser
              title="目标网盘目录"
              :stack="targetStack"
              :dirs="targetDirs"
              :loading="loadingTarget"
              @open="openTarget"
              @go="goTarget"
            />
          </div>
          <div class="muted hint">
            已选：{{ form.target_path || "（未选）" }}
          </div>
        </a-form-item>

        <a-form-item>
          <a-space direction="vertical">
            <a-checkbox v-model:checked="form.include_subdirs">递归子目录</a-checkbox>
            <a-checkbox v-model:checked="form.enabled">启用（立即开始监听）</a-checkbox>
            <a-checkbox v-model:checked="form.delete_after_upload">
              同步成功后删除本地文件（彻底删除，不可恢复）
            </a-checkbox>
            <a-alert
              v-if="form.delete_after_upload"
              type="warning"
              show-icon
              style="max-width: 460px"
              message="这是破坏性操作"
              description="上传成功后本地文件会被**彻底删除**（unlink，不进回收站）。只有「云端 fileId 存在且大小与本地一致」才会删；失败 / 冲突 / 云端已有（已存在）一律保留本地文件。**空文件夹会一并删除**（向上递归直到监听根，监听根本身保留）。云端是完整副本，需要时可从网盘重新下载。"
            />
          </a-space>
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from "vue"
import { message } from "ant-design-vue"
import DirBrowser from "../../components/DirBrowser.vue"
import PageHeader from "../../components/PageHeader.vue"
import { errMsg, mediaApi as api } from "../../api"
import type {
  DirEntry,
  Driver,
  SyncNowResult,
  UploadCapabilities,
  UploadRuntime,
  UploadTask,
} from "../../types"
import { formatBytes } from "../../utils/format"

const tasks = ref<UploadTask[]>([])
const drivers = ref<Driver[]>([])
const loading = ref(false)
const saving = ref(false)
const syncing = ref(false)

/** 详情面板当前选中的任务 */
const current = ref<UploadTask | null>(null)
const runtime = ref<UploadRuntime | null>(null)
const logBox = ref<HTMLElement | null>(null)

/** 新建 / 编辑表单 */
const formOpen = ref(false)
const editing = ref<UploadTask | null>(null)
const form = reactive({
  name: "",
  driver_id: undefined as number | undefined,
  watch_root: "",
  include_subdirs: true,
  enabled: true,
  delete_after_upload: false,
  target_file_id: "",
  target_path: "",
})

/** 目标网盘目录浏览 */
const targetStack = ref<DirEntry[]>([])

/** 本地监听目录浏览 */
const localStack = ref<DirEntry[]>([])
const localDirs = ref<DirEntry[]>([])
const loadingLocal = ref(false)
const targetDirs = ref<DirEntry[]>([])
const loadingTarget = ref(false)

let timer: number | undefined

/**
 * **支持上传的驱动类型**（后端声明，前端据此过滤下拉）。
 *
 * 加新网盘时前端零改动——后端在 `UPLOADABLE_DRIVER_TYPES` 登记即可。
 */
const uploadableTypes = ref<string[]>([])

/** 可作为同步目标的存储源 */
const targetDrivers = computed(() =>
  drivers.value.filter((d) => uploadableTypes.value.includes(d.type)),
)

const watch = computed(() => runtime.value?.watch)

onMounted(async () => {
  await reload()
  await loadDrivers()
  await loadCapabilities()
  timer = window.setInterval(poll, 2000)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})

async function reload(): Promise<void> {
  loading.value = true
  try {
    tasks.value = await api.get<UploadTask[]>("/upload-tasks")
    // **默认展示第一个任务的详情**——用户打开页面不用再点一次。
    // 只在没选中任何任务时自动选（避免覆盖用户已选中的任务）。
    if (!current.value && tasks.value.length > 0) {
      await openDetail(tasks.value[0])
    }
  } catch (e) {
    message.error(errMsg(e, "加载上传任务失败"))
  } finally {
    loading.value = false
  }
}

async function loadDrivers(): Promise<void> {
  try {
    drivers.value = await api.get<Driver[]>("/drivers")
  } catch {
    // 没有可用的目标存储源时表单里选不到，不算错
  }
}

async function loadCapabilities(): Promise<void> {
  try {
    const caps = await api.get<UploadCapabilities>("/upload-tasks/capabilities")
    uploadableTypes.value = caps.uploadable_types
  } catch {
    // 拿不到能力列表时保守处理：不显示任何存储源
    uploadableTypes.value = []
  }
}

async function poll(): Promise<void> {
  if (!current.value) return
  try {
    runtime.value = await api.get<UploadRuntime>(`/upload-tasks/${current.value.id}/status`)
    await scrollLogs()
  } catch {
    // 任务被删了就算了
  }
}

async function scrollLogs(): Promise<void> {
  await nextTick()
  if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight
}

// ---- 详情 ----

async function openDetail(task: UploadTask): Promise<void> {
  current.value = task
  try {
    runtime.value = await api.get<UploadRuntime>(`/upload-tasks/${task.id}/status`)
  } catch {
    runtime.value = null
  }
}

async function toggleEnabled(task: UploadTask): Promise<void> {
  const next = !task.enabled
  try {
    const updated = await api.patch<UploadTask>(`/upload-tasks/${task.id}`, {
      enabled: next,
    })
    message.success(next ? "已启用" : "已停用")
    await reload()
    // 详情面板要拿**更新后**的任务对象——传旧对象会显示过期状态
    if (current.value?.id === task.id) {
      current.value = updated
      await openDetail(updated)
    }
  } catch (e) {
    message.error(errMsg(e, "操作失败"))
  }
}

async function remove(task: UploadTask): Promise<void> {
  try {
    await api.del(`/upload-tasks/${task.id}`)
    message.success("已删除")
    if (current.value?.id === task.id) {
      current.value = null
      runtime.value = null
    }
    await reload()
  } catch (e) {
    message.error(errMsg(e, "删除失败"))
  }
}

async function syncNow(): Promise<void> {
  if (!current.value) return
  syncing.value = true
  try {
    const result = await api.post<SyncNowResult>(`/upload-tasks/${current.value.id}/sync`)
    message.success(
      `同步完成：上传 ${result.uploaded}，覆盖 ${result.overwritten}，` +
        `已存在 ${result.unchanged}，失败 ${result.failed}` +
        (result.deleted_local ? `，本地已删除 ${result.deleted_local}` : ""),
    )
    await reload()
    await openDetail(current.value)
  } catch (e) {
    message.error(errMsg(e, "同步失败"))
  } finally {
    syncing.value = false
  }
}

// ---- 新建 / 编辑 ----

function openCreate(): void {
  editing.value = null
  form.name = ""
  form.driver_id = targetDrivers.value[0]?.id
  form.watch_root = ""
  localStack.value = [{ id: "/", name: "/" }]
  localDirs.value = []
  form.include_subdirs = true
  form.enabled = true
  form.delete_after_upload = false
  form.target_file_id = ""
  form.target_path = ""
  targetStack.value = [{ id: "", name: "网盘根目录" }]
  targetDirs.value = []
  formOpen.value = true
  if (form.driver_id) void loadTargetDirs("")
  void loadLocalDirs("/")
}

/** 打开编辑表单（预填已有任务的值）。 */
function openEdit(task: UploadTask): void {
  editing.value = task
  form.name = task.name
  form.driver_id = task.driver_id
  form.watch_root = task.watch_root
  form.watch_root = task.watch_root
  localStack.value = [{ id: task.watch_root, name: task.watch_root }]
  localDirs.value = []
  form.include_subdirs = task.include_subdirs
  form.enabled = task.enabled
  form.delete_after_upload = task.delete_after_upload
  form.target_file_id = task.target_file_id
  form.target_path = task.target_path ?? ""
  // 目录浏览器从**目标目录本身**开始（部分网盘没有「路径 ↔ id」反查，
  // 只能从 id 往下钻，无法回到父级）
  targetStack.value = [{ id: task.target_file_id, name: task.target_path || "目标目录" }]
  targetDirs.value = []
  formOpen.value = true
  void loadTargetDirs(task.target_file_id)
  void loadLocalDirs(task.watch_root)
}

async function save(): Promise<void> {
  if (!form.name || !form.driver_id || !form.watch_root) {
    message.warning("请填写任务名、存储源与监听根")
    return
  }
  saving.value = true
  try {
    const payload = {
      name: form.name,
      driver_id: form.driver_id,
      target_file_id: form.target_file_id,
      target_path: form.target_path,
      watch_root: form.watch_root,
      include_subdirs: form.include_subdirs,
      enabled: form.enabled,
      delete_after_upload: form.delete_after_upload,
    }
    if (editing.value) {
      await api.patch(`/upload-tasks/${editing.value.id}`, payload)
      message.success("已保存")
    } else {
      await api.post("/upload-tasks", payload)
      message.success("已创建")
    }
    formOpen.value = false
    await reload()
  } catch (e) {
    message.error(errMsg(e, "保存失败"))
  } finally {
    saving.value = false
  }
}

// ---- 目标网盘目录浏览 ----

/** 切换存储源 —— 目标目录必须重置（fileId 是**另一个账号**的）。 */
function onDriverChange(driverId: number): void {
  form.target_file_id = ""
  form.target_path = ""
  targetStack.value = [{ id: "", name: "网盘根目录" }]
  targetDirs.value = []
  void loadTargetDirs("", driverId)
}

async function loadTargetDirs(parentId: string, driverId?: number): Promise<void> {
  const driver = driverId ?? form.driver_id
  if (!driver) return
  loadingTarget.value = true
  try {
    const resp = await api.get<{ dirs: DirEntry[] }>(
      `/upload-tasks/targets?driver_id=${driver}&parent_id=${encodeURIComponent(parentId)}`,
    )
    targetDirs.value = resp.dirs ?? []
  } catch (e) {
    message.error(errMsg(e, "加载目标网盘目录失败"))
  } finally {
    loadingTarget.value = false
  }
}

/**
 * 由面包屑栈拼出目标路径（展示用）。
 *
 * 根节点（id 为空串 = 网盘根目录）不参与拼接 —— 它的「名字」只是占位。
 */
function targetPathOf(stack: DirEntry[]): string {
  return stack
    .map((c) => c.name)
    .filter((n) => n !== "网盘根目录")
    .join("/")
}

// ---- 本地目录浏览 ----

/** 加载本地目录的子目录列表。 */
async function loadLocalDirs(path: string): Promise<void> {
  loadingLocal.value = true
  try {
    const resp = await api.get<{ dirs: DirEntry[] }>(
      `/upload-tasks/local-dirs?path=${encodeURIComponent(path)}`,
    )
    localDirs.value = resp.dirs ?? []
  } catch (e) {
    message.error(errMsg(e, "加载本地目录失败"))
    localDirs.value = []
  } finally {
    loadingLocal.value = false
  }
}

/**
 * 进入本地子目录 —— **同时把它设为选中的监听目录**。
 *
 * 语义与目标网盘目录一致：「正在看哪个目录」=「要监听哪个目录」。
 */
function openLocal(entry: DirEntry): void {
  localStack.value = [...localStack.value, entry]
  form.watch_root = entry.id
  void loadLocalDirs(entry.id)
}

/** 面包屑回退 —— 选回那一层目录。 */
function goLocal(index: number): void {
  localStack.value = localStack.value.slice(0, index + 1)
  const picked = localStack.value[index]
  form.watch_root = picked?.id ?? ""
  void loadLocalDirs(picked?.id ?? "/")
}

/**
 * 进入子目录 —— **同时把它设为选中的目标目录**。
 *
 * 目录浏览器的语义是「正在看哪个目录」=「要传到哪个目录」，
 * 所以点进子目录即选中它（面包屑回退则选回那一层）。
 */
function openTarget(entry: DirEntry): void {
  targetStack.value = [...targetStack.value, entry]
  form.target_file_id = entry.id
  form.target_path = targetPathOf(targetStack.value)
  void loadTargetDirs(entry.id)
}

/** 面包屑回退 —— 选回那一层目录。 */
function goTarget(index: number): void {
  targetStack.value = targetStack.value.slice(0, index + 1)
  const picked = targetStack.value[index]
  form.target_file_id = picked?.id ?? ""
  form.target_path = targetPathOf(targetStack.value)
  void loadTargetDirs(picked?.id ?? "")
}

/** 日志行的时间戳（后端格式 ``[HH:MM:SS] 内容``）。 */
function logTime(line: string): string {
  const m = /^\[(\d{2}:\d{2}:\d{2})\]/.exec(line)
  return m ? m[1] : ""
}

/** 去掉时间戳后的内容。 */
function logText(line: string): string {
  return line.replace(/^\[\d{2}:\d{2}:\d{2}\]\s*/, "")
}
</script>

<style scoped>
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}

.hint {
  margin-top: 4px;
}

.error-text {
  color: #cf1322;
}

.target-picker {
  width: 100%;
}

.logs {
  margin-top: 12px;
  max-height: 260px;
  overflow-y: auto;
  border: 1px solid #f0f0f0;
  border-radius: 4px;
  padding: 6px;
  background: #fafafa;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}

.log-line {
  display: flex;
  gap: 8px;
  line-height: 1.7;
}

.log-time {
  color: #bbb;
  flex-shrink: 0;
}

.log-text {
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
