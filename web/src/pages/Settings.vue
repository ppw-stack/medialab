<script setup lang="ts">
import { ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import SchedulerTab from './settings/SchedulerTab.vue'

const auth = useAuthStore()

const activeKey = ref('scheduler')
</script>

<template>
  <div>
    <a-typography-title :level="4" style="margin-bottom: 16px">系统设置（{{ auth.username }}）</a-typography-title>
    <a-tabs v-model:active-key="activeKey">
      <a-tab-pane key="scheduler" tab="调度作业">
        <SchedulerTab />
      </a-tab-pane>
    </a-tabs>
  </div>
</template>
