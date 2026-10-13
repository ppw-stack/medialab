<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../api'

interface DashboardData {
  sites: { total: number; enabled: number }
  downloaders: { total: number; enabled: number }
}

const data = ref<DashboardData | null>(null)

onMounted(() => {
  api.get<DashboardData>('/dashboard').then((d) => (data.value = d)).catch(() => {})
})
</script>

<template>
  <div v-if="!data">
    <a-typography-text>加载中...</a-typography-text>
  </div>
  <div v-else>
    <a-typography-title :level="4">总览仪表盘</a-typography-title>
    <a-row :gutter="16">
      <a-col :span="12">
        <a-card>
          <a-statistic
            title="站点（启用 / 总数）"
            :value="data.sites.enabled"
            :suffix="`/ ${data.sites.total}`"
          />
        </a-card>
      </a-col>
      <a-col :span="12">
        <a-card>
          <a-statistic
            title="下载器（启用 / 总数）"
            :value="data.downloaders.enabled"
            :suffix="`/ ${data.downloaders.total}`"
          />
        </a-card>
      </a-col>
    </a-row>
  </div>
</template>
