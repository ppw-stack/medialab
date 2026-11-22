<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { api, errMsg } from '../api'
import type {
  JobRunItem,
  JobRunPage,
  RunResult,
  SchedulerInfo,
  SchedulerJob,
} from '../types'
import { message } from 'ant-design-vue'
import SchedulerConfigModal from '../components/SchedulerConfigModal.vue'
import SchedulerHistoryModal from '../components/SchedulerHistoryModal.vue'
import PageHeader from '../components/PageHeader.vue'

/**
 * 定时任务页（原「设置 → 调度作业」tab，2026-11-22 拆成独立菜单）。
 *
 * **按业务域分成「辅种」「媒体库」两栏**：两个域各有自己的周期性作业，
 * 混在一张清单里时要在一堆不关心的作业里找目标那一个；堆在一页又会拉成
 * 2-3 屏的长列表（11 个作业实测 2750px）。故一个域一个 tab，一屏看完。
 *
 * 分组数据全部由后端下发（``jobs[].module`` + ``modules``）——前端不硬编码
 * job → 域的映射，也不自己排域的顺序（两处顺序约定必然漂移）。
 */

const data = ref<SchedulerInfo | null>(null)
const runningJob = ref<string | null>(null)

const runsByJob = ref<Record<string, JobRunItem[]>>({})

// 配置 / 历史 modal 状态
const configOpen = ref(false)
const configJobName = ref('')
const historyOpen = ref(false)
const historyJobName = ref('')

/**
 * 作业的中文名 + 描述**由后端下发**（`JobSpec.title` / `description`）。
 *
 * 以前这里是前端硬编码的映射表——每加一个作业就要手改前端，漏一处就
 * 在调度页显示原始 job 名（如 "scan_orphans"）和 "—" 描述。
 */
function jobTitle(job: { title?: string; name: string }): string {
  return job.title || job.name
}

function jobDesc(job: { description?: string }): string {
  return job.description || '—'
}

/** 按 job 名取中文名（弹框标题用）——从后端下发的作业列表里查。 */
function jobTitleByName(name: string): string {
  return data.value?.jobs.find(j => j.name === name)?.title || name
}

/** 某个模块下的作业（保持后端下发顺序）。 */
function jobsOf(moduleKey: string): SchedulerJob[] {
  return (data.value?.jobs ?? []).filter((j) => (j.module ?? '') === moduleKey)
}

/**
 * 分组列表：后端给了 `modules` 就用它（顺序 + 中文名都是真源）；
 * 老后端（无该字段）时按作业首次出现顺序兜底，页面不至于空白。
 */
const moduleGroups = computed(() => {
  const jobs = data.value?.jobs ?? []
  const declared = data.value?.modules
  if (declared?.length) {
    return declared.map((m) => ({
      key: m.key,
      title: m.title,
      jobs: jobsOf(m.key),
    }))
  }
  const seen: { key: string; title: string; jobs: SchedulerJob[] }[] = []
  for (const j of jobs) {
    const key = j.module ?? ''
    let group = seen.find((g) => g.key === key)
    if (!group) {
      group = { key, title: j.module_title || '其它', jobs: [] }
      seen.push(group)
    }
    group.jobs.push(j)
  }
  return seen
})

/**
 * 当前 tab（域的 key）。
 *
 * 默认选第一个域（后端下发顺序 = 注册顺序，辅种域在前）；data 到达后
 * 若还没选过就落到第一个，避免首屏是空 tab。
 */
const activeModule = ref('')

watch(moduleGroups, (groups) => {
  if (!groups.length) return
  if (!groups.some((g) => g.key === activeModule.value)) {
    activeModule.value = groups[0].key
  }
})

/**
 * 数据流链路：把「有依赖关系」的作业排到前面（上游 → 下游），其余按
 * 后端顺序跟在后面。
 *
 * 依赖是后端下发的（`depends_on`），前端只负责**排序与标注**：
 * 深度小的先排（上游在前），同一深度保持后端顺序（`sort` 稳定）。
 */
function orderedJobs(jobs: SchedulerJob[]): SchedulerJob[] {
  const byName = new Map(jobs.map((j) => [j.name, j]))
  const linked = (j: SchedulerJob) => !!j.depends_on && byName.has(j.depends_on)
  const depth = (job: SchedulerJob): number => {
    let d = 0
    let cur: SchedulerJob | undefined = job
    const seen = new Set<string>()
    while (cur?.depends_on && !seen.has(cur.name)) {
      seen.add(cur.name)
      d += 1
      cur = byName.get(cur.depends_on)
    }
    return d
  }
  // 链的成员 = 有依赖的节点 **加上它们的所有上游祖先**
  // （只取 depends_on 非空的那个会把「站点拉取」漏到独立节点里去，
  //   结果下游排在它的上游前面——2026-11-22 真机截图发现的）
  const inChain = new Set<string>()
  for (const j of jobs.filter(linked)) {
    let cur: SchedulerJob | undefined = j
    const seen = new Set<string>()
    // 一路走到链首：**链首自己通常没有 depends_on**，所以「有 depends_on 才
    // 收集」会把上游漏掉（曾导致下游排在它的上游前面）
    while (cur && !seen.has(cur.name)) {
      seen.add(cur.name)
      inChain.add(cur.name)
      cur = cur.depends_on ? byName.get(cur.depends_on) : undefined
    }
  }
  const chain = jobs.filter((j) => inChain.has(j.name))
  const rest = jobs.filter((j) => !inChain.has(j.name))
  chain.sort((a, b) => depth(a) - depth(b))
  return [...chain, ...rest]
}

/** 每个 tab 的展示顺序（后端顺序 + 依赖优先）。 */
const orderedByModule = computed<Record<string, SchedulerJob[]>>(() => {
  const out: Record<string, SchedulerJob[]> = {}
  for (const g of moduleGroups.value) {
    out[g.key] = orderedJobs(g.jobs)
  }
  return out
})

/** 该 tab 是否有数据流链路（决定要不要显示「独立节奏」分隔）。 */
function hasChain(moduleKey: string): boolean {
  return (orderedByModule.value[moduleKey] ?? []).some((j) => j.depends_on)
}

/** 是不是「链路结束了、开始独立节点」的那一项（用于插分隔标题）。 */
function isFirstIndependent(moduleKey: string, index: number): boolean {
  const list = orderedByModule.value[moduleKey] ?? []
  if (!hasChain(moduleKey)) return false
  // 链的成员一定带 depends_on（链首是别人的上游、自己也有上游或有人依赖它），
  // 故「当前项无 depends_on 且上一项在链内」= 链刚结束
  const hasDep = (i: number) => !!list[i]?.depends_on
  return index > 0 && !hasDep(index) && hasDep(index - 1)
}

/** 门槛秒数 → 人话（1800 → 「30 分钟」）。 */
function minutesLabel(sec: number | undefined): string {
  if (!sec) return '每次上游成功都触发'
  if (sec % 60 === 0) return `${sec / 60} 分钟`
  return `${Math.round(sec / 60)} 分钟`
}

/** 当前 tab 的统计（启用 / 运行中）——一眼看出这个域在不在干活。 */
function groupStats(jobs: SchedulerJob[]): { enabled: number; running: number } {
  return {
    enabled: jobs.filter((j) => j.enabled).length,
    running: jobs.filter((j) => j.running).length,
  }
}

async function loadRuns() {
  try {
    // 取 200 条（后端上限）：作业节奏从 5 分钟到 3 小时不等，50 条只
    // 覆盖 ~2 小时，会让 3 小时间隔的辅种维护经常「最近运行」空白
    // （2026-11-04 用户反馈）。200 条 ≈ 7 小时，覆盖最长间隔。
    const page = await api.get<JobRunPage>('/scheduler/runs?limit=200')
    const grouped: Record<string, JobRunItem[]> = {}
    for (const r of page.items) {
      (grouped[r.job_name] ??= []).push(r)
    }
    runsByJob.value = grouped
  } catch { /* ignore */ }
}

async function load() {
  try {
    data.value = await api.get<SchedulerInfo>('/scheduler')
  } catch { /* ignore */ }
}

onMounted(() => {
  load()
  loadRuns()
  const timer = setInterval(() => {
    load()
    loadRuns()
  }, 5000)
  onUnmounted(() => clearInterval(timer))
})

async function toggle(name: string, enabled: boolean) {
  try {
    await api.post(`/scheduler/${name}/toggle?enabled=${enabled}`)
    message.success(`${name} → ${enabled ? '启用' : '暂停'}`)
    load()
  } catch (e) {
    // 失败时重新拉取状态——否则开关停在用户点击后的乐观状态，
    // 与后端实际状态不一致。
    message.error('切换失败：' + errMsg(e))
    load()
  }
}

async function runNow(name: string) {
  runningJob.value = name
  const hide = message.loading(`正在执行 ${name}...`, 0)
  try {
    const r = await api.post<RunResult>(`/scheduler/${name}/run`)
    const summary = formatResult(r)
    if (r.ok) {
      message.success(`${name} 完成：${summary}（${r.duration_ms} ms）`)
    } else {
      message.error(`${name} 失败：${summary || r.error || '未知错误'}`)
    }
  } catch (e) {
    message.error('触发失败：' + errMsg(e))
  } finally {
    hide()
    runningJob.value = null
    load()
    loadRuns()
  }
}

/** 结果摘要优先用后端给的 summary（后端是人话来源，前端不硬编码字段名）。 */
function formatResult(r: RunResult): string {
  const v = r.result
  if (!v || typeof v !== 'object') return ''
  const summary = v.summary
  if (typeof summary === 'string' && summary) return summary
  // 兜底：老数据（无 summary）时拍平其余字段
  return Object.entries(v)
    .filter(([k]) => k !== 'steps' && k !== 'summary')
    .map(([key, val]) => `${key}: ${typeof val === 'object' ? JSON.stringify(val) : String(val)}`)
    .join('，')
}

function openConfig(name: string) {
  configJobName.value = name
  configOpen.value = true
}

function openHistory(name: string) {
  historyJobName.value = name
  historyOpen.value = true
}

function formatRunTime(s: string): string {
  return s.length >= 16 ? s.substring(11, 16) : s
}

function formatRunFull(s: string): string {
  return s
}
</script>

<template>
  <div>
    <PageHeader
      eyebrow="系统 · SCHEDULER"
      title="定时任务"
      sub="辅种 / 媒体库分开调度 · 启停 / 配置 / 历史"
    />

    <hr class="divider-hairline" style="margin: 0" />

    <section class="section">
      <div v-if="!data" class="loading-row">
        <span class="mono dim">加载中...</span>
      </div>
      <div v-else>
        <div class="scheduler-head">
          <div>
            <span class="label-caps">调度器状态</span>
            <div class="scheduler-head__state">
              <span
                class="live-dot"
                :class="{ 'live-dot--idle': !data.accepting_new_work }"
              />
              <span class="mono">{{ data.accepting_new_work ? 'WORKING' : 'STOPPED' }}</span>
              <span class="dim mono" style="margin-left: var(--sp-2)">inflight {{ data.inflight }}</span>
              <span class="dim mono" style="margin-left: var(--sp-2)">
                {{ moduleGroups.length }} 个业务域 · {{ data.jobs.length }} 个作业
              </span>
            </div>
          </div>
        </div>

        <!--
          **一个业务域一个 tab**（辅种 / 媒体库）：混排时要在一堆不关心的
          作业里找目标，堆一页又会拉成 2-3 屏长列表。域的顺序与中文名都
          来自后端（`modules`），前端不自己排。
        -->
        <a-tabs v-model:active-key="activeModule">
          <a-tab-pane
            v-for="group in moduleGroups"
            :key="group.key"
          >
            <template #tab>
              <span class="domain-tab">
                <span class="domain-tab__name">{{ group.title }}</span>
                <span class="domain-tab__meta mono">
                  {{ group.jobs.length }}
                  <template v-if="groupStats(group.jobs).running"> · 运行中 {{ groupStats(group.jobs).running }}</template>
                </span>
              </span>
            </template>

            <div class="module-head">
              <span class="label-caps">{{ group.title }}</span>
              <span class="module-head__meta mono dim">
                {{ group.jobs.length }} 个作业 · 启用 {{ groupStats(group.jobs).enabled }}
                <template v-if="groupStats(group.jobs).running">
                  · <span class="module-head__running">运行中 {{ groupStats(group.jobs).running }}</span>
                </template>
              </span>
            </div>

            <div class="job-list">
              <div
                v-for="(job, i) in orderedByModule[group.key] ?? group.jobs"
                :key="job.name"
                class="job-item"
              >
                <!--
                  数据流来源：上游成功后按门槛触发本作业（后端 `depends_on`）。
                  标在卡片顶部而不是画箭头连线——窄屏/折叠时不依赖几何关系，
                  标签里带上游中文名，读起来就是「由 站点拉取 触发」。
                -->
                <div v-if="job.depends_on" class="job-item__upstream">
                  <span class="job-item__upstream-icon">↑</span>
                  <span>
                    由 <b>{{ jobTitleByName(job.depends_on) }}</b> 成功后触发
                  </span>
                  <span class="mono dim">
                    间隔 ≥ {{ minutesLabel(job.min_trigger_interval_sec) }}
                  </span>
                </div>
                <div v-if="isFirstIndependent(group.key, i)" class="job-item__section">
                  <span class="label-caps">独立节奏</span>
                  <span class="dim mono">只按自己的 cron 触发</span>
                </div>
                <div class="job-item__main">
                  <div class="job-item__title-row">
                    <span
                      class="live-dot"
                      :class="{ 'live-dot--idle': !job.running }"
                    />
                    <span class="job-item__name">{{ jobTitle(job) }}</span>
                    <span class="job-item__id mono">{{ job.name }}</span>
                  </div>
                  <div class="job-item__desc">{{ jobDesc(job) }}</div>
                  <div class="job-item__cron">
                    <span class="job-item__cron-label dim mono">cron</span>
                    <span class="job-item__cron-text mono">{{ job.cron }}</span>
                  </div>
                </div>
                <div class="job-item__actions">
                  <a-switch
                    :checked="job.enabled"
                    @change="(e: boolean) => toggle(job.name, e)"
                  />
                  <a-button
                    size="small"
                    type="default"
                    :loading="runningJob === job.name"
                    :disabled="!data.accepting_new_work"
                    @click="runNow(job.name)"
                  >
                    立即执行 →
                  </a-button>
                  <a-button
                    size="small"
                    type="default"
                    @click="openConfig(job.name)"
                  >
                    配置
                  </a-button>
                  <a-button
                    size="small"
                    type="default"
                    @click="openHistory(job.name)"
                  >
                    历史
                  </a-button>
                </div>
                <div v-if="runsByJob[job.name]?.length" class="job-item__runs">
                  <span class="dim mono" style="font-size: var(--text-xs)">最近运行：</span>
                  <a-tooltip
                    v-for="r in runsByJob[job.name].slice(0, 6)"
                    :key="r.id"
                    :title="`${formatRunFull(r.started_at)} · ${r.duration_ms ?? 0}ms · ${r.ok ? 'OK' : 'FAIL'}${r.error ? ' · ' + r.error : ''}`"
                  >
                    <span class="run-chip" :class="r.ok ? 'run-chip--ok' : 'run-chip--fail'">
                      <span class="run-chip__time mono">{{ formatRunTime(r.started_at) }}</span>
                      <span v-if="!r.ok" class="run-chip__x">✕</span>
                    </span>
                  </a-tooltip>
                </div>
              </div>
            </div>
          </a-tab-pane>
        </a-tabs>

        <a-alert
          v-if="!data.enabled"
          type="warning"
          show-icon
          style="margin-top: var(--sp-4)"
          message="调度器已禁用"
          description="SCHEDULER_ENABLED=false。所有周期性作业都不会执行。"
        />
        <a-alert
          v-else-if="!data.accepting_new_work"
          type="warning"
          show-icon
          style="margin-top: var(--sp-4)"
          message="调度器停机中"
          description="正在等待在途作业收敛，立即执行暂不可用。"
        />

        <SchedulerConfigModal
          v-model:open="configOpen"
          :job-name="configJobName"
          :job-label="jobTitleByName(configJobName)"
          @saved="() => { load(); loadRuns() }"
        />
        <SchedulerHistoryModal
          v-model:open="historyOpen"
          :job-name="historyJobName"
          :job-label="jobTitleByName(historyJobName)"
        />
      </div>
    </section>
  </div>
</template>

<style scoped>
.loading-row {
  padding: var(--sp-4) 0;
}

.scheduler-head {
  margin-bottom: var(--sp-4);
}

.scheduler-head__state {
  margin-top: var(--sp-2);
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--text-md);
  flex-wrap: wrap;
}

.dim {
  color: var(--text-tertiary);
}

/*
 * 业务域 tab（辅种 / 媒体库）。
 *
 * tab 标签带作业数 + 「运行中 N」——不用切过去也知道另一个域在不在干活。
 * 手机上标签也够窄（4 个字 + 数字），不会折行。
 */
.domain-tab {
  display: inline-flex;
  align-items: baseline;
  gap: var(--sp-2);
}

.domain-tab__name {
  font-weight: 500;
}

.domain-tab__meta {
  font-size: var(--text-xs);
  color: var(--text-tertiary);
}

/* 当前 tab 的统计条：域内作业数 / 启用数 / 运行中 */
.module-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--sp-3);
  flex-wrap: wrap;
  margin-bottom: var(--sp-2);
}

.module-head__meta {
  font-size: var(--text-xs);
}

.module-head__running {
  color: var(--accent-red);
}

.job-list {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border-hairline);
  border-radius: var(--r-md);
  background: var(--bg-surface);
}

.job-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--sp-3) var(--sp-4);
  gap: var(--sp-4);
  border-bottom: 1px solid var(--border-hairline);
  /* 窄屏：作业名 / cron / 动作区换行堆叠，否则一行挤不下 */
  flex-wrap: wrap;
  min-width: 0;
}

.job-item:last-child {
  border-bottom: 0;
}

.job-item:hover {
  background: var(--bg-inset);
}

/*
 * 数据流来源条：贴在卡片顶部，说明「这次是谁叫的」。
 * 用左边一道竖线表达「来自上游」，不依赖绝对定位（窄屏不会错位）。
 */
.job-item__upstream {
  flex: 1 1 100%;
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  flex-wrap: wrap;
  margin-bottom: var(--sp-1);
  padding-left: var(--sp-2);
  border-left: 2px solid var(--accent-red);
  font-size: var(--text-xs);
  color: var(--text-secondary);
}

.job-item__upstream-icon {
  color: var(--accent-red);
  font-weight: 700;
}

/* 链路结束后的「独立节奏」分隔 */
.job-item__section {
  flex: 1 1 100%;
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  flex-wrap: wrap;
  padding: var(--sp-2) 0 var(--sp-1);
  border-top: 1px dashed var(--border-hairline);
  margin-top: var(--sp-1);
}

/*
 * 主体：**给一个可用宽度下限**（basis 320px），不写 `flex: 1`（basis 0）。
 *
 * 写 basis 0 时，只要「动作区 + 运行记录」两块的基准宽度之和略小于容器宽，
 * 它们就会被排进同一行且不换行（0 + 253 + 384 ≤ 容器宽），主体只剩几像素
 * → 中文标题逐字竖排（698px 列宽实测）。给 basis 后主体先占够位置，
 * 装不下自然换行，任何列宽下都不会被压扁。
 */
.job-item__main {
  flex: 1 1 320px;
  min-width: 0;
}

.job-item__title-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-1);
}

.job-item__name {
  font-weight: 500;
  font-size: var(--text-md);
}

.job-item__id {
  font-size: var(--text-xs);
  color: var(--text-tertiary);
  margin-left: var(--sp-1);
}

.job-item__desc {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  margin-bottom: var(--sp-1);
  line-height: 1.5;
}

.job-item__cron {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--text-xs);
}

.job-item__cron-label {
  letter-spacing: 0.05em;
}

.job-item__cron-text {
  color: var(--text-secondary);
}

.job-item__actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex: 0 0 auto;
}

.job-item__runs {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  flex-wrap: wrap;
  padding-top: var(--sp-2);
  border-top: 1px dashed var(--border-hairline);
  /*
   * **恒独占一行**（basis 100%）：运行记录是「补充信息」，不该和主体 /
   * 动作区抢同一行的宽度——抢的结果就是主体被压扁。
   */
  flex: 1 1 100%;
  min-width: 0;
  max-width: 100%;
}

.run-chip {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 1px 6px;
  border-radius: var(--r-sm);
  font-size: var(--text-xs);
  border: 1px solid var(--border-hairline);
  background: var(--bg-surface);
  cursor: default;
  /* 窄屏下时间戳不能把卡片撑宽（flex item 默认 min-width:auto） */
  min-width: 0;
  max-width: 100%;
}

.run-chip--ok {
  border-color: var(--success-border, rgba(82, 196, 26, 0.5));
  color: var(--success-text, #52c41a);
}

.run-chip--fail {
  border-color: var(--error-border, rgba(245, 34, 45, 0.5));
  color: var(--error-text, #f5222d);
  background: var(--error-bg-subtle, rgba(245, 34, 45, 0.04));
}

.run-chip__x {
  font-weight: 600;
}

/*
 * 手机端：主体 + 动作区各占一行（动作区靠左）。
 *
 * ⚠️ **必须放在基础规则之后**：媒体查询不提升优先级，同样特异性下
 * 「后写的赢」。此前这段排在 `.job-item__main { flex: 1 }` /
 * `.job-item__actions { flex: 0 0 auto }` 之前 → 两条覆盖全被基础规则
 * 盖掉，手机上主体被压到 ~0 宽、中文标题逐字竖排（390px 视口实测）。
 */
@media (max-width: 767px) {
  .job-item {
    gap: var(--sp-2);
    padding: var(--sp-3);
  }

  .job-item__main {
    flex: 1 1 100%;
  }

  .job-item__actions {
    flex: 1 1 100%;
    justify-content: flex-start;
  }
}
</style>
