<template>
  <div class="page">
    <!-- 顶部：连接信息 + 根目录配置 -->
    <a-card size="small" class="mb">
      <template #title>WebDAV</template>
      <template #extra>
        <a-space>
          <a-tag color="green">只读</a-tag>
          <a-button size="small" @click="loadAll">刷新</a-button>
        </a-space>
      </template>

      <a-descriptions :column="1" size="small" bordered>
        <a-descriptions-item label="连接地址">
          <div class="mono">{{ davUrl }}</div>
          <div class="muted">
            协议选 <b>WebDAV</b>，地址填上面这串，用户名/密码用下方账号。
          </div>
        </a-descriptions-item>
        <a-descriptions-item label="媒体库根目录">
          <a-space>
            <a-select
              v-model:value="rootDriverId"
              style="min-width: 260px"
              :options="rootOptions"
              @change="onRootChange"
            />
            <a-button
              size="small"
              type="primary"
              :loading="savingRoot"
              :disabled="rootDriverId === undefined"
              @click="saveRoot"
            >
              应用
            </a-button>
          </a-space>
          <div class="muted">
            DAV 暴露的是「同步下来的媒体库目录」——<code>.strm</code> + 海报 +
            nfo + 字幕，客户端拿到的就是与 Emby 挂载的同一棵树。
          </div>
        </a-descriptions-item>
      </a-descriptions>

      <a-alert
        v-if="!rootUsable"
        type="warning"
        show-icon
        class="mt"
        message="当前没有可用的 DAV 根目录"
        description="存储源需要先配置「本地输出目录」（output_dir），且该目录必须存在。否则 DAV 请求会返回 404。"
      />
    </a-card>

    <a-tabs v-model:activeKey="activeTab">
      <!-- 账号 -->
      <a-tab-pane key="users" tab="账号">
        <a-card size="small">
          <template #title>WebDAV 账号</template>
          <template #extra>
            <a-button type="primary" size="small" @click="openCreate">新增账号</a-button>
          </template>

          <a-table
            :data-source="users"
            :columns="userColumns"
            row-key="id"
            size="small"
            :loading="loadingUsers"
            :pagination="false"
            :locale="{ emptyText: '暂无账号' }"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'username'">
                <span class="mono">{{ record.username }}</span>
              </template>

              <template v-else-if="column.key === 'is_active'">
                <a-tag :color="record.is_active ? 'green' : 'default'">
                  {{ record.is_active ? "启用" : "停用" }}
                </a-tag>
              </template>

              <template v-else-if="column.key === 'last_login_at'">
                <span class="muted">{{ formatTime(record.last_login_at) }}</span>
              </template>

              <template v-else-if="column.key === 'action'">
                <a-space :size="4">
                  <a-button size="small" @click="openReset(record)">重置密码</a-button>
                  <a-button size="small" @click="toggleActive(record)">
                    {{ record.is_active ? "停用" : "启用" }}
                  </a-button>
                  <a-popconfirm
                    title="删除该账号？"
                    description="删除后审计记录仍保留，但该账号无法再登录。"
                    ok-text="删除"
                    cancel-text="取消"
                    @confirm="removeUser(record)"
                  >
                    <a-button size="small" danger>删除</a-button>
                  </a-popconfirm>
                </a-space>
              </template>
            </template>
          </a-table>
        </a-card>
      </a-tab-pane>

      <!-- 审计 -->
      <a-tab-pane key="audit" tab="审计日志">
        <a-card size="small">
          <template #title>审计日志</template>
          <template #extra>
            <a-space>
              <a-button size="small" @click="loadAudit">刷新</a-button>
              <a-popconfirm
                title="清空全部审计日志？"
                ok-text="清空"
                cancel-text="取消"
                @confirm="clearAudit"
              >
                <a-button size="small" danger>清空</a-button>
              </a-popconfirm>
            </a-space>
          </template>

          <a-space class="mb" wrap>
            <a-input
              v-model:value="auditFilters.username"
              placeholder="用户名"
              style="width: 160px"
              allow-clear
            />
            <a-select
              v-model:value="auditFilters.method"
              placeholder="方法"
              style="width: 130px"
              allow-clear
              :options="methodOptions"
            />
            <a-button size="small" type="primary" @click="loadAudit">筛选</a-button>
            <span class="muted">共 {{ auditTotal }} 条</span>
          </a-space>

          <a-table
            :data-source="auditItems"
            :columns="auditColumns"
            row-key="id"
            size="small"
            :loading="loadingAudit"
            :pagination="false"
            :locale="{ emptyText: '暂无审计记录' }"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'username'">
                <span class="mono">{{ record.username || "—" }}</span>
              </template>

              <template v-else-if="column.key === 'method'">
                <a-tag :color="methodColor(record.method)">{{ record.method }}</a-tag>
              </template>

              <template v-else-if="column.key === 'path'">
                <a-tooltip :title="record.path">
                  <span class="mono truncate" :class="{ traversal: isTraversal(record.path) }">
                    {{ isTraversal(record.path) ? "路径穿越尝试" : record.path }}
                  </span>
                </a-tooltip>
              </template>

              <template v-else-if="column.key === 'status'">
                <a-tag :color="statusColor(record.status)">{{ record.status }}</a-tag>
              </template>

              <template v-else-if="column.key === 'client'">
                <span class="truncate" style="display: inline-block; max-width: 180px">
                  {{ record.client || "—" }}
                </span>
              </template>

              <template v-else-if="column.key === 'remote_addr'">
                <span class="mono">{{ record.remote_addr || "—" }}</span>
              </template>

              <template v-else-if="column.key === 'duration_ms'">
                <span class="muted">{{ record.duration_ms }} ms</span>
              </template>

              <template v-else-if="column.key === 'created_at'">
                <span class="muted">{{ formatTime(record.created_at) }}</span>
              </template>
            </template>
          </a-table>
        </a-card>
      </a-tab-pane>

      <!-- 客户端配置 -->
      <a-tab-pane key="help" tab="客户端配置">
        <a-card size="small">
          <template #title>客户端配置</template>
          <a-descriptions :column="1" size="small" bordered>
            <a-descriptions-item label="Infuse">
              <div class="mono">设置 → 网络 → 文件夹 → 添加 → WebDAV</div>
              <div class="muted">地址填上面的连接地址，协议选 WebDAV</div>
            </a-descriptions-item>
            <a-descriptions-item label="网易爆米花">
              <div class="mono">设置 → 媒体库 → 添加 → WebDAV</div>
              <div class="muted">用户名/密码用下方账号</div>
            </a-descriptions-item>
            <a-descriptions-item label="Finder（macOS）">
              <div class="mono">前往 → 连接服务器 → smb://… 改成 http://…</div>
              <div class="muted">Finder 只支持 http/https 的 WebDAV</div>
            </a-descriptions-item>
            <a-descriptions-item label="文件管理器（Android）">
              <div class="mono">支持 WebDAV 的客户端填地址 + 账号密码即可</div>
            </a-descriptions-item>
          </a-descriptions>

          <a-alert
            type="info"
            show-icon
            class="mt"
            message="媒体流量不经过本服务"
            description="DAV 只返回 .strm 文本（几 KB）。播放器读到 .strm 里的中转 URL 后，GB 级流量走播放器 ↔ 网盘 CDN 直连。"
          />

          <a-alert
            type="warning"
            show-icon
            class="mt"
            message="外网访问需注意"
            description="STRM 文件里写的是 PUBLIC_BASE_URL。若它是局域网地址，外网播放器拿到的 URL 访问不到 —— 需改成公网可达地址并重新签名。"
          />
        </a-card>
      </a-tab-pane>
    </a-tabs>

    <!-- 新建账号 -->
    <a-modal
      v-model:open="createOpen"
      title="新增 WebDAV 账号"
      ok-text="创建"
      cancel-text="取消"
      :confirm-loading="saving"
      @ok="createUser"
    >
      <a-form layout="vertical">
        <a-form-item label="用户名" required>
          <a-input v-model:value="createForm.username" placeholder="如 infuse" />
        </a-form-item>
        <a-form-item label="密码" required>
          <a-input-password v-model:value="createForm.password" placeholder="至少 1 位" />
        </a-form-item>
      </a-form>
      <a-alert
        type="info"
        show-icon
        message="账号独立于面板账号"
        description="WebDAV 账号只能访问媒体库，不能打开管理面板。"
      />
    </a-modal>

    <!-- 重置密码 -->
    <a-modal
      v-model:open="resetOpen"
      :title="`重置密码：${resetTarget?.username ?? ''}`"
      ok-text="确认"
      cancel-text="取消"
      :confirm-loading="saving"
      @ok="resetPassword"
    >
      <a-form layout="vertical">
        <a-form-item label="新密码" required>
          <a-input-password v-model:value="resetForm.password" placeholder="至少 1 位" />
        </a-form-item>
      </a-form>
      <a-alert
        type="info"
        show-icon
        message="旧密码立即失效"
        description="重置后会清空认证缓存，旧密码马上就不能用了。"
      />
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue"
import { message } from "ant-design-vue"
import { errMsg, mediaApi as api } from "../../api"
import type {
  DavAuditItem,
  DavAuditPage,
  DavRootConfig,
  DavRootDriver,
  DavUser,
} from "../../types"

const activeTab = ref("users")

// ---- 账号 ----
const users = ref<DavUser[]>([])
const loadingUsers = ref(false)
const saving = ref(false)

const userColumns = [
  { title: "用户名", key: "username" },
  { title: "状态", key: "is_active", width: 90 },
  { title: "上次登录", key: "last_login_at", width: 180 },
  { title: "操作", key: "action", width: 260 },
]

// ---- 审计 ----
const auditItems = ref<DavAuditItem[]>([])
const auditTotal = ref(0)
const loadingAudit = ref(false)
const auditFilters = reactive<{ username?: string; method?: string }>({})

const methodOptions = [
  { value: "GET", label: "GET" },
  { value: "HEAD", label: "HEAD" },
  { value: "PROPFIND", label: "PROPFIND" },
  { value: "OPTIONS", label: "OPTIONS" },
]

const auditColumns = [
  { title: "用户", key: "username", width: 120 },
  { title: "方法", key: "method", width: 100 },
  { title: "路径", key: "path" },
  { title: "状态", key: "status", width: 80 },
  { title: "客户端", key: "client", width: 200 },
  { title: "来源 IP", key: "remote_addr", width: 130 },
  { title: "耗时", key: "duration_ms", width: 90 },
  { title: "时间", key: "created_at", width: 180 },
]

// ---- 根目录 ----
const rootConfig = ref<DavRootConfig>({ driver_id: null, drivers: [] })
const rootDriverId = ref<number | undefined>(undefined)
const savingRoot = ref(false)

// ---- 弹窗 ----
const createOpen = ref(false)
const createForm = reactive({ username: "", password: "" })
const resetOpen = ref(false)
const resetTarget = ref<DavUser | null>(null)
const resetForm = reactive({ password: "" })

// ---- 计算属性 ----

/** DAV 连接地址（用浏览器当前 origin，部署到哪就是哪）。 */
const davUrl = computed(() => `${window.location.origin}/dav`)

const rootOptions = computed(() => {
  const opts = rootConfig.value.drivers.map((d: DavRootDriver) => ({
    value: d.id,
    label: d.usable ? d.name : `${d.name}（未配置输出目录）`,
    disabled: !d.usable,
  }))
  return [{ value: -1, label: "自动（第一个可用）" }, ...opts]
})

const rootUsable = computed(() => {
  const id = rootDriverId.value
  if (id === undefined || id === -1) {
    return rootConfig.value.drivers.some((d) => d.usable)
  }
  return rootConfig.value.drivers.find((d) => d.id === id)?.usable ?? false
})

// ---- 加载 ----

async function loadUsers() {
  loadingUsers.value = true
  try {
    users.value = await api.get<DavUser[]>("/dav-users")
  } catch (e) {
    message.error(`加载账号失败：${errMsg(e)}`)
  } finally {
    loadingUsers.value = false
  }
}

async function loadAudit() {
  loadingAudit.value = true
  try {
    const params = new URLSearchParams()
    if (auditFilters.username) params.set("username", auditFilters.username)
    if (auditFilters.method) params.set("method", auditFilters.method)
    const qs = params.toString()
    const page = await api.get<DavAuditPage>(`/dav-users/audit${qs ? `?${qs}` : ""}`)
    auditItems.value = page.items
    auditTotal.value = page.total
  } catch (e) {
    message.error(`加载审计失败：${errMsg(e)}`)
  } finally {
    loadingAudit.value = false
  }
}

async function loadRoot() {
  try {
    rootConfig.value = await api.get<DavRootConfig>("/dav-users/root")
    rootDriverId.value = rootConfig.value.driver_id ?? -1
  } catch (e) {
    message.error(`加载根目录配置失败：${errMsg(e)}`)
  }
}

async function loadAll() {
  await Promise.all([loadUsers(), loadAudit(), loadRoot()])
}

// ---- 操作 ----

function openCreate() {
  createForm.username = ""
  createForm.password = ""
  createOpen.value = true
}

async function createUser() {
  if (!createForm.username || !createForm.password) {
    message.warning("用户名和密码都要填")
    return
  }
  saving.value = true
  try {
    await api.post("/dav-users", {
      username: createForm.username,
      password: createForm.password,
    })
    message.success("账号已创建")
    createOpen.value = false
    await loadUsers()
  } catch (e) {
    message.error(`创建失败：${errMsg(e)}`)
  } finally {
    saving.value = false
  }
}

function openReset(user: DavUser) {
  resetTarget.value = user
  resetForm.password = ""
  resetOpen.value = true
}

async function resetPassword() {
  if (!resetTarget.value) return
  if (!resetForm.password) {
    message.warning("密码不能为空")
    return
  }
  saving.value = true
  try {
    await api.patch(`/dav-users/${resetTarget.value.id}`, { password: resetForm.password })
    message.success("密码已重置，旧密码立即失效")
    resetOpen.value = false
  } catch (e) {
    message.error(`重置失败：${errMsg(e)}`)
  } finally {
    saving.value = false
  }
}

async function toggleActive(user: DavUser) {
  try {
    await api.patch(`/dav-users/${user.id}`, { is_active: !user.is_active })
    message.success(user.is_active ? "已停用" : "已启用")
    await loadUsers()
  } catch (e) {
    message.error(`操作失败：${errMsg(e)}`)
  }
}

async function removeUser(user: DavUser) {
  try {
    await api.del(`/dav-users/${user.id}`)
    message.success("账号已删除")
    await loadUsers()
  } catch (e) {
    message.error(`删除失败：${errMsg(e)}`)
  }
}

async function clearAudit() {
  try {
    await api.del("/dav-users/audit")
    message.success("审计日志已清空")
    await loadAudit()
  } catch (e) {
    message.error(`清空失败：${errMsg(e)}`)
  }
}

function onRootChange() {
  // 变更只是选中，真正生效点「应用」
}

async function saveRoot() {
  const id = rootDriverId.value
  savingRoot.value = true
  try {
    rootConfig.value = await api.put<DavRootConfig>("/dav-users/root", {
      driver_id: id === undefined || id === -1 ? null : id,
    })
    rootDriverId.value = rootConfig.value.driver_id ?? -1
    message.success("已应用")
  } catch (e) {
    message.error(`应用失败：${errMsg(e)}`)
  } finally {
    savingRoot.value = false
  }
}

// ---- 工具 ----

function formatTime(ts: string | null | undefined): string {
  if (!ts) return "从未"
  return ts
}

function methodColor(method: string): string {
  switch (method) {
    case "GET":
      return "blue"
    case "PROPFIND":
      return "purple"
    case "HEAD":
      return "cyan"
    default:
      return "default"
  }
}

function statusColor(status: number): string {
  if (status >= 200 && status < 300) return "green"
  if (status === 401 || status === 403) return "orange"
  if (status === 429) return "red"
  return "default"
}

/** 路径穿越尝试（含 `..` 的路径）。 */
function isTraversal(path: string): boolean {
  return path.includes("..")
}

onMounted(loadAll)
</script>

<style scoped>
.mb {
  margin-bottom: 12px;
}
.mt {
  margin-top: 12px;
}
.traversal {
  color: #cf1322;
  font-weight: 600;
}
</style>
