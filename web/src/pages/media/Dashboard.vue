<template>
  <div class="page">
    <PageHeader
      eyebrow="媒体库 · MEDIA"
      title="总览"
      sub="存储源、媒体条目与中转链路状态"
      compact
    />
    <a-spin :spinning="loading">
      <a-row :gutter="16">
        <a-col :xs="24" :sm="12" :lg="6">
          <a-card size="small">
            <a-statistic title="媒体条目" :value="stats?.total ?? 0" />
          </a-card>
        </a-col>
        <a-col :xs="24" :sm="12" :lg="6">
          <a-card size="small">
            <a-statistic title="媒体总大小" :value="formatBytes(stats?.total_size ?? 0)" />
          </a-card>
        </a-col>
        <a-col :xs="24" :sm="12" :lg="6">
          <a-card size="small">
            <a-statistic title="存储源" :value="drivers.length" />
          </a-card>
        </a-col>
        <a-col :xs="24" :sm="12" :lg="6">
          <a-card size="small">
            <a-statistic
              title="缓存命中率"
              :value="`${((ready?.cache?.hit_rate ?? 0) * 100).toFixed(1)}%`"
            />
          </a-card>
        </a-col>
      </a-row>

      <a-row :gutter="16" style="margin-top: 16px">
        <a-col :xs="24" :lg="12">
          <a-card size="small" title="中转链路">
            <a-descriptions bordered :column="1" size="small">
              <a-descriptions-item label="服务状态">
                <a-tag :color="ready?.database ? 'green' : 'red'">
                  {{ ready?.database ? "正常" : "数据库异常" }}
                </a-tag>
              </a-descriptions-item>
              <a-descriptions-item label="缓存条目">
                L1 {{ ready?.cache?.l1_size ?? 0 }} / {{ ready?.cache?.l1_capacity ?? 0 }}
              </a-descriptions-item>
              <a-descriptions-item label="命中 / 未命中">
                {{ ready?.cache?.hits ?? 0 }} / {{ ready?.cache?.misses ?? 0 }}
              </a-descriptions-item>
              <a-descriptions-item label="在途请求">
                {{ ready?.cache?.in_flight ?? 0 }}
              </a-descriptions-item>
            </a-descriptions>
            <a-alert
              type="info"
              show-icon
              style="margin-top: 12px"
              message="媒体字节流不经过本服务"
              description="播放器请求 /302 后直接跟随重定向到网盘 CDN，GB 级流量走玩家↔网盘，本服务只出网换取直链（几 KB）。"
            />
          </a-card>
        </a-col>

        <a-col :xs="24" :lg="12">
          <a-card size="small" title="存储源">
            <a-list :data-source="drivers" size="small" :locale="{ emptyText: '暂无存储源' }">
              <template #renderItem="{ item }">
                <a-list-item>
                  <a-space>
                    <span>{{ item.name }}</span>
                    <a-tag v-if="item.media_path" color="blue" class="mono">
                      {{ item.media_path }}
                    </a-tag>
                    <a-tag v-else color="default">未配置媒体库</a-tag>
                  </a-space>
                  <span class="muted">
                    {{ item.last_scan_at ? formatTime(item.last_scan_at) : "未扫描" }}
                  </span>
                </a-list-item>
              </template>
            </a-list>
          </a-card>
        </a-col>
      </a-row>
    </a-spin>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue"
import { mediaApi as api } from "../../api"
import type { Driver, MediaStats, ReadyResponse } from "../../types"
import PageHeader from "../../components/PageHeader.vue"
import { formatBytes, formatTime } from "../../utils/format"

const stats = ref<MediaStats | null>(null)
const drivers = ref<Driver[]>([])
const ready = ref<ReadyResponse | null>(null)
const loading = ref(false)

onMounted(async () => {
  loading.value = true
  try {
    const [s, d, rd] = await Promise.all([
      api.get<MediaStats>("/media/stats"),
      api.get<Driver[]>("/drivers"),
      // readyz 在根路径（不在 /api 下）
      fetch("/readyz")
        .then((resp) => resp.json() as Promise<ReadyResponse>)
        .catch(() => null),
    ])
    stats.value = s
    drivers.value = d
    ready.value = rd
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
</style>
