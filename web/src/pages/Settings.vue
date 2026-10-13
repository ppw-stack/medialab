<script setup lang="ts">
import { ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import SchedulerTab from './settings/SchedulerTab.vue'

const auth = useAuthStore()

const activeKey = ref('scheduler')
</script>

<template>
  <div>
    <header class="page-header">
      <div>
        <div class="page-header__eyebrow">设置 · SETTINGS</div>
        <h1 class="page-header__title">系统配置</h1>
        <div class="page-header__sub">
          当前会话 · <span class="mono">{{ auth.username }}</span>
        </div>
      </div>
    </header>

    <hr class="divider-hairline" style="margin: 0" />

    <section class="section">
      <a-tabs v-model:active-key="activeKey">
        <a-tab-pane key="scheduler" tab="调度作业">
          <SchedulerTab />
        </a-tab-pane>
      </a-tabs>
    </section>
  </div>
</template>
