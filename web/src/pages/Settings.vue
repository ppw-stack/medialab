<script setup lang="ts">
import { ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import AlertPolicyTab from './settings/AlertPolicyTab.vue'
import ChannelsTab from './settings/ChannelsTab.vue'
import SchedulerTab from './settings/SchedulerTab.vue'

const auth = useAuthStore()

const activeKey = ref('alert')
</script>

<template>
  <div>
    <a-typography-title :level="4" style="margin-bottom: 16px">系统设置（{{ auth.username }}）</a-typography-title>
    <a-tabs v-model:active-key="activeKey">
      <a-tab-pane key="alert" tab="告警策略">
        <AlertPolicyTab />
      </a-tab-pane>
      <a-tab-pane key="channels" tab="通知渠道">
        <ChannelsTab />
      </a-tab-pane>
      <a-tab-pane key="scheduler" tab="调度作业">
        <SchedulerTab />
      </a-tab-pane>
    </a-tabs>
  </div>
</template>
