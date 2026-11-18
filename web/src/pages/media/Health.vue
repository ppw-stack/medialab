<template>
  <div class="page">
    <PageHeader
      eyebrow="媒体库 · MEDIA"
      title="库健康检查"
      sub="断链检查——签名失效、文件缺失、孤儿文件"
      compact
    >
      <template #actions>
        <a-button size="small" :loading="loading" @click="check">重新检查</a-button>
      </template>
    </PageHeader>

    <a-card size="small">
      <a-spin :spinning="loading">
        <template v-if="report">
          <a-alert
            :type="report.healthy ? 'success' : 'warning'"
            show-icon
            :message="
              report.configuration_error
                ? '配置不完整'
                : report.healthy
                  ? '媒体库健康'
                  : '发现问题'
            "
            :description="
              report.configuration_error
                ? report.configuration_error
                : report.healthy
                  ? '所有 STRM 文件均在位，签名有效，无孤儿文件。'
                  : '下方列出各类问题。修复请到「媒体库」批量重签，或重新扫描对应规则。'
            "
            style="margin-bottom: 16px"
          />

          <a-row :gutter="16">
            <a-col :xs="12" :lg="6">
              <a-statistic title="失效（需重签）" :value="report.stale_count" />
            </a-col>
            <a-col :xs="12" :lg="6">
              <a-statistic title="文件缺失" :value="report.missing_files_count" />
            </a-col>
            <a-col :xs="12" :lg="6">
              <a-statistic title="孤儿文件" :value="report.orphan_files_count" />
            </a-col>
            <a-col :xs="12" :lg="6">
              <a-statistic title="签名无效" :value="report.invalid_signature_count" />
            </a-col>
          </a-row>

          <a-divider />

          <a-collapse>
            <a-collapse-panel key="stale" header="失效条目（STRM 内容与当前签名不符）">
              <a-empty v-if="!report.stale.length" description="无" />
              <a-space v-else wrap>
                <a-tag v-for="id in report.stale" :key="id">#{{ id }}</a-tag>
              </a-space>
            </a-collapse-panel>
            <a-collapse-panel key="missing" header="文件缺失（DB 有记录但磁盘无文件）">
              <a-empty v-if="!report.missing_files.length" description="无" />
              <a-space v-else wrap>
                <a-tag v-for="id in report.missing_files" :key="id" color="orange">
                  #{{ id }}
                </a-tag>
              </a-space>
            </a-collapse-panel>
            <a-collapse-panel key="orphan" header="孤儿文件（磁盘有文件但 DB 无记录）">
              <a-empty v-if="!report.orphan_files.length" description="无" />
              <a-list v-else size="small">
                <a-list-item v-for="p in report.orphan_files" :key="p">
                  <span class="mono truncate block-truncate">{{ p }}</span>
                </a-list-item>
              </a-list>
            </a-collapse-panel>
            <a-collapse-panel key="invalid" header="签名无效（用当前密钥算不出）">
              <a-empty v-if="!report.invalid_signature.length" description="无" />
              <a-space v-else wrap>
                <a-tag v-for="id in report.invalid_signature" :key="id" color="red">
                  #{{ id }}
                </a-tag>
              </a-space>
            </a-collapse-panel>
          </a-collapse>

          <p class="muted" style="margin-top: 16px">检查时间：{{ formatTime(report.checked_at) }}</p>
        </template>
        <a-empty v-else description="尚未检查" />
      </a-spin>
    </a-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue"
import { message } from "ant-design-vue"
import { errMsg, mediaApi as api } from "../../api"
import type { HealthReport } from "../../types"
import PageHeader from "../../components/PageHeader.vue"

const report = ref<HealthReport | null>(null)
const loading = ref(false)

onMounted(check)

async function check(): Promise<void> {
  loading.value = true
  try {
    report.value = await api.get<HealthReport>("/health/library")
  } catch (e) {
    message.error(errMsg(e, "检查失败"))
  } finally {
    loading.value = false
  }
}

function formatTime(ts: number): string {
  if (!ts) return "—"
  return new Date(ts * 1000).toLocaleString("zh-CN")
}
</script>
<style scoped>
/* .truncate 默认 inline-block——列表项里要占满整行才能真正截断 */
.block-truncate {
  display: block;
}
</style>
