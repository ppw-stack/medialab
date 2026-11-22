<script setup lang="ts">
import { ref, onMounted, computed } from 'vue';
import { api, errMsg } from '../../api';
import type { EnvSettingsView } from '../../types';
import { message } from 'ant-design-vue';

/**
 * 环境变量配置（「设置」页的主体内容）。
 *
 * 服务「字符串型」DB 持久化配置（辅种扫描目录 / 未来其它 env→DB 字符串
 * 设置）。2026-11-22 起与「定时任务」是**两个独立菜单**：作业调度在
 * `/scheduler`（`pages/Scheduler.vue`，按业务模块分组），本组件留在
 * `/settings`——它是部署级设置，不是作业级配置。
 *
 * 与 SchedulerConfigModal 的区别：本页直接展示表单（不弹框）——
 * 字段少（目前仅 cross_seed_scan_dirs 一项）+ 改后立即生效，弹框徒增
 * 一次点击。若未来字段增多（>5）可考虑改成弹框。
 */
const data = ref<EnvSettingsView | null>(null);
const loading = ref(false);
const saving = ref(false);

/** 草稿值（key → 用户编辑中的字符串）。 */
const draft = ref<Record<string, string>>({});

const items = computed(() => data.value?.items ?? []);

function isDirty(): boolean {
  if (!data.value) return false;
  return items.value.some((it) => draft.value[it.key] !== it.value);
}

async function load() {
  loading.value = true;
  try {
    const r = await api.get<EnvSettingsView>('/env-settings');
    data.value = r;
    // 草稿初始化为当前值
    const next: Record<string, string> = {};
    for (const it of r.items) {
      next[it.key] = it.value;
    }
    draft.value = next;
  } catch (e) {
    message.error('加载配置失败：' + errMsg(e));
  } finally {
    loading.value = false;
  }
}

async function onSave() {
  if (!data.value) return;
  // 只传有改动的字段（partial update）
  const updates: Record<string, string> = {};
  for (const it of items.value) {
    const v = draft.value[it.key];
    if (v !== undefined && v !== it.value) {
      updates[it.key] = v;
    }
  }
  if (Object.keys(updates).length === 0) {
    return;
  }
  saving.value = true;
  try {
    const r = await api.put<EnvSettingsView>('/env-settings', updates);
    data.value = r;
    // 同步草稿（保存成功后端返回最新值）
    const next: Record<string, string> = {};
    for (const it of r.items) {
      next[it.key] = it.value;
    }
    draft.value = next;
    message.success('环境变量配置已保存（重启后保持）');
  } catch (e) {
    message.error('保存失败：' + errMsg(e));
  } finally {
    saving.value = false;
  }
}

onMounted(() => {
  load();
});
</script>

<template>
  <div>
    <div v-if="loading || !data" class="loading-row">
      <span class="mono dim">加载中…</span>
    </div>
    <div v-else>
      <section v-if="items.length" class="env-section">
        <div class="env-section__hint">
          <span class="env-section__hint-icon">ⓘ</span>
          改动会立即生效并写入数据库——重启容器后保持不变。 多个目录用逗号分隔。
        </div>

        <div v-for="it in items" :key="it.key" class="env-row">
          <label class="env-label">{{ it.label }}</label>
          <div class="env-field">
            <a-textarea
              v-model:value="draft[it.key]"
              :placeholder="it.placeholder"
              :rows="2"
              :maxlength="it.max_length"
              class="env-textarea"
            />
            <div class="env-meta">
              <span class="env-meta__note">{{ it.note }}</span>
              <span class="env-meta__count">
                {{ (draft[it.key] ?? '').length }} / {{ it.max_length }}
              </span>
            </div>
          </div>
        </div>

        <div class="env-actions">
          <a-button :disabled="saving" @click="load">取消</a-button>
          <a-button type="primary" :loading="saving" :disabled="!isDirty()" @click="onSave">
            保存
          </a-button>
        </div>
      </section>

      <div v-else class="loading-row">
        <span class="mono dim">暂无可配置项</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.loading-row {
  padding: var(--sp-4) 0;
  text-align: center;
}
.dim {
  color: var(--text-tertiary);
}
.mono {
  font-family: var(--font-mono, monospace);
}

.env-section {
  max-width: 720px;
}

.env-section__hint {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
  padding: var(--sp-3);
  margin-bottom: var(--sp-4);
  border: 1px solid var(--border-hairline);
  border-radius: var(--r-md);
  background: var(--bg-inset);
  color: var(--text-secondary);
  font-size: var(--text-xs);
  line-height: 1.5;
}
.env-section__hint-icon {
  font-weight: 700;
  flex: 0 0 auto;
}

.env-row {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-3);
  margin-bottom: var(--sp-4);
}

.env-label {
  flex: 0 0 200px;
  font-size: var(--text-sm);
  color: var(--text-primary);
  padding-top: 6px;
}

.env-field {
  flex: 1;
  min-width: 0;
}

.env-textarea {
  width: 100%;
  font-family: var(--font-mono, monospace);
  font-size: var(--text-sm);
}

.env-meta {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--sp-3);
  margin-top: var(--sp-1);
}

.env-meta__note {
  color: var(--text-tertiary);
  font-size: var(--text-xs);
  line-height: 1.5;
  flex: 1;
}

.env-meta__count {
  color: var(--text-tertiary);
  font-size: var(--text-xs);
  font-family: var(--font-mono, monospace);
  flex: 0 0 auto;
}

.env-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-2);
  margin-top: var(--sp-3);
  padding-top: var(--sp-3);
  border-top: 1px solid var(--border-hairline);
}
</style>
