<template>
  <div class="page">
    <PageHeader
      eyebrow="媒体库 · MEDIA"
      title="存储源"
      sub="网盘账号（115 / 光鸭盘）——凭据、容量与直链能力"
      compact
    >
      <template #actions>
        <a-button size="small" :loading="probingAll" @click="probeAll">全部测连</a-button>
        <a-button type="primary" size="small" @click="openCreate">新增</a-button>
      </template>
    </PageHeader>

    <a-card size="small">
      <DataTable
        :data-source="drivers"
        :columns="columns"
        row-key="id"
        size="small"
        table-layout="fixed"
        :loading="loading"
        :pagination="false"
        :locale="{ emptyText: '暂无存储源' }"
      >
        <template #bodyCell="{ column, record }">
          <!-- 名称 + 状态点 -->
          <template v-if="column.key === 'name'">
            <div class="name-cell">
              <a-badge :status="statusBadge(record.id)" />
              <span>{{ record.name }}</span>
              <a-tag v-if="record.type === 'p115'" color="blue">115</a-tag>
              <a-tag v-else-if="record.type === 'guangya'" color="cyan">光鸭盘</a-tag>
            </div>
            <div v-if="statusText(record.id)" class="status-hint">
              {{ statusText(record.id) }}
            </div>
          </template>

          <!-- 账号 + 容量 -->
          <template v-else-if="column.key === 'account'">
            <template v-if="statusOf(record.id)">
              <div class="mono">{{ statusOf(record.id)?.account_id || "—" }}</div>
              <div class="capacity">
                <a-progress
                  :percent="usedPercent(record.id)"
                  :show-info="false"
                  size="small"
                  :status="usedPercent(record.id) > 90 ? 'exception' : 'normal'"
                />
                <span class="muted">
                  {{ formatBytes(statusOf(record.id)?.used_bytes ?? 0) }} /
                  {{ formatBytes(statusOf(record.id)?.total_bytes ?? 0) }}
                </span>
              </div>
            </template>
            <span v-else class="muted">—</span>
          </template>

          <!-- 媒体库（cid + 路径） -->
          <template v-else-if="column.key === 'library'">
            <template v-if="record.media_cid">
              <div class="mono truncate">{{ record.media_cid }}</div>
              <div class="muted truncate">{{ record.media_path || "—" }}</div>
            </template>
            <a-tag v-else color="default">未配置</a-tag>
          </template>

          <!-- 本地输出目录 -->
          <template v-else-if="column.key === 'output_dir'">
            <span class="mono truncate">{{ record.output_dir || "—" }}</span>
          </template>

          <template v-else-if="column.key === 'device_type'">
            <span class="mono">{{ record.device_type ?? "—" }}</span>
            <a-tooltip
              v-if="record.device_type && !isQrCapable(record.device_type)"
              title="该设备类型不支持扫码登录，请编辑后改选"
            >
              <a-tag color="orange" style="margin-left: 6px">不可扫码</a-tag>
            </a-tooltip>
          </template>

          <template v-else-if="column.key === 'status'">
            <a-tag :color="statusColor(record.id)">{{ statusLabel(record.id) }}</a-tag>
          </template>

          <template v-else-if="column.key === 'action'">
            <a-space :size="4">
              <a-tooltip title="测连：检查凭据 / 账号容量 / 能否换直链">
                <a-button
                  size="small"
                  :loading="probingId === record.id"
                  @click="probeDriver(record.id)"
                >
                  测连
                </a-button>
              </a-tooltip>
              <a-tooltip
                :title="record.credential_masked ? '重新登录（会覆盖现有凭据）' : '登录存储源'"
              >
                <a-button size="small" @click="onLogin(record)">登录</a-button>
              </a-tooltip>
              <a-button size="small" @click="openConfig(record)">配置</a-button>
              <a-button
                size="small"
                :disabled="!record.media_cid || !record.output_dir"
                :loading="scanningId === record.id || isScanning(record.id)"
                @click="onScan(record)"
              >
                {{ isScanning(record.id) ? "扫描中" : "扫描" }}
              </a-button>
              <a-tooltip
                v-if="record.type === 'p115'"
                title="增量同步：只处理上次之后的变化，不遍历目录树"
              >
                <a-button
                  size="small"
                  :loading="incrementalId === record.id"
                  @click="onIncremental(record)"
                >
                  增量
                </a-button>
              </a-tooltip>
              <a-popconfirm
                title="删除存储源？关联的媒体条目也会一并删除。"
                ok-text="删除"
                cancel-text="取消"
                @confirm="onDelete(record.id)"
              >
                <a-button size="small" danger>删除</a-button>
              </a-popconfirm>
            </a-space>
          </template>
        </template>
      </DataTable>
    </a-card>

    <!-- 新增存储源 -->
    <a-modal
      v-model:open="createOpen"
      title="新增存储源"
      :confirm-loading="saving"
      ok-text="创建"
      cancel-text="取消"
      @ok="onCreate"
    >
      <a-form layout="vertical">
        <a-form-item label="名称">
          <a-input v-model:value="form.name" placeholder="我的 115" />
        </a-form-item>
        <a-form-item label="类型">
          <a-select v-model:value="form.type">
            <a-select-option value="p115">115 网盘</a-select-option>
            <a-select-option value="guangya">光鸭盘</a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item v-if="form.type === 'p115'" label="设备类型">
          <a-select v-model:value="form.device_type" :loading="deviceTypesLoading">
            <a-select-option
              v-for="d in deviceTypes"
              :key="d.value"
              :value="d.value"
              :disabled="!d.qr_capable"
            >
              {{ d.label }}
              <span v-if="!d.qr_capable" class="muted">（不支持扫码）</span>
            </a-select-option>
          </a-select>
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- 媒体库配置 -->
    <a-drawer
      v-model:open="configOpen"
      :title="`媒体库配置 — ${configDriver?.name ?? ''}`"
      :width="560"
      :confirm-loading="saving"
      @ok="onSaveConfig"
    >
      <a-form layout="vertical">
        <a-form-item
          :label="configDriver?.type === 'guangya' ? '光鸭媒体库 fileId' : '115 媒体库 cid'"
          required
        >
          <a-input
            v-model:value="configForm.media_cid"
            :placeholder="
              configDriver?.type === 'guangya' ? '1835243138919944265' : '3116168171472551604'
            "
            class="mono"
          />
          <div class="hint">
            {{
              configDriver?.type === "guangya"
                ? "光鸭的目录 fileId。用 id 而非路径：接口只接受 id，不接受路径。"
                : "115 的目录 id（数字）。用 cid 而非路径：目录改名后 id 不变，路径会失效。"
            }}
          </div>
        </a-form-item>

        <a-form-item v-if="configDriver?.type !== 'guangya'" label="媒体库路径">
          <a-input :value="configForm.media_path || ''" disabled class="mono" />
          <div class="hint">保存时按 cid 自动解析，仅供查看。</div>
        </a-form-item>

        <a-form-item label="本地媒体库目录" required>
          <a-input
            v-model:value="configForm.output_dir"
            placeholder="/app/library"
            class="mono"
          />
        </a-form-item>

        <a-form-item label="视频文件后缀" required>
          <a-select
            v-model:value="configForm.video_exts"
            mode="tags"
            :token-separators="[',', ' ']"
            placeholder="输入后缀后回车"
            class="mono"
          />
          <div class="hint">只有这些后缀的文件会生成 strm。</div>
        </a-form-item>

        <a-form-item label="媒体图片后缀">
          <a-select
            v-model:value="configForm.image_exts"
            mode="tags"
            :token-separators="[',', ' ']"
            placeholder="jpg png webp"
            class="mono"
          />
          <div class="hint">扫描时**同步到本地**（与 strm 同目录，保持原文件名）。</div>
        </a-form-item>

        <a-form-item label="媒体数据后缀">
          <a-select
            v-model:value="configForm.data_exts"
            mode="tags"
            :token-separators="[',', ' ']"
            placeholder="nfo ass srt"
            class="mono"
          />
          <div class="hint">扫描时**同步到本地**（与 strm 同目录，保持原文件名）。</div>
        </a-form-item>

        <a-form-item label="最小视频">
          <a-input-number
            v-model:value="configForm.min_size_mb"
            :min="0"
            :step="10"
            style="width: 160px"
          />
          <span class="unit">MB</span>
          <div class="hint">小于此体积的文件跳过（过滤样片）。0 = 不过滤。</div>
        </a-form-item>

        <a-form-item label="包含子目录">
          <a-switch v-model:checked="configForm.include_subdirs" />
          <div class="hint">
            关闭则只扫 cid 指向的那一层，不递归其下子目录。
          </div>
        </a-form-item>
      </a-form>

      <template #footer>
        <a-space>
          <a-button @click="configOpen = false">取消</a-button>
          <a-button type="primary" :loading="saving" @click="onSaveConfig">保存</a-button>
        </a-space>
      </template>
    </a-drawer>

    <!-- 增量同步进度 -->
    <a-modal
      v-model:open="incrementalOpen"
      title="增量同步"
      :footer="null"
      :width="720"
      :mask-closable="false"
      @cancel="closeIncremental"
    >
      <template v-if="incrementalJob">
        <a-descriptions :column="2" size="small" bordered>
          <a-descriptions-item label="存储源">
            {{ incrementalJob.driver_name }}
          </a-descriptions-item>
          <a-descriptions-item label="状态">
            <a-tag :color="scanStateColor(incrementalJob.state)">
              {{ scanStateLabel(incrementalJob.state) }}
            </a-tag>
          </a-descriptions-item>
          <a-descriptions-item label="事件" :span="2">
            <span class="mono">
              拉到 {{ incrementalJob.events_fetched }} 条，媒体库内
              {{ incrementalJob.events_in_scope }} 条
            </span>
          </a-descriptions-item>
          <a-descriptions-item label="STRM" :span="2">
            <span class="mono">
              新增 {{ incrementalJob.created }} / 更新 {{ incrementalJob.updated }} /
              未变 {{ incrementalJob.unchanged }} / 删除 {{ incrementalJob.deleted }}
            </span>
          </a-descriptions-item>
          <a-descriptions-item label="附件" :span="2">
            <span class="mono">
              新增 {{ incrementalJob.attachment_synced }} / 未变
              {{ incrementalJob.attachment_unchanged }} / 失败
              {{ incrementalJob.attachment_failed }}
            </span>
          </a-descriptions-item>
          <a-descriptions-item label="游标" :span="2">
            <span class="mono">
              {{ incrementalJob.cursor_old }} → {{ incrementalJob.cursor_new }}
            </span>
          </a-descriptions-item>
          <a-descriptions-item v-if="incrementalJob.conflicts" label="冲突" :span="2">
            <span class="mono">
              {{ incrementalJob.conflicts }} 处路径冲突（疑似目录改名）——建议跑一次全量
            </span>
          </a-descriptions-item>
          <a-descriptions-item label="正在处理" :span="2">
            <span class="mono path-line" :title="incrementalJob.current_path">
              {{ incrementalJob.current_path || "—" }}
            </span>
          </a-descriptions-item>
        </a-descriptions>

        <a-alert
          v-if="incrementalJob.state === 'failed'"
          type="error"
          :message="incrementalJob.error || '增量同步失败'"
          show-icon
          style="margin-top: 12px"
        />

        <div class="scan-log">
          <div
            v-for="(line, i) in incrementalJob.logs"
            :key="i"
            class="log-line mono"
          >
            {{ line }}
          </div>
        </div>
      </template>
    </a-modal>

    <!-- 扫描进度 -->
    <a-modal
      v-model:open="scanOpen"
      title="扫描进度"
      :footer="null"
      :width="720"
      :mask-closable="false"
      @cancel="closeScan"
    >
      <template v-if="scanJob">
        <a-descriptions :column="2" size="small" bordered>
          <a-descriptions-item label="存储源">
            {{ scanJob.driver_name }}
          </a-descriptions-item>
          <a-descriptions-item label="状态">
            <a-tag :color="scanStateColor(scanJob.state)">
              {{ scanStateLabel(scanJob.state) }}
            </a-tag>
          </a-descriptions-item>
          <a-descriptions-item label="已处理">
            {{ scanJob.processed }} 个文件
          </a-descriptions-item>
          <a-descriptions-item label="耗时">
            {{ scanJob.elapsed_sec }}s
          </a-descriptions-item>
          <a-descriptions-item label="新增 / 更新" :span="2">
            <span class="mono">
              STRM {{ scanJob.created }} / {{ scanJob.updated }}
              （未变 {{ scanJob.unchanged }}）
            </span>
          </a-descriptions-item>
          <a-descriptions-item label="附件" :span="2">
            <span class="mono">
              新增 {{ scanJob.attachment_synced }} / 未变
              {{ scanJob.attachment_unchanged }} / 失败
              {{ scanJob.attachment_failed }}
            </span>
          </a-descriptions-item>
          <a-descriptions-item label="跳过" :span="2">
            <span class="mono">
              未知 {{ scanJob.skipped_unknown }} / 太小
              {{ scanJob.skipped_small }}
            </span>
          </a-descriptions-item>
          <a-descriptions-item label="正在处理" :span="2">
            <span class="mono path-line" :title="scanJob.current_path">
              {{ scanJob.current_path || "—" }}
            </span>
          </a-descriptions-item>
        </a-descriptions>

        <a-alert
          v-if="scanJob.state === 'failed'"
          type="error"
          :message="scanJob.error || '扫描失败'"
          show-icon
          style="margin-top: 12px"
        />

        <div class="scan-log">
          <div v-for="(line, i) in scanJob.logs" :key="i" class="log-line mono">
            {{ line }}
          </div>
        </div>

        <div style="margin-top: 12px; text-align: right">
          <a-space>
            <a-button
              v-if="scanJob.state === 'running'"
              size="small"
              @click="closeScan"
            >
              后台运行
            </a-button>
            <a-button v-else type="primary" size="small" @click="closeScan">
              关闭
            </a-button>
          </a-space>
        </div>
      </template>
      <a-empty v-else description="正在启动扫描…" />
    </a-modal>

    <!-- 登录方式选择 -->
    <a-modal
      v-model:open="loginOpen"
      title="登录存储源"
      :footer="null"
      :width="360"
    >
      <template v-if="loginDriver">
        <p class="hint">
          存储源「{{ loginDriver.name }}」（{{
            loginDriver.type === "guangya" ? "光鸭盘" : "115"
          }}）尚未登录。
        </p>
        <a-space direction="vertical" style="width: 100%">
          <a-button block type="primary" @click="onLoginByQr">
            扫码登录
          </a-button>
          <a-button
            v-if="loginDriver.type === 'guangya'"
            block
            @click="openSms(loginDriver)"
          >
            短信验证码登录
          </a-button>
        </a-space>
      </template>
    </a-modal>

    <!-- 短信验证码登录（光鸭盘）-->
    <a-modal
      v-model:open="smsOpen"
      title="短信验证码登录"
      :footer="null"
      :width="360"
    >
      <a-form layout="vertical">
        <a-form-item label="手机号">
          <a-input
            v-model:value="smsPhone"
            placeholder="+86 13800138000"
            class="mono"
          />
          <div class="hint">需含国际区号（+86 / +1 …）。</div>
        </a-form-item>
        <a-button
          :loading="smsSending"
          :disabled="!smsPhone"
          block
          @click="onSendSms"
        >
          发送验证码
        </a-button>
        <a-form-item v-if="smsToken" label="验证码">
          <a-input
            v-model:value="smsCode"
            placeholder="6 位数字"
            class="mono"
            @press-enter="onVerifySms"
          />
        </a-form-item>
      </a-form>
      <a-alert
        v-if="smsError"
        type="error"
        :message="smsError"
        show-icon
        style="margin-top: 12px"
      />
      <a-alert
        v-else-if="smsOk"
        type="success"
        message="登录成功，凭据已加密存储"
        show-icon
        style="margin-top: 12px"
      />
      <a-button
        v-if="smsToken && !smsOk"
        type="primary"
        block
        :loading="smsVerifying"
        :disabled="!smsCode"
        style="margin-top: 12px"
        @click="onVerifySms"
      >
        验证并登录
      </a-button>
    </a-modal>

    <!-- 扫码登录 -->
    <a-modal
      v-model:open="qrOpen"
      :title="`${qrDriverType === 'guangya' ? '光鸭盘' : '115'} 扫码登录`"
      :footer="null"
      :width="360"
    >
      <div v-if="qrImageSrc" class="qr-box">
        <img :src="qrImageSrc" alt="扫码登录" />
      </div>
      <a-empty v-else description="正在获取二维码…" />

      <a-alert
        v-if="qrError"
        type="error"
        :message="qrError"
        show-icon
        style="margin-top: 12px"
      />
      <a-alert
        v-else-if="qrKind === 'confirmed'"
        type="success"
        message="登录成功，凭据已加密存储"
        show-icon
        style="margin-top: 12px"
      />
      <a-alert
        v-else-if="qrKind === 'expired'"
        type="warning"
        message="二维码已过期，请重新获取"
        show-icon
        style="margin-top: 12px"
      />
      <p v-else class="hint">
        用 {{ qrDriverType === "guangya" ? "光鸭云盘" : "115" }} APP 扫描二维码。{{
          qrKind === "scanned" ? "已扫描，等待确认…" : ""
        }}
      </p>
      <template #footer>
        <a-space>
          <a-button
            v-if="!isQrCapable(configDriver?.device_type)"
            size="small"
            @click="openEditDeviceType"
          >
            修改设备类型
          </a-button>
          <a-button
            size="small"
            :disabled="!isQrCapable(configDriver?.device_type)"
            @click="openQr(configDriver!)"
          >
            重新获取二维码
          </a-button>
        </a-space>
      </template>
    </a-modal>

    <!-- 编辑设备类型 -->
    <a-modal
      v-model:open="editOpen"
      title="编辑存储源"
      :confirm-loading="saving"
      ok-text="保存"
      cancel-text="取消"
      @ok="onEdit"
    >
      <a-form layout="vertical">
        <a-form-item label="名称">
          <a-input
            :value="editForm.name ?? ''"
            @update:value="(v: string) => (editForm.name = v)"
          />
        </a-form-item>
        <a-form-item label="设备类型">
          <a-select v-model:value="editForm.device_type" :loading="deviceTypesLoading">
            <a-select-option
              v-for="d in deviceTypes"
              :key="d.value"
              :value="d.value"
              :disabled="!d.qr_capable"
            >
              {{ d.label }}
              <span v-if="!d.qr_capable" class="muted">（不支持扫码）</span>
            </a-select-option>
          </a-select>
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue"
import { message } from "ant-design-vue"
import { ApiError, errMsg, mediaApi as api } from "../../api"
import type {
  DeviceType,
  Driver,
  DriverConfig,
  DriverInput,
  DriverStatus,
  DriverUpdate,
  IncrementalJob,
  QrStartResult,
  QrStatusKind,
  QrStatusResult,
  ScanJob,
  SmsSendResult,
  SmsVerifyResult,
} from "../../types"
import { formatBytes } from "../../utils/format"
import DataTable from '../../components/DataTable.vue'
import PageHeader from '../../components/PageHeader.vue'

const drivers = ref<Driver[]>([])
const loading = ref(false)
const saving = ref(false)

/** 设备类型清单（**从后端取**——后端是单一真源，前端不手写）。 */
const deviceTypes = ref<DeviceType[]>([])
const deviceTypesLoading = ref(false)

const createOpen = ref(false)
const form = ref<DriverInput>({ type: "p115", name: "", device_type: "115android" })

/** 媒体库配置抽屉 */
const configOpen = ref(false)
const configDriver = ref<Driver | null>(null)
const configForm = ref<DriverConfig>({
  media_cid: "",
  media_path: "",
  output_dir: "",
  video_exts: [],
  image_exts: [],
  data_exts: [],
  min_size_mb: 0,
  include_subdirs: true,
})

/** 编辑存储源（名称 / 设备类型） */
const editOpen = ref(false)
const editId = ref<number | null>(null)
const editForm = ref<DriverUpdate>({ name: "", device_type: null })

const qrOpen = ref(false)
const qrDriverId = ref<number | null>(null)
const qrDriverType = ref("p115")
const qrSessionId = ref("")
const qrImage = ref("")
const qrKind = ref<QrStatusKind>("waiting")
const qrError = ref("")

// -- 登录方式选择 --
const loginOpen = ref(false)
const loginDriver = ref<Driver | null>(null)

// -- 短信登录（光鸭盘）--
const smsOpen = ref(false)
const smsPhone = ref("")
const smsCode = ref("")
const smsToken = ref("")
const smsError = ref("")
const smsOk = ref(false)
const smsSending = ref(false)
const smsVerifying = ref(false)

/** 各存储源的账号状态（测连结果）—— key = driver id */
const statuses = ref<Record<number, DriverStatus>>({})
const probingId = ref<number | null>(null)
const probingAll = ref(false)
const scanningId = ref<number | null>(null)

let pollTimer: number | undefined

const columns = [
  { title: "名称", key: "name" },
  { title: "账号", key: "account", width: 200 },
  { title: "媒体库", key: "library", width: 220 },
  { title: "本地目录", key: "output_dir", width: 180 },
  { title: "状态", key: "status", width: 100 },
  // 6 个操作按钮（测连/登录/配置/扫描/增量/删除）需要 ~323px——
  // 窄于这个宽度时按钮会被挤出行外，导致表格横向溢出。
  { title: "操作", key: "action", width: 340 },
]

const qrImageSrc = computed(() =>
  qrImage.value ? `data:image/png;base64,${qrImage.value}` : "",
)

// -- 状态读取 --

function statusOf(id: number): DriverStatus | undefined {
  return statuses.value[id]
}

function statusLabel(id: number): string {
  const s = statusOf(id)
  if (!s) return "未测连"
  switch (s.status) {
    case "ok":
      return s.can_get_link ? "正常" : "可用"
    case "needs_verify":
      return "待验证"
    case "no_credential":
      return "未登录"
    default:
      return "异常"
  }
}

function statusColor(id: number): string {
  const s = statusOf(id)
  if (!s) return "default"
  switch (s.status) {
    case "ok":
      return "green"
    case "needs_verify":
      return "orange"
    case "no_credential":
      return "default"
    default:
      return "red"
  }
}

function statusBadge(id: number): "success" | "warning" | "error" | "default" {
  const s = statusOf(id)
  if (!s) return "default"
  switch (s.status) {
    case "ok":
      return "success"
    case "needs_verify":
      return "warning"
    case "no_credential":
      return "default"
    default:
      return "error"
  }
}

function statusText(id: number): string {
  const s = statusOf(id)
  if (!s) return ""
  if (s.status === "ok") {
    return s.can_get_link ? "直链正常" : "可直链未验证"
  }
  return s.message
}

function usedPercent(id: number): number {
  const s = statusOf(id)
  if (!s || !s.total_bytes) return 0
  return Math.round((s.used_bytes / s.total_bytes) * 100)
}

function isQrCapable(value: string | null | undefined): boolean {
  if (!value) return false
  const found = deviceTypes.value.find((d) => d.value === value)
  return found ? found.qr_capable : true
}

// -- 加载 --

async function loadDeviceTypes(): Promise<void> {
  deviceTypesLoading.value = true
  try {
    deviceTypes.value = await api.get<DeviceType[]>("/drivers/device-types")
  } catch (e) {
    message.error(errMsg(e, "加载设备类型失败"))
  } finally {
    deviceTypesLoading.value = false
  }
}

async function load(): Promise<void> {
  loading.value = true
  try {
    drivers.value = await api.get<Driver[]>("/drivers")
    // 用后端缓存的测连状态初始化（启动自动测连 / 手动测连的结果）。
    // 没有缓存（后端刚重启还没测完）时保持空 → 显示「未测连」。
    const next: Record<number, DriverStatus> = {}
    for (const d of drivers.value) {
      if (d.conn) {
        next[d.id] = {
          logged_in: d.conn.logged_in,
          status: d.conn.status,
          message: d.conn.message,
          account_id: d.conn.account_id,
          total_bytes: d.conn.total_bytes,
          used_bytes: d.conn.used_bytes,
          remain_bytes: d.conn.remain_bytes,
          can_get_link: d.conn.can_get_link,
        }
      }
    }
    statuses.value = next
  } catch (e) {
    message.error(errMsg(e, "加载存储源失败"))
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void loadDeviceTypes()
  void load()
  // 页面刷新后**找回正在跑的后台任务**——否则关掉页面就看不到进度了，
  // 而后台任务其实还在跑。
  void resumeRunningJob()
})

/** 找回正在运行的扫描任务（刷新/重新进入页面时）。 */
async function resumeRunningJob(): Promise<void> {
  try {
    const jobs = await api.get<ScanJob[]>("/scan-jobs")
    const running = jobs.find((j) => j.state === "running")
    if (running) {
      scanJob.value = running
      scanOpen.value = true
      startScanPolling(running.id)
    }
  } catch {
    // 找回失败不打扰用户——可能本来就没有任务
  }
}

/** 该存储源是否正在扫描。 */
function isScanning(driverId: number): boolean {
  return scanJob.value?.state === "running" && scanJob.value.driver_id === driverId
}

// -- 新增 --

function openCreate(): void {
  form.value = { type: "p115", name: "", device_type: "115android" }
  createOpen.value = true
}

async function onCreate(): Promise<void> {
  saving.value = true
  try {
    await api.post<Driver>("/drivers", form.value)
    message.success("已创建")
    createOpen.value = false
    await load()
  } catch (e) {
    message.error(errMsg(e, "创建失败"))
  } finally {
    saving.value = false
  }
}

// -- 配置 --

function openConfig(driver: Driver): void {
  configDriver.value = driver
  configForm.value = {
    media_cid: driver.media_cid ?? "",
    media_path: driver.media_path ?? "",
    output_dir: driver.output_dir ?? "",
    video_exts: splitExts(driver.video_exts),
    image_exts: splitExts(driver.image_exts),
    data_exts: splitExts(driver.data_exts),
    min_size_mb: driver.min_size_mb ?? 0,
    include_subdirs: driver.include_subdirs ?? true,
  }
  configOpen.value = true
}

function splitExts(raw: string | null | undefined): string[] {
  if (!raw) return []
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
}

async function onSaveConfig(): Promise<void> {
  if (configDriver.value === null) return
  saving.value = true
  try {
    await api.put<Driver>(`/drivers/${configDriver.value.id}/config`, {
      media_cid: configForm.value.media_cid || null,
      output_dir: configForm.value.output_dir || null,
      video_exts: configForm.value.video_exts.join(","),
      image_exts: configForm.value.image_exts.join(","),
      data_exts: configForm.value.data_exts.join(","),
      min_size_mb: configForm.value.min_size_mb ?? 0,
      include_subdirs: configForm.value.include_subdirs ?? true,
    })
    message.success("已保存")
    configOpen.value = false
    await load()
  } catch (e) {
    message.error(errMsg(e, "保存失败"))
  } finally {
    saving.value = false
  }
}

// -- 扫描 --

/** 扫描进度弹窗 */
const scanOpen = ref(false)
const scanJob = ref<ScanJob | null>(null)
let scanTimer: number | undefined

async function onScan(driver: Driver): Promise<void> {
  scanningId.value = driver.id
  try {
    const res = await api.post<{ job_id: string }>(`/drivers/${driver.id}/scan`)
    scanOpen.value = true
    scanJob.value = null
    startScanPolling(res.job_id)
  } catch (e) {
    // 409 = 该存储源已有任务在跑 → 直接接管那个任务，而不是报错
    if (e instanceof ApiError && e.status === 409) {
      await resumeRunningJob()
      message.info("该存储源已有扫描任务，已切换到它的进度")
    } else {
      message.error(errMsg(e, "启动扫描失败"))
    }
  } finally {
    scanningId.value = null
  }
}

function startScanPolling(jobId: string): void {
  stopScanPolling()
  const tick = async (): Promise<void> => {
    try {
      const job = await api.get<ScanJob>(`/scan-jobs/${jobId}`)
      scanJob.value = job
      if (job.state !== "running") {
        stopScanPolling()
        // 完成/失败后刷新列表（last_scan_at / 条目数变了）
        await load()
      }
    } catch (e) {
      stopScanPolling()
      message.error(errMsg(e, "查询扫描进度失败"))
    }
  }
  void tick()
  scanTimer = window.setInterval(tick, 1500)
}

function stopScanPolling(): void {
  if (scanTimer !== undefined) {
    window.clearInterval(scanTimer)
    scanTimer = undefined
  }
}

function closeScan(): void {
  stopScanPolling()
  scanOpen.value = false
}

// -- 增量同步 --

/** 增量同步进度弹窗 */
const incrementalOpen = ref(false)
const incrementalJob = ref<IncrementalJob | null>(null)
const incrementalId = ref<number | null>(null)
let incrementalTimer: number | undefined

async function onIncremental(driver: Driver): Promise<void> {
  incrementalId.value = driver.id
  try {
    const res = await api.post<{ job_id: string }>(
      `/drivers/${driver.id}/incremental`,
    )
    incrementalOpen.value = true
    incrementalJob.value = null
    startIncrementalPolling(res.job_id)
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      message.info("该存储源已有增量任务在跑")
    } else {
      message.error(errMsg(e, "启动增量同步失败"))
    }
  } finally {
    incrementalId.value = null
  }
}

function startIncrementalPolling(jobId: string): void {
  stopIncrementalPolling()
  const tick = async (): Promise<void> => {
    try {
      const job = await api.get<IncrementalJob>(`/incremental-jobs/${jobId}`)
      incrementalJob.value = job
      if (job.state !== "running") {
        stopIncrementalPolling()
        await load()
        if (job.state === "success") {
          message.success(
            `增量完成：新增 ${job.created} / 更新 ${job.updated} / 删除 ${job.deleted}`,
          )
        }
      }
    } catch (e) {
      stopIncrementalPolling()
      message.error(errMsg(e, "查询增量进度失败"))
    }
  }
  void tick()
  incrementalTimer = window.setInterval(tick, 1500)
}

function stopIncrementalPolling(): void {
  if (incrementalTimer !== undefined) {
    window.clearInterval(incrementalTimer)
    incrementalTimer = undefined
  }
}

function closeIncremental(): void {
  stopIncrementalPolling()
  incrementalOpen.value = false
}

function scanStateLabel(state: string): string {
  switch (state) {
    case "running":
      return "扫描中"
    case "success":
      return "完成"
    case "cancelled":
      return "已取消"
    default:
      return "失败"
  }
}

function scanStateColor(state: string): string {
  switch (state) {
    case "running":
      return "processing"
    case "success":
      return "green"
    case "cancelled":
      return "default"
    default:
      return "red"
  }
}

// -- 测连 --

async function probeDriver(id: number, silent = false): Promise<void> {
  probingId.value = id
  try {
    const status = await api.get<DriverStatus>(`/drivers/${id}/probe`)
    statuses.value = { ...statuses.value, [id]: status }

    if (silent) return // 后台自动测连（扫码/短信登录后）不打扰
    switch (status.status) {
      case "ok":
        message.success(status.message)
        break
      case "needs_verify":
        message.warning(status.message)
        break
      case "no_credential":
        message.info(status.message)
        break
      default:
        message.error(status.message)
    }
  } catch (e) {
    message.error(errMsg(e, "测连失败"))
  } finally {
    probingId.value = null
  }
}

async function probeAll(): Promise<void> {
  probingAll.value = true
  try {
    for (const driver of drivers.value) {
      await probeDriver(driver.id)
    }
  } finally {
    probingAll.value = false
  }
}

// -- 编辑 --

function openEdit(device: Driver): void {
  editId.value = device.id
  editForm.value = { name: device.name, device_type: device.device_type }
  editOpen.value = true
}

function openEditDeviceType(): void {
  if (configDriver.value) openEdit(configDriver.value)
}

async function onEdit(): Promise<void> {
  if (editId.value === null) return
  saving.value = true
  try {
    await api.patch<Driver>(`/drivers/${editId.value}`, editForm.value)
    message.success("已保存")
    editOpen.value = false
    await load()
  } catch (e) {
    message.error(errMsg(e, "保存失败"))
  } finally {
    saving.value = false
  }
}

// -- 删除 --

async function onDelete(id: number): Promise<void> {
  try {
    await api.del(`/drivers/${id}`)
    message.success("已删除")
    await load()
  } catch (e) {
    message.error(errMsg(e, "删除失败"))
  }
}

// -- 登录 --

/** 打开登录方式选择。 */
function onLogin(driver: Driver): void {
  loginDriver.value = driver
  loginOpen.value = true
}

/** 用扫码方式登录。 */
function onLoginByQr(): void {
  const driver = loginDriver.value
  if (!driver) return
  loginOpen.value = false
  void openQr(driver)
}

/** 打开短信登录（光鸭盘）。 */
function openSms(_driver: Driver): void {
  loginOpen.value = false
  smsOpen.value = true
  smsPhone.value = ""
  smsCode.value = ""
  smsToken.value = ""
  smsError.value = ""
  smsOk.value = false
}

async function onSendSms(): Promise<void> {
  const driver = loginDriver.value
  if (!driver) return
  smsSending.value = true
  smsError.value = ""
  try {
    const res = await api.post<SmsSendResult>(
      `/drivers/${driver.id}/login/sms/send`,
      { phone: smsPhone.value },
    )
    if (res.captcha_url) {
      smsError.value = `需要人机验证，请先打开：${res.captcha_url}`
      return
    }
    smsToken.value = res.captcha_token
    message.success("验证码已发送")
  } catch (e) {
    smsError.value = errMsg(e, "发送验证码失败")
  } finally {
    smsSending.value = false
  }
}

async function onVerifySms(): Promise<void> {
  const driver = loginDriver.value
  if (!driver) return
  smsVerifying.value = true
  smsError.value = ""
  try {
    const res = await api.post<SmsVerifyResult>(
      `/drivers/${driver.id}/login/sms/verify`,
      {
        phone: smsPhone.value,
        code: smsCode.value,
        captcha_token: smsToken.value,
      },
    )
    if (!res.ok) {
      smsError.value = res.message || "验证失败"
      return
    }
    smsOk.value = true
    message.success("登录成功")
    await load()
    // 登录成功立刻后台测连 + 自动关闭短信弹框和登录选择框
    void probeDriver(driver.id, true)
    setTimeout(() => {
      smsOpen.value = false
      loginOpen.value = false
    }, 1200)
  } catch (e) {
    smsError.value = errMsg(e, "验证失败")
  } finally {
    smsVerifying.value = false
  }
}

// -- 扫码 --

async function openQr(driver: Driver): Promise<void> {
  qrDriverId.value = driver.id
  qrDriverType.value = driver.type
  qrSessionId.value = ""
  qrImage.value = ""
  qrKind.value = "waiting"
  qrError.value = ""
  qrOpen.value = true
  try {
    const res = await api.post<QrStartResult>(`/drivers/${driver.id}/qr/start`)
    qrSessionId.value = res.session_id
    qrImage.value = res.qr_image_b64
    startPolling()
  } catch (e) {
    qrError.value = errMsg(e, "获取二维码失败")
  }
}

function startPolling(): void {
  stopPolling()
  pollTimer = window.setInterval(async () => {
    if (!qrDriverId.value || !qrSessionId.value) return
    try {
      const res = await api.get<QrStatusResult>(
        `/drivers/${qrDriverId.value}/qr/status?session_id=${encodeURIComponent(qrSessionId.value)}`,
      )
      qrKind.value = res.kind
      if (res.error_kind) {
        qrError.value = res.error_msg ?? res.error_kind
        stopPolling()
        return
      }
      if (res.confirmed) {
        stopPolling()
        qrKind.value = "confirmed"
        message.success("扫码登录成功")
        await load()
        // 登录成功立刻后台测连（刷新连通/容量/直链状态）
        const id = qrDriverId.value
        if (id !== null) void probeDriver(id, true)
        // 停留 1.2 秒让用户看到成功提示，然后自动关闭
        setTimeout(() => {
          qrOpen.value = false
        }, 1200)
      }
    } catch (e) {
      stopPolling()
      const status = e instanceof ApiError ? e.status : 0
      if (status === 404) {
        qrKind.value = "expired"
        qrError.value = "扫码会话已结束，请重新获取二维码"
      } else {
        qrError.value = errMsg(e, "查询扫码状态失败")
      }
    }
  }, 2000)
}

function stopPolling(): void {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer)
    pollTimer = undefined
  }
}

onBeforeUnmount(stopPolling)
</script>

<style scoped>
.name-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}

.status-hint {
  color: #999;
  font-size: 12px;
  margin-top: 2px;
  margin-left: 20px;
}

.capacity {
  margin-top: 4px;
  min-width: 170px;
}

.hint {
  color: #999;
  font-size: 12px;
  margin-top: 4px;
}

.unit {
  margin-left: 8px;
  color: #888;
}

.qr-box {
  text-align: center;
}

.qr-box img {
  width: 220px;
  height: 220px;
  image-rendering: pixelated;
}

.scan-log {
  margin-top: 12px;
  max-height: 260px;
  overflow-y: auto;
  background: #fafafa;
  border: 1px solid #f0f0f0;
  border-radius: 4px;
  padding: 8px 10px;
}

.log-line {
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}

/* 「正在处理」可能是一条很长的网盘路径——单行省略，悬停看全文 */
.path-line {
  display: block;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  font-size: 12px;
}
</style>
