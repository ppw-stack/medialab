<script setup lang="ts">
import { useAuthStore } from '../stores/auth'
import EnvTab from './settings/EnvTab.vue'
import PageHeader from '../components/PageHeader.vue'

/**
 * 设置页（原「调度作业 / 环境变量」两个 tab 中的**设置**这一半）。
 *
 * 2026-11-22 拆分：调度（定时任务）与环境变量配置是**两件不同的事**——
 * 前者面向「什么时候跑哪些作业」，后者是「这套部署的路径/环境值」。
 * 挂在一个页面的两个 tab 里时，找调度要先进「设置」，与它作为日常高频
 * 页面的地位不符。现拆成两个平级菜单：`/scheduler`（定时任务）与
 * `/settings`（设置）。
 *
 * 环境变量配置仍留在本页（它是部署级设置，不是作业级配置）。
 * 媒体库域的「签名密钥」是域内配置，仍在 `/media-settings`。
 */
const auth = useAuthStore()
</script>

<template>
  <div>
    <PageHeader
      eyebrow="系统 · SETTINGS"
      title="设置"
      sub="环境变量配置（重启后保持）"
    >
      <template #sub>
        当前会话 · <span class="mono">{{ auth.username }}</span>
      </template>
    </PageHeader>

    <hr class="divider-hairline" style="margin: 0" />

    <section class="section">
      <EnvTab />
    </section>
  </div>
</template>
