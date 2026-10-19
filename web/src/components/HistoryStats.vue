<script setup lang="ts">
/** 作业历史顶部统计条：成功率 / 平均耗时 / 最长耗时 / 最近结果摘要。 */
defineProps<{
  okPct: number
  okCount: number
  total: number
  avgMs: number
  maxMs: number
  summary: string
}>()

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60000)}m${Math.round((ms % 60000) / 1000)}s`
}
</script>

<template>
  <div class="history-stats">
    <span
      class="stat-pill"
      :class="okCount < total ? 'stat-pill--warn' : 'stat-pill--ok'"
    >
      {{ okPct }}% ({{ okCount }}/{{ total }})
    </span>
    <span class="stat-pill">平均 {{ formatDuration(avgMs) }}</span>
    <span class="stat-pill">最长 {{ formatDuration(maxMs) }}</span>
    <span class="stat-pill stat-pill--muted">最近 {{ summary }}</span>
  </div>
</template>

<style scoped>
.history-stats {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
}
.stat-pill {
  display: inline-flex;
  align-items: center;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: var(--text-xs);
  font-family: var(--font-mono, monospace);
  border: 1px solid var(--border-hairline);
  background: var(--bg-inset);
  color: var(--text-secondary);
}
.stat-pill--ok {
  border-color: var(--success-border, rgba(82, 196, 26, 0.5));
  color: var(--success-text, #52c41a);
}
.stat-pill--warn {
  border-color: var(--error-border, rgba(245, 34, 45, 0.5));
  color: var(--error-text, #f5222d);
}
.stat-pill--muted {
  color: var(--text-tertiary);
}
</style>
