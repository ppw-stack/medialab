<script setup lang="ts">
/**
 * cron 表达式可视化构建器。
 *
 * **不用现成组件的原因**：npm 上的 Vue cron 构建器都是 2022 年的、基于
 * Element 系（vue-cron-picker / vue3-cron），与本项目的 Ant Design Vue
 * 风格不统一，强行引入还要带一整套 Element Plus。故自建。
 *
 * **每个字段都能选预设或写自定义**（无「高级模式」开关——粒度更细，
 * 不用在两种界面间切换）。预设覆盖不到时落「自定义」，仍可编辑。
 */
import { computed, nextTick, ref, watch } from 'vue';
import {
  DAY_PRESETS,
  HOUR_PRESETS,
  MINUTE_PRESETS,
  MONTH_PRESETS,
  WEEKDAY_PRESETS,
  type CronFieldsState,
  type CronFieldState,
  cronToState,
  describeCron,
  formatNextRuns,
  isCronShapeValid,
  stateToCron,
} from '../utils/cron';

const props = defineProps<{
  /** 当前 cron 表达式（v-model）。 */
  modelValue: string;
}>();

const emit = defineEmits<(e: 'update:modelValue', v: string) => void>();

/** 字段定义（顺序 = cron 的 5 字段顺序）。 */
const FIELDS: {
  key: keyof CronFieldsState;
  label: string;
  presets: { value: string; label: string }[];
  placeholder: string;
}[] = [
  { key: 'minute', label: '分钟', presets: MINUTE_PRESETS, placeholder: '如 5,15,25 或 */10' },
  { key: 'hour', label: '小时', presets: HOUR_PRESETS, placeholder: '如 */3 或 8-20' },
  { key: 'day', label: '日', presets: DAY_PRESETS, placeholder: '如 1-7' },
  { key: 'month', label: '月', presets: MONTH_PRESETS, placeholder: '如 */3' },
  { key: 'weekday', label: '星期', presets: WEEKDAY_PRESETS, placeholder: '如 1-5（周一至周五）' },
];

const state = ref<CronFieldsState>(cronToState(props.modelValue));

/**
 * **内部更新中**标志——区分两个 watch 的方向，避免互相触发。
 *
 * 两个 watch 会互相触发（props 变 → 重建 state → state 变 → emit）：
 * - 外部改（父组件改了 modelValue）→ 重建 state，**不该**再 emit
 * - 内部改（用户选了预设）→ emit，**不该**再重建 state
 *
 * 没有标志位时靠「字符串相等」隐式打破循环——一旦出现「语义相同但
 * 字符串不同」的表达式（如 `0 8 * * *` vs `00 8 * * *`），就会来回弹跳。
 */
let applyingExternal = false;

// 外部值变化 → 同步回编辑状态（用户可能从别处改了 cron）
watch(
  () => props.modelValue,
  (v) => {
    const next = stateToCron(state.value);
    if (next === v) return;
    applyingExternal = true;
    state.value = cronToState(v);
    // 下一个 tick 复位（watch 回调是同步的，赋值会立即触发下方 watch）
    // 用 Vue 的 nextTick 而不是 Promise.resolve().then()——后者只是下一个
    // 微任务，nextTick 等的是**调度队列 flush 完**，才是这里真正要的语义。
    void nextTick(() => {
      applyingExternal = false;
    });
  },
);

// 编辑状态变化 → 生成表达式并抛给父组件
watch(
  state,
  (s) => {
    if (applyingExternal) return;
    const expr = stateToCron(s);
    if (expr !== props.modelValue) emit('update:modelValue', expr);
  },
  { deep: true },
);

/** 当前表达式。 */
const expr = computed(() => stateToCron(state.value));

/** 人类可读描述（cronstrue）。 */
const description = computed(() => describeCron(expr.value));

/** 形状校验（不合法时提示，最终以��端校验为准）。 */
const shapeOk = computed(() => isCronShapeValid(expr.value));

/** 下次运行时间（前端算，零请求）。 */
const nextRuns = computed(() => (shapeOk.value ? formatNextRuns(expr.value, 3) : []));

/** 某字段当前是否用自定义（决定显示输入框）。 */
function isCustom(f: CronFieldState): boolean {
  return f.preset === '';
}

/** 切换某字段的预设：选回预设时清掉自定义串。 */
function onPresetChange(key: keyof CronFieldsState, value: string) {
  const field = state.value[key];
  field.preset = value;
  if (value !== '') field.custom = value;
}

/** 编辑某字段的自定义表达式。 */
function onCustomChange(key: keyof CronFieldsState, value: string) {
  state.value[key].custom = value;
}
</script>

<template>
  <div class="cron-builder">
    <div class="cron-fields">
      <div v-for="f in FIELDS" :key="f.key" class="cron-field">
        <label class="cron-field__label">{{ f.label }}</label>
        <a-select
          :value="state[f.key].preset"
          size="small"
          class="cron-field__select"
          :options="[
            ...f.presets.map((p) => ({ value: p.value, label: p.label })),
            { value: '', label: '自定义…' },
          ]"
          @change="(v: string) => onPresetChange(f.key, v)"
        />
        <a-input
          v-if="isCustom(state[f.key])"
          :value="state[f.key].custom"
          size="small"
          class="cron-field__custom"
          :placeholder="f.placeholder"
          @update:value="(v: string) => onCustomChange(f.key, v)"
        />
      </div>
    </div>

    <div class="cron-preview">
      <div class="cron-preview__expr">
        <span class="cron-preview__label">表达式</span>
        <code class="mono">{{ expr }}</code>
      </div>
      <div v-if="description" class="cron-preview__desc">
        <span class="cron-preview__label">含义</span>
        <span>{{ description }}</span>
      </div>
      <div v-if="!shapeOk" class="cron-preview__invalid">
        ⚠️ 表达式格式不对（应为 5 字段：分 时 日 月 星期）
      </div>
      <div v-else-if="nextRuns.length" class="cron-preview__next">
        <span class="cron-preview__label">下次运行</span>
        <span class="mono">{{ nextRuns.join('\u3000·\u3000') }}</span>
      </div>
      <div v-else class="cron-preview__invalid">⚠️ 该表达式在未来一年内不会触发，请检查</div>
    </div>
  </div>
</template>

<style scoped>
.cron-builder {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}

/* 5 个字段横排——窄屏时换行 */
.cron-fields {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.cron-field {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
}

.cron-field__label {
  font-size: var(--text-xs);
  color: var(--text-tertiary);
  flex: 0 0 auto;
  min-width: 28px;
}

.cron-field__select {
  width: 130px;
}

/* 自定义输入框——比预设宽，要能放下 "5,15,25,35,45,55" */
.cron-field__custom {
  width: 150px;
}

.cron-preview {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding: var(--sp-2) var(--sp-3);
  background: var(--bg-inset);
  border: 1px solid var(--border-hairline);
  border-radius: var(--r-sm);
  font-size: var(--text-xs);
  color: var(--text-secondary);
}

.cron-preview__label {
  color: var(--text-tertiary);
  margin-right: var(--sp-2);
}

.cron-preview__expr code {
  color: var(--text-primary);
}

.cron-preview__invalid {
  color: var(--warning-text, #d97706);
}
</style>
