<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../api'

interface DashboardSnap {
  sites: { total: number; enabled: number }
  downloaders: { total: number; enabled: number }
  free_ending_soon?: number
  free_index_total?: number
  inflight_torrents?: number
}

interface SchedulerInfo {
  enabled: boolean
  inflight: number
  accepting_new_work: boolean
  jobs: { name: string; enabled: boolean; running: boolean; cron: string }[]
}

const snap = ref<DashboardSnap | null>(null)
const scheduler = ref<SchedulerInfo | null>(null)
const now = ref(new Date())
let timer: ReturnType<typeof setInterval> | null = null

async function load() {
  try {
    snap.value = await api.get<DashboardSnap>('/dashboard')
  } catch {
    /* ignore */
  }
  try {
    scheduler.value = await api.get<SchedulerInfo>('/scheduler')
  } catch {
    /* ignore */
  }
}

onMounted(() => {
  load()
  timer = setInterval(() => {
    now.value = new Date()
    load()
  }, 5000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
})

const timeStr = computed(() =>
  now.value.toLocaleTimeString('zh-CN', { hour12: false }),
)
const dateStr = computed(() =>
  now.value.toLocaleDateString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    weekday: 'short',
  }),
)

const enabledSites = computed(() => snap.value?.sites.enabled ?? 0)
const totalSites = computed(() => snap.value?.sites.total ?? 0)
const enabledDownloaders = computed(() => snap.value?.downloaders.enabled ?? 0)
const totalDownloaders = computed(() => snap.value?.downloaders.total ?? 0)
const freeEndingSoon = computed(() => snap.value?.free_ending_soon ?? null)
const freeIndexTotal = computed(() => snap.value?.free_index_total ?? null)
const inflightTorrents = computed(() => snap.value?.inflight_torrents ?? null)

const inflightJobs = computed(() => scheduler.value?.inflight ?? 0)
const accepting = computed(() => scheduler.value?.accepting_new_work ?? false)

const JOB_LABELS: Record<string, string> = {
  fetch_all_sites: '站点拉取',
  push_free_torrents: '免费种推送',
}
</script>

<template>
  <div>
    <header class="page-header">
      <div>
        <div class="page-header__eyebrow">总览 · OVERVIEW</div>
        <h1 class="page-header__title">系统当前状态</h1>
        <div class="page-header__sub">
          {{ dateStr }}&nbsp;&nbsp;<span class="mono">{{ timeStr }}</span>
        </div>
      </div>
      <div class="mono page-header__meta">
        <span class="live-dot" :class="{ 'live-dot--idle': !accepting }" />
        <span>{{ accepting ? `调度器 · ${inflightJobs} 在途` : '调度器 · 停机' }}</span>
      </div>
    </header>

    <hr class="divider-hairline" style="margin: 0" />

    <section class="section">
      <div class="label-caps" style="margin-bottom: var(--sp-3)">实时信号 · LIVE</div>
      <a-row :gutter="[12, 12]">
        <a-col :xs="12" :sm="6">
          <div class="stat-tile" :class="{ 'stat-tile--active': enabledSites > 0 }">
            <div class="stat-tile__label">启用站点</div>
            <div class="stat-tile__value">
              {{ enabledSites }}<span style="color: var(--text-tertiary); font-size: var(--text-md); font-weight: 400"> / {{ totalSites }}</span>
            </div>
            <div class="stat-tile__hint">API / Cookies</div>
          </div>
        </a-col>
        <a-col :xs="12" :sm="6">
          <div class="stat-tile" :class="{ 'stat-tile--active': enabledDownloaders > 0 }">
            <div class="stat-tile__label">启用下载器</div>
            <div class="stat-tile__value">
              {{ enabledDownloaders }}<span style="color: var(--text-tertiary); font-size: var(--text-md); font-weight: 400"> / {{ totalDownloaders }}</span>
            </div>
            <div class="stat-tile__hint">qB · Transmission</div>
          </div>
        </a-col>
        <a-col :xs="12" :sm="6">
          <div
            class="stat-tile"
            :class="{ 'stat-tile--active': freeEndingSoon !== null && freeEndingSoon > 0 }"
            :style="freeEndingSoon !== null && freeEndingSoon > 0
              ? { borderColor: 'var(--status-amber)', background: 'var(--status-amber-soft)' }
              : {}"
          >
            <div class="stat-tile__label">免费即将截止</div>
            <div
              class="stat-tile__value"
              :class="{ 'countdown--soon': freeEndingSoon !== null && freeEndingSoon > 0 }"
              :style="freeEndingSoon !== null && freeEndingSoon > 0
                ? { color: 'var(--status-amber)' }
                : {}"
            >
              {{ freeEndingSoon !== null ? freeEndingSoon : '—' }}
            </div>
            <div class="stat-tile__hint">窗口 ≤ 6 小时</div>
          </div>
        </a-col>
        <a-col :xs="12" :sm="6">
          <div class="stat-tile">
            <div class="stat-tile__label">索引总条数</div>
            <div class="stat-tile__value">{{ freeIndexTotal !== null ? freeIndexTotal : '—' }}</div>
            <div class="stat-tile__hint">torrent_index</div>
          </div>
        </a-col>
      </a-row>
    </section>

    <section class="section">
      <div class="label-caps" style="margin-bottom: var(--sp-3)">调度作业 · SCHEDULER</div>
      <a-table
        v-if="scheduler"
        :data-source="scheduler.jobs"
        :columns="[{ title: '', key: 'job' }]"
        :show-header="false"
        :pagination="false"
        row-key="name"
        size="middle"
      >
        <template #bodyCell="{ record }">
          <div class="job-row">
            <span
              class="live-dot"
              :class="{ 'live-dot--idle': !(record as any).running }"
            />
            <span class="job-row__name">
              {{ JOB_LABELS[(record as any).name] || (record as any).name }}
            </span>
            <span class="job-row__cron mono">{{ (record as any).cron }}</span>
            <span class="job-row__state">
              <a-tag v-if="(record as any).running" color="gold">RUNNING</a-tag>
              <a-tag v-else-if="(record as any).enabled" color="green">READY</a-tag>
              <a-tag v-else>DISABLED</a-tag>
            </span>
          </div>
        </template>
      </a-table>
      <a-empty v-else description="调度器未连接" />
    </section>

    <section class="section" v-if="inflightTorrents !== null">
      <div class="label-caps" style="margin-bottom: var(--sp-3)">下载器在途 · INFLIGHT</div>
      <div class="stat-tile" style="max-width: 280px">
        <div class="stat-tile__label">当前下载任务</div>
        <div class="stat-tile__value">{{ inflightTorrents }}</div>
        <div class="stat-tile__hint">跨所有下载器合计</div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.page-header__meta {
  font-size: var(--text-xs);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}

.job-row {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-2) 0;
  font-size: var(--text-base);
}

.job-row__name {
  flex: 0 0 auto;
  font-weight: 500;
  min-width: 120px;
}

.job-row__cron {
  flex: 1;
  color: var(--text-tertiary);
  font-size: var(--text-xs);
}

.job-row__state {
  flex: 0 0 auto;
}
</style>
