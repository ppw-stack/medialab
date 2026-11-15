<template>
  <div class="page">
    <PageHeader
      eyebrow="媒体库 · MEDIA"
      title="网盘聚合"
      sub="主数据源为准，副数据源缓冲——归拢完成后自动清理副盘副本"
      compact
    >
      <template #actions>
        <a-button size="small" :loading="loading" @click="loadAll">刷新</a-button>
        <a-button type="primary" size="small" @click="openCreateGroup">新建聚合组</a-button>
      </template>
    </PageHeader>

    <a-alert type="info" show-icon :closable="false" class="tip">
      <template #message>
        一个聚合组 = <b>1 个主数据源</b> + N 个副数据源。主盘是唯一真源（媒体库扫描 / STRM /
        播放都只认它）；副盘是<b>缓冲</b>——放进副盘缓冲目录的文件会被登记成在途任务，
        后台搬回主盘，主盘确认落盘后<b>自动清理副盘副本</b>（进回收站，可恢复）。
        副盘内容<b>永不对外展示</b>。
      </template>
    </a-alert>

    <a-alert type="warning" show-icon :closable="false" class="tip">
      <template #message>
        归拢速度受<b>云添加额度</b>限制：光鸭实测约 <b>{{ dailyQuota }} 次/账号/天</b>，
        且<b>失败也扣</b>。额度耗尽时任务转「暂停」，<b>不硬撞不重试</b>，
        次日额度窗口打开时自动续传。
        <br />
        迁移与归拢都<b>串行</b>提交（不支持并发）——并发会让云下载落盘名撞车
        （<span class="mono">download(1)</span>），且更快撞上额度墙。
      </template>
    </a-alert>

    <!-- 聚合组 -->
    <a-card v-for="group in groups" :key="group.id" size="small" class="mt">
      <template #title>
        <a-space>
          <span>{{ group.name }}</span>
          <a-tag :color="group.enabled ? 'green' : 'default'">
            {{ group.enabled ? "启用" : "停用" }}
          </a-tag>
        </a-space>
      </template>
      <template #extra>
        <a-space>
          <a-tooltip title="扫描副盘缓冲目录，把里面的文件登记成在途任务（幂等）">
            <a-button size="small" :loading="busyGroupId === group.id" @click="enqueue(group.id)">
              登记在途
            </a-button>
          </a-tooltip>
          <a-tooltip title="立刻跑一轮归拢（受当日云添加额度限制）">
            <a-button size="small" :loading="syncingGroupId === group.id" @click="syncNow(group.id)">
              立即归拢
            </a-button>
          </a-tooltip>
          <a-switch
            :checked="group.enabled"
            size="small"
            @change="(v: boolean) => toggleGroup(group, v)"
          />
          <a-popconfirm
            title="解散该聚合组？"
            ok-text="解散"
            cancel-text="取消"
            @confirm="removeGroup(group)"
          >
            <a-button size="small" danger>解散</a-button>
          </a-popconfirm>
        </a-space>
      </template>

      <a-row :gutter="12">
        <a-col :span="10">
          <div class="section">组内存储源</div>
          <div v-if="driversOf(group).length === 0" class="muted">尚未指派存储源</div>
          <div v-for="d in driversOf(group)" :key="d.id" class="member">
            <a-tag :color="d.role === 'master' ? 'blue' : 'orange'">
              {{ d.role === "master" ? "主" : "副" }}
            </a-tag>
            <span class="member-name">{{ d.name }}</span>
            <a-button size="small" danger @click="detach(d)">移出</a-button>
          </div>
          <a-button size="small" class="mt-sm" @click="openAssign(group)">
            指派存储源
          </a-button>
        </a-col>

        <a-col :span="14">
          <div class="section">水位</div>
          <a-descriptions :column="2" size="small" bordered>
            <a-descriptions-item label="主数据源">
              {{ masterName(group) }}
            </a-descriptions-item>
            <a-descriptions-item label="待归拢">
              {{ pendingCount(group) }} 个 / {{ formatBytes(pendingBytes(group)) }}
            </a-descriptions-item>
            <a-descriptions-item label="失败 / 暂停">
              {{ countOf(group, "failed") }} / {{ countOf(group, "paused") }}
            </a-descriptions-item>
          </a-descriptions>
        </a-col>
      </a-row>
    </a-card>

    <a-card v-if="groups.length === 0" size="small" class="mt">
      <a-empty description="还没有聚合组" />
    </a-card>

    <!-- 在途台账 -->
    <a-card size="small" class="mt">
      <template #title>在途任务（副盘 → 主盘）</template>
      <DataTable
        :data-source="staging"
        :columns="stagingColumns"
        row-key="id"
        size="small"
        table-layout="fixed"
        :loading="loading"
        :pagination="false"
        :locale="{ emptyText: '暂无在途任务' }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'name'">
            <div class="mono truncate" :title="record.file_name">
              {{ record.file_name }}
            </div>
            <div v-if="record.last_error" class="muted truncate" :title="record.last_error">
              {{ record.last_error }}
            </div>
          </template>

          <template v-else-if="column.key === 'size'">
            <span class="mono">{{ formatBytes(record.size_bytes) }}</span>
          </template>

          <template v-else-if="column.key === 'state'">
            <a-tag :color="stateColor(record.state)">{{ stateLabel(record.state) }}</a-tag>
          </template>

          <template v-else-if="column.key === 'action'">
            <a-space :size="4">
              <a-button size="small" @click="retry(record.id)">重试</a-button>
              <a-popconfirm
                title="删除这条在途记录？（不动网盘上的文件）"
                ok-text="删除"
                cancel-text="取消"
                @confirm="removeStaging(record.id)"
              >
                <a-button size="small" danger>删除</a-button>
              </a-popconfirm>
            </a-space>
          </template>
        </template>
      </DataTable>
    </a-card>

    <!-- 新建聚合组 -->
    <a-modal
      v-model:open="createOpen"
      title="新建聚合组"
      :confirm-loading="saving"
      ok-text="创建"
      cancel-text="取消"
      @ok="createGroup"
    >
      <a-form layout="vertical">
        <a-form-item label="名称">
          <a-input v-model:value="groupName" placeholder="例如：主号 + 两个副号" />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- 指派存储源 -->
    <a-modal
      v-model:open="assignOpen"
      title="指派存储源"
      :confirm-loading="saving"
      ok-text="指派"
      cancel-text="取消"
      @ok="assign"
    >
      <a-form layout="vertical">
        <a-form-item label="存储源">
          <a-select v-model:value="assignDriverId" placeholder="选择存储源">
            <a-select-option v-for="d in candidates" :key="d.id" :value="d.id">
              {{ d.name }}（{{ d.type }}）
            </a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item label="角色">
          <a-radio-group v-model:value="assignRole">
            <a-radio value="master">主数据源（唯一真源）</a-radio>
            <a-radio value="secondary">副数据源（缓冲）</a-radio>
          </a-radio-group>
          <div class="hint">
            副数据源需要在「存储源 → 配置」里设置<b>缓冲根目录</b>，
            放进那个目录的文件才会被登记成在途任务。
          </div>
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue"
import { message } from "ant-design-vue"
import DataTable from "../../components/DataTable.vue"
import PageHeader from "../../components/PageHeader.vue"
import { errMsg, mediaApi as api } from "../../api"
import type {
  AggregateGroup,
  AggregateGroupStats,
  AggregateSyncJob,
  Driver,
  StagingRow,
} from "../../types"
import { formatBytes } from "../../utils/format"

const groups = ref<AggregateGroup[]>([])
const stats = ref<AggregateGroupStats[]>([])
const drivers = ref<Driver[]>([])
const staging = ref<StagingRow[]>([])
/** 服务端常量（单一真源）：光鸭云添加每日额度 */
const dailyQuota = ref(3)

const loading = ref(false)
const saving = ref(false)
const busyGroupId = ref<number | null>(null)
const syncingGroupId = ref<number | null>(null)

const createOpen = ref(false)
const groupName = ref("")
const assignOpen = ref(false)
const assignGroupId = ref<number | null>(null)
const assignDriverId = ref<number | undefined>(undefined)
const assignRole = ref<string>("secondary")

const stagingColumns = [
  { title: "文件", dataIndex: "file_name", key: "name", ellipsis: true },
  { title: "大小", key: "size", width: 110 },
  { title: "状态", key: "state", width: 100 },
  { title: "操作", key: "action", width: 150 },
]

/** 组内存储源（主盘在前）。 */
function driversOf(group: AggregateGroup): Driver[] {
  const members = drivers.value.filter((d) => d.aggregate_group_id === group.id)
  return members.sort((a, b) => {
    if (a.role === b.role) return a.id - b.id
    return a.role === "master" ? -1 : 1
  })
}

/** 可指派的存储源（未入组的优先，已入组的也能换组）。 */
const candidates = computed(() => drivers.value.filter((d) => d.type === "guangya"))

function statsOf(groupId: number): AggregateGroupStats | undefined {
  return stats.value.find((s) => s.group_id === groupId)
}

function masterName(group: AggregateGroup): string {
  const master = driversOf(group).find((d) => d.role === "master")
  return master?.name ?? "未设置"
}

function countOf(group: AggregateGroup, state: string): number {
  return statsOf(group.id)?.counts?.[state] ?? 0
}

function pendingCount(group: AggregateGroup): number {
  const counts = statsOf(group.id)?.counts ?? {}
  return (
    (counts["pending"] ?? 0) +
    (counts["syncing"] ?? 0) +
    (counts["paused"] ?? 0) +
    (counts["verified"] ?? 0)
  )
}

function pendingBytes(group: AggregateGroup): number {
  return statsOf(group.id)?.pending_bytes ?? 0
}

function stateLabel(state: string): string {
  switch (state) {
    case "pending":
      return "待归拢"
    case "syncing":
      return "归拢中"
    case "verified":
      return "待清理"
    case "done":
      return "已完成"
    case "failed":
      return "失败"
    case "paused":
      return "已暂停"
    default:
      return state
  }
}

function stateColor(state: string): string {
  switch (state) {
    case "pending":
      return "blue"
    case "syncing":
      return "processing"
    case "verified":
      return "cyan"
    case "done":
      return "green"
    case "failed":
      return "red"
    case "paused":
      return "orange"
    default:
      return "default"
  }
}

async function loadAll() {
  loading.value = true
  try {
    // 核心数据一起拉；limits 是辅助展示，**单独拉、失败降级**——
    // 别让一个常量接口失败把整页拖垮。
    const [g, s, d, st] = await Promise.all([
      api.get<AggregateGroup[]>("/aggregate/groups"),
      api.get<AggregateGroupStats[]>("/aggregate/stats"),
      api.get<Driver[]>("/drivers"),
      api.get<StagingRow[]>("/aggregate/staging"),
    ])
    groups.value = g
    stats.value = s
    drivers.value = d
    staging.value = st
    try {
      const lim = await api.get<{ daily_cloud_add_quota: number }>(
        "/aggregate/limits",
      )
      dailyQuota.value = lim.daily_cloud_add_quota
    } catch {
      // 降级：保持默认值 3（文案仍正确，只是数字可能过时）
    }
  } catch (e) {
    message.error(errMsg(e, "加载聚合数据失败"))
  } finally {
    loading.value = false
  }
}

function openCreateGroup() {
  groupName.value = ""
  createOpen.value = true
}

async function createGroup() {
  saving.value = true
  try {
    await api.post("/aggregate/groups", { name: groupName.value })
    createOpen.value = false
    message.success("已创建聚合组")
    await loadAll()
  } catch (e) {
    message.error(errMsg(e, "创建聚合组失败"))
  } finally {
    saving.value = false
  }
}

async function toggleGroup(group: AggregateGroup, enabled: boolean) {
  try {
    await api.patch(`/aggregate/groups/${group.id}`, { enabled })
    await loadAll()
  } catch (e) {
    message.error(errMsg(e, "修改聚合组失败"))
  }
}

async function removeGroup(group: AggregateGroup) {
  try {
    await api.del(`/aggregate/groups/${group.id}`)
    message.success("已解散聚合组")
    await loadAll()
  } catch (e) {
    message.error(errMsg(e, "解散聚合组失败"))
  }
}

function openAssign(group: AggregateGroup) {
  assignGroupId.value = group.id
  assignDriverId.value = undefined
  assignRole.value = "secondary"
  assignOpen.value = true
}

async function assign() {
  if (!assignDriverId.value || !assignGroupId.value) {
    message.warning("请选择存储源")
    return
  }
  saving.value = true
  try {
    await api.put(`/aggregate/drivers/${assignDriverId.value}/role`, {
      group_id: assignGroupId.value,
      role: assignRole.value,
    })
    assignOpen.value = false
    message.success("已指派")
    await loadAll()
  } catch (e) {
    message.error(errMsg(e, "指派失败"))
  } finally {
    saving.value = false
  }
}

async function detach(driver: Driver) {
  try {
    await api.del(`/aggregate/drivers/${driver.id}/role`)
    message.success("已移出聚合组")
    await loadAll()
  } catch (e) {
    message.error(errMsg(e, "移出失败"))
  }
}

async function enqueue(groupId: number) {
  busyGroupId.value = groupId
  try {
    const result = await api.post<{ scanned: number; added: number; skipped: number }>(
      `/aggregate/groups/${groupId}/enqueue`,
    )
    message.success(
      `扫描 ${result.scanned} 个文件，新登记 ${result.added} 个，已存在 ${result.skipped} 个`,
    )
    await loadAll()
  } catch (e) {
    message.error(errMsg(e, "登记在途任务失败"))
  } finally {
    busyGroupId.value = null
  }
}

/**
 * 手动触发归拢：**立即返回 job_id**，前端每 2s 轮询到结束。
 *
 * 为什么轮询：单文件传输要几分钟，同步请求会被网关 60s 超时掐断
 * （与「存储源扫描」同款处理）。
 */
async function syncNow(groupId: number) {
  syncingGroupId.value = groupId
  try {
    const ref = await api.post<{ job_id: string; state: string }>(
      `/aggregate/groups/${groupId}/sync`,
    )
    const jobId = ref.job_id
    for (;;) {
      await new Promise((r) => setTimeout(r, 2000))
      const job = await api.get<AggregateSyncJob>(`/aggregate/sync-jobs/${jobId}`)
      if (job.state === "success") {
        message.success(
          `归拢完成：成功 ${job.synced} / 清理 ${job.cleaned} / 暂停 ${job.paused} / 失败 ${job.failed} / 跳过 ${job.skipped}`,
        )
        break
      }
      if (job.state === "failed") {
        message.error(errMsg(job.error || "归拢失败", "归拢失败"))
        break
      }
      if (job.state === "cancelled") {
        message.warning("归拢已取消")
        break
      }
    }
    await loadAll()
  } catch (e) {
    message.error(errMsg(e, "归拢失败"))
  } finally {
    syncingGroupId.value = null
  }
}

async function retry(id: number) {
  try {
    await api.post(`/aggregate/staging/${id}/retry`)
    message.success("已重置为待归拢")
    await loadAll()
  } catch (e) {
    message.error(errMsg(e, "重试失败"))
  }
}

async function removeStaging(id: number) {
  try {
    await api.del(`/aggregate/staging/${id}`)
    await loadAll()
  } catch (e) {
    message.error(errMsg(e, "删除失败"))
  }
}

onMounted(loadAll)
</script>

<style scoped>
.tip {
  margin-bottom: 8px;
}

.section {
  font-size: 12px;
  color: var(--muted, #888);
  margin-bottom: 6px;
}

.member {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
}

.member-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.muted {
  color: var(--muted, #888);
  font-size: 12px;
}

.hint {
  font-size: 12px;
  color: var(--muted, #888);
  margin-top: 4px;
}

.truncate {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mono {
  font-family: var(--mono, monospace);
}

.mt {
  margin-top: 12px;
}

.mt-sm {
  margin-top: 8px;
}
</style>
