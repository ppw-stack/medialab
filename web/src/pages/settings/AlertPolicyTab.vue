<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { api } from '../../api'
import { message } from 'ant-design-vue'

interface AlertPolicy {
  mute_window_hours: number;
  warning_daily_digest: boolean;
  critical_immediate: boolean;
  storm_threshold: number;
  quiet_hours: string | null;
}

const loading = ref(false)
const saving = ref(false)

const formState = reactive<AlertPolicy>({
  mute_window_hours: 6,
  warning_daily_digest: true,
  critical_immediate: true,
  storm_threshold: 50,
  quiet_hours: null,
})

async function load() {
  loading.value = true
  try {
    const p = await api.get<AlertPolicy>('/alerts/policy')
    Object.assign(formState, p)
  } finally {
    loading.value = false
  }
}

onMounted(load)

async function onSave() {
  saving.value = true
  try {
    await api.put('/alerts/policy', { ...formState })
    message.success('已保存')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <a-card :loading="loading">
    <a-form layout="vertical" style="max-width: 600px">
      <a-form-item
        label="静默窗口 (小时)"
        name="mute_window_hours"
        tooltip="同 hash + 同类别在该窗口内不重复推送"
      >
        <a-input-number v-model:value="formState.mute_window_hours" :min="0" :max="168" />
      </a-form-item>
      <a-form-item label="🟡 警告每日汇总一次" name="warning_daily_digest">
        <a-switch v-model:checked="formState.warning_daily_digest" />
      </a-form-item>
      <a-form-item label="🔴 严重立即推送" name="critical_immediate">
        <a-switch v-model:checked="formState.critical_immediate" />
      </a-form-item>
      <a-form-item label="风暴保护阈值（单日告警数）" name="storm_threshold">
        <a-input-number v-model:value="formState.storm_threshold" :min="1" :max="10000" />
      </a-form-item>
      <a-form-item
        label="静默时段 (HH:MM-HH:MM)"
        name="quiet_hours"
        tooltip="支持跨零点如 23:00-07:00；🔴 始终不受影响"
      >
        <a-input v-model:value="formState.quiet_hours" placeholder="如 23:00-07:00，留空=无" />
      </a-form-item>
      <a-form-item>
        <a-button type="primary" :loading="saving" @click="onSave">保存</a-button>
      </a-form-item>
    </a-form>
  </a-card>
</template>
