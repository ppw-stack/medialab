<script setup lang="ts">
import { ref, watch } from 'vue'
import { api } from '../api'
import { message } from 'ant-design-vue'

interface ParamSpec {
  key: string
  label: string
  min: number
  max: number
  default: number
}

interface JobConfig {
  job_name: string
  cron: string
  enabled: boolean
  params: Record<string, number>
  specs: ParamSpec[]
}

const props = defineProps<{
  open: boolean
  jobName: string
  jobLabel: string
}>()

const emit = defineEmits<{
  (e: 'update:open', v: boolean): void
  (e: 'saved'): void
}>()

const config = ref<JobConfig | null>(null)
const loading = ref(false)
const saving = ref(false)
const cronDraft = ref('')
const paramDraft = ref<Record<string, number>>({})

watch(
  () => props.open,
  async (o) => {
    if (o) {
      await load()
    }
  }
)

async function load() {
  loading.value = true
  try {
    const r = await api.get<JobConfig>(`/scheduler/${props.jobName}/config`)
    config.value = r
    cronDraft.value = r.cron
    paramDraft.value = { ...r.params }
  } catch (e) {
    message.error('加载配置失败：' + (e as Error).message)
  } finally {
    loading.value = false
  }
}

function isDirty(): boolean {
  if (!config.value) return false
  if (cronDraft.value.trim() !== config.value.cron) return true
  for (const [k, v] of Object.entries(paramDraft.value)) {
    if (config.value.params[k] !== v) return true
  }
  return false
}

async function onSave() {
  if (!config.value) return
  const cronNew = cronDraft.value.trim()
  if (!cronNew) {
    message.warning('cron 不能为空')
    return
  }
  saving.value = true
  try {
    if (cronNew !== config.value.cron) {
      await api.post(`/scheduler/${props.jobName}/cron`, { cron: cronNew })
    }
    // 只传本任务专属的 param 全量 (覆盖该任务的 param 子集)
    const fullParams: Record<string, number> = { ...config.value.params, ...paramDraft.value }
    const dirty = Object.entries(fullParams).some(
      ([k, v]) => config.value!.params[k] !== v
    )
    if (dirty) {
      await api.put('/scheduler/runtime-params', fullParams)
    }
    message.success(`${props.jobLabel} 配置已保存`)
    emit('saved')
    emit('update:open', false)
  } catch (e) {
    message.error('保存失败：' + (e as Error).message)
  } finally {
    saving.value = false
  }
}

function onCancel() {
  emit('update:open', false)
}
</script>

<template>
  <a-modal
    :open="open"
    :title="`配置 · ${jobLabel} (${jobName})`"
    :footer="null"
    width="600px"
    :mask-closable="!saving"
    :keyboard="!saving"
    @cancel="onCancel"
  >
    <div v-if="loading || !config" class="loading-row">
      <span class="mono dim">加载中…</span>
    </div>
    <div v-else>
      <!-- 调度 -->
      <section class="config-section">
        <div class="config-section__title">调度</div>
        <div class="config-row">
          <label class="config-label">cron 表达式</label>
          <a-input
            v-model:value="cronDraft"
            placeholder="*/10 * * * *"
            style="width: 180px"
          />
          <span class="config-hint">5 字段: 分 时 日 月 星期</span>
        </div>
      </section>

      <!-- 作业运行参数 (按 spec 渲染) -->
      <section v-if="config.specs.length" class="config-section">
        <div class="config-section__title">作业运行参数</div>
        <div
          v-for="spec in config.specs"
          :key="spec.key"
          class="config-row"
        >
          <label class="config-label">{{ spec.label }}</label>
          <a-input-number
            v-model:value="paramDraft[spec.key]"
            :min="spec.min"
            :max="spec.max"
            :step="1"
            style="width: 140px"
          />
          <span class="config-hint">范围 {{ spec.min }}–{{ spec.max }} (默认 {{ spec.default }})</span>
        </div>
      </section>

      <section v-else class="config-section">
        <span class="config-hint">本作业无可调参数</span>
      </section>

      <!-- 操作 -->
      <div class="config-actions">
        <a-button :disabled="saving" @click="onCancel">取消</a-button>
        <a-button
          type="primary"
          :loading="saving"
          :disabled="!isDirty()"
          @click="onSave"
        >
          保存
        </a-button>
      </div>
    </div>
  </a-modal>
</template>

<style scoped>
.loading-row {
  padding: var(--sp-4) 0;
  text-align: center;
}
.dim { color: var(--text-tertiary); }
.mono { font-family: var(--font-mono, monospace); }

.config-section {
  margin-bottom: var(--sp-4);
}
.config-section__title {
  font-weight: 500;
  margin-bottom: var(--sp-3);
  color: var(--text-secondary);
  font-size: var(--text-sm);
  letter-spacing: 0.05em;
  text-transform: uppercase;
}
.config-row {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  margin-bottom: var(--sp-2);
}
.config-label {
  flex: 0 0 200px;
  font-size: var(--text-sm);
  color: var(--text);
}
.config-hint {
  color: var(--text-tertiary);
  font-size: var(--text-xs);
  font-family: var(--font-mono, monospace);
}
.config-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-2);
  margin-top: var(--sp-3);
  padding-top: var(--sp-3);
  border-top: 1px solid var(--border-hairline);
}
</style>
