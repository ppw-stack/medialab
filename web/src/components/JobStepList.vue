<script setup lang="ts">
/** 作业步骤卡片列表 + 列表小圆点预览（共用 JobStep 数据结构）。 */
import type { JobStep } from '../types'

defineProps<{
  steps: JobStep[]
  /** compact = 只显示小圆点（列表内预览） */
  compact?: boolean
}>()

function statusIcon(s: string): string {
  switch (s) {
    case 'ok': return '✓'
    case 'warn': return '⚠'
    case 'fail': return '✕'
    default: return '○'
  }
}

function stepClass(s: string): string {
  return `step--${s}`
}
</script>

<template>
  <!-- 紧凑模式：小圆点预览 -->
  <span v-if="compact" class="step-dots">
    <span
      v-for="(s, i) in steps"
      :key="i"
      class="step-dot"
      :class="stepClass(s.status)"
      :title="`${s.name} — ${s.detail ?? ''}`"
    />
  </span>

  <!-- 完整模式：步骤卡片 -->
  <div v-else class="steps-list">
    <div
      v-for="(s, i) in steps"
      :key="i"
      class="step-card"
      :class="stepClass(s.status)"
    >
      <span class="step-icon">{{ statusIcon(s.status) }}</span>
      <span class="step-name">{{ s.name }}</span>
      <span v-if="s.count != null" class="step-count mono">{{ s.count }}</span>
      <span v-if="s.detail" class="step-detail">{{ s.detail }}</span>
    </div>
  </div>
</template>

<style scoped>
.mono { font-family: var(--font-mono, monospace); }

/* 步骤卡片 */
.steps-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  margin-top: var(--sp-2);
}
.step-card {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  padding: var(--sp-2);
  border-left: 3px solid var(--text-tertiary);
  border-radius: 0 var(--r-sm) var(--r-sm) 0;
  background: var(--bg-surface);
  font-size: var(--text-xs);
}
.step--ok { border-left-color: var(--success-text, #52c41a); }
.step--warn { border-left-color: var(--warning-text, #d97706); }
.step--skip { border-left-color: var(--text-tertiary); opacity: 0.7; }
.step--fail { border-left-color: var(--error-text, #f5222d); }

.step-icon {
  font-weight: 700;
  width: 14px;
  flex: 0 0 14px;
  text-align: center;
}
.step--ok .step-icon { color: var(--success-text, #52c41a); }
.step--warn .step-icon { color: var(--warning-text, #d97706); }
.step--skip .step-icon { color: var(--text-tertiary); }
.step--fail .step-icon { color: var(--error-text, #f5222d); }

.step-name {
  font-weight: 500;
  color: var(--text-primary);
  flex: 0 0 auto;
}
.step-count {
  color: var(--text-secondary);
  font-weight: 600;
}
.step-detail {
  color: var(--text-tertiary);
  margin-left: auto;
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 50%;
}

/* 列表里的步骤小圆点 */
.step-dots {
  display: inline-flex;
  gap: 3px;
  align-items: center;
}
.step-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  display: inline-block;
  background: var(--text-tertiary);
}
.step-dot.step--ok { background: var(--success-text, #52c41a); }
.step-dot.step--warn { background: var(--warning-text, #d97706); }
.step-dot.step--skip { background: var(--border-hairline); }
.step-dot.step--fail { background: var(--error-text, #f5222d); }
</style>
