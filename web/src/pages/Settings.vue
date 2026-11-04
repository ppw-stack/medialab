<script setup lang="ts">
import { ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import SchedulerTab from './settings/SchedulerTab.vue'
import EnvTab from './settings/EnvTab.vue'
import PageHeader from '../components/PageHeader.vue'

const auth = useAuthStore()

const activeKey = ref('scheduler')
</script>

<template>
  <div>
    <PageHeader
      eyebrow="设置 · SETTINGS"
      title="系统配置"
      sub="调度作业与环境变量"
    >
      <template #sub>
        当前会话 · <span class="mono">{{ auth.username }}</span>
      </template>
    </PageHeader>

    <hr class="divider-hairline" style="margin: 0" />

    <section class="section">
      <a-tabs v-model:active-key="activeKey">
        <a-tab-pane key="scheduler" tab="调度作业">
          <SchedulerTab />
        </a-tab-pane>
        <a-tab-pane key="env" tab="环境变量">
          <EnvTab />
        </a-tab-pane>
      </a-tabs>
    </section>
  </div>
</template>
