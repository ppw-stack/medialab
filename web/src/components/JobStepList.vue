<script setup lang="ts">
/**
 * 作业步骤卡片列表 + 列表小圆点预览（共用 JobStep 数据结构）。
 *
 * 明细（`items`）**默认折叠**——monitor 这类作业可能几百条，
 * 默认展开会把详情面板撑爆。点击「明细（N）」才展开。
 *
 * 子步骤（`children`）**总是展开**：编排型作业（cross_seed_maintenance）
 * 把子作业的 5 条步骤折成一句 summary 后，页面上只剩三个大段——排查时
 * 必须能看到「写入快照 / 检测异常 / 自动暂停」这些真实环节。子步骤最多
 * 5 条，展开不会撑爆面板。
 */
import { ref, watch } from 'vue'
import type { JobStep } from '../types'

const props = defineProps<{
  steps: JobStep[]
  /** compact = 只显示小圆点（列表内预览） */
  compact?: boolean
}>()

/**
 * 哪些步骤的明细处于展开态。
 *
 * 用 `Set` 存步骤索引而不是组件级布尔——多个步骤各自独立展开，
 * 且步骤名可能重复（不同作业的同名步骤），索引更安全。
 */
const expanded = ref<Set<number>>(new Set())

/** 步骤内容变化时收起全部（换了运行记录，旧展开态无意义）。 */
watch(
  () => props.steps,
  () => {
    expanded.value = new Set()
  }
)

function isExpanded(idx: number): boolean {
  return expanded.value.has(idx)
}

function toggle(idx: number): void {
  const next = new Set(expanded.value)
  if (next.has(idx)) {
    next.delete(idx)
  } else {
    next.add(idx)
  }
  expanded.value = next
}

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

/** 明细条数（截断时取完整条数）。 */
function itemCount(step: JobStep): number {
  return step.items_total ?? step.items?.length ?? 0
}

/** 是否被截断（显示条数 < 完整条数）。 */
function truncated(step: JobStep): boolean {
  const total = step.items_total ?? 0
  const shown = step.items?.length ?? 0
  return total > shown
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

      <!--
        detail **独占一行**、不截断：
        之前是同行右对齐 + `white-space: nowrap` + `text-overflow: ellipsis`
        + `max-width: 50%`——详情面板窄，只有 ~200px 可用，
        「未封顶 5 站，无信号 2 站，已封顶 1 站」被砍成「...」。
        步骤的 detail 是排查时最该看清的一行，不能被省略。
      -->
      <span v-if="s.detail" class="step-detail">{{ s.detail }}</span>

      <!-- 子步骤（编排型作业）：总是展开，视觉上缩进一级 -->
      <div v-if="s.children && s.children.length" class="step-children">
        <div
          v-for="(c, k) in s.children"
          :key="k"
          class="step-child"
          :class="stepClass(c.status)"
        >
          <span class="step-child__icon">{{ statusIcon(c.status) }}</span>
          <span class="step-child__name">{{ c.name }}</span>
          <span v-if="c.count != null" class="step-child__count mono">{{ c.count }}</span>
          <span v-if="c.detail" class="step-child__detail">{{ c.detail }}</span>
        </div>
      </div>

      <!-- 明细展开按钮（默认折叠） -->
      <button
        v-if="s.items && s.items.length"
        type="button"
        class="step-items-toggle"
        @click="toggle(i)"
      >
        {{ isExpanded(i) ? '▾' : '▸' }} 明细（{{ itemCount(s) }}）
        <span v-if="truncated(s)" class="step-items-note">
          显示 {{ s.items!.length }} / 共 {{ s.items_total }}
        </span>
      </button>

      <!-- 明细列表（展开态） -->
      <div v-if="isExpanded(i) && s.items && s.items.length" class="step-items">
        <div
          v-for="(item, j) in s.items"
          :key="j"
          class="step-item"
          :class="stepClass(item.status)"
        >
          <span class="step-item__icon">{{ statusIcon(item.status) }}</span>
          <span class="step-item__name">{{ item.name }}</span>
          <span v-if="item.detail" class="step-item__detail">{{ item.detail }}</span>
        </div>
      </div>
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
  flex-wrap: wrap;
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

/*
  detail 独占一行、左对齐、**不截断**。
  之前同行右对齐 + ellipsis，窄面板里只显示得下 ~200px，
  关键信息（「未封顶 5 站，无信号 2 站，已封顶 1 站」）被砍成「...」。
*/
.step-detail {
  flex: 0 0 100%;
  color: var(--text-secondary);
  white-space: normal;
  overflow: visible;
  word-break: break-word;
  line-height: 1.5;
}

/* 子步骤（缩进一级，与父步骤的 detail 区分开） */
.step-children {
  flex: 0 0 100%;
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: var(--sp-1);
  padding-left: var(--sp-2);
  border-left: 1px solid var(--border-hairline);
}
.step-child {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  font-size: var(--text-xs);
  line-height: 1.5;
}
.step-child__icon {
  font-weight: 700;
  width: 12px;
  flex: 0 0 12px;
  text-align: center;
}
.step-child--ok .step-child__icon { color: var(--success-text, #52c41a); }
.step-child--warn .step-child__icon { color: var(--warning-text, #d97706); }
.step-child--skip .step-child__icon { color: var(--text-tertiary); }
.step-child--fail .step-child__icon { color: var(--error-text, #f5222d); }
.step-child__name {
  color: var(--text-secondary);
  flex: 0 0 auto;
}
.step-child__count {
  color: var(--text-secondary);
  font-weight: 600;
}
.step-child__detail {
  color: var(--text-tertiary);
  flex: 1 1 auto;
  min-width: 0;
  word-break: break-word;
}

/* 明细展开按钮 */
.step-items-toggle {
  flex: 0 0 100%;
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  margin-top: var(--sp-1);
  padding: 2px 0;
  border: 0;
  background: none;
  color: var(--text-tertiary);
  font-family: var(--font-sans);
  font-size: var(--text-xs);
  cursor: pointer;
  text-align: left;
}
.step-items-toggle:hover {
  color: var(--accent-red);
}
.step-items-note {
  color: var(--text-tertiary);
  font-family: var(--font-mono, monospace);
  font-size: var(--text-2xs);
}

/* 明细列表 */
.step-items {
  flex: 0 0 100%;
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: var(--sp-1);
  padding: var(--sp-1) var(--sp-2);
  border-radius: var(--r-sm);
  background: var(--bg-inset);
}
.step-item {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  font-size: var(--text-xs);
  line-height: 1.5;
}
.step-item__icon {
  font-weight: 700;
  width: 12px;
  flex: 0 0 12px;
  text-align: center;
}
.step-item--ok .step-item__icon { color: var(--success-text, #52c41a); }
.step-item--warn .step-item__icon { color: var(--warning-text, #d97706); }
.step-item--skip .step-item__icon { color: var(--text-tertiary); }
.step-item--fail .step-item__icon { color: var(--error-text, #f5222d); }

.step-item__name {
  color: var(--text-primary);
  font-weight: 500;
  flex: 0 1 auto;
  min-width: 0;
  word-break: break-word;
}
.step-item__detail {
  color: var(--text-tertiary);
  flex: 1 1 auto;
  min-width: 0;
  word-break: break-word;
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
