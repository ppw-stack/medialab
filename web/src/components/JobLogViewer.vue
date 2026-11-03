<script setup lang="ts">
/**
 * 作业执行日志查看器。
 *
 * 展示作业执行期间的 Python 日志（时序视角：哪步慢、中间发生了什么）。
 * 与步骤卡片的「逐项明细」互补：
 * - 明细回答「**是哪些**」（哪些站、哪些种子）
 * - 日志回答「**什么时候 / 多慢**」
 *
 * 数据来自 `result.logs`——**只有详情端点返回**（列表端点为控制
 * 响应体积会剥离它）。
 */
import { computed, ref, watch, nextTick } from 'vue'
import { message } from 'ant-design-vue'
import type { JobLogEntry } from '../types'

const props = defineProps<{
  logs: JobLogEntry[] | null | undefined
}>()

/** 只看警告+错误（默认关——成功运行时 INFO 是主要信息）。 */
const onlyProblems = ref(false)

const logBox = ref<HTMLElement | null>(null)

const visible = computed<JobLogEntry[]>(() => {
  const list = props.logs ?? []
  if (!onlyProblems.value) return list
  return list.filter((l) => l.level === 'WARNING' || l.level === 'ERROR' || l.level === 'CRITICAL')
})

/** 时间戳（``HH:MM:SS``）。 */
function logTime(ts: string): string {
  return ts.length >= 19 ? ts.substring(11, 19) : ts
}

/** logger 名的末段（``app.services.jobs`` → ``jobs``）——省宽度。 */
function shortLogger(name: string): string {
  const parts = name.split('.')
  return parts[parts.length - 1] || name
}

function levelClass(level: string): string {
  switch (level) {
    case 'ERROR':
    case 'CRITICAL':
      return 'log-line--error'
    case 'WARNING':
      return 'log-line--warn'
    case 'DEBUG':
      return 'log-line--debug'
    default:
      return 'log-line--info'
  }
}

/** 新日志到达时滚到底。 */
watch(
  () => visible.value.length,
  async () => {
    await nextTick()
    if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight
  }
)

async function copyAll(): Promise<void> {
  const text = visible.value
    .map((l) => `[${logTime(l.ts)}] ${l.level} ${l.logger}: ${l.message}`)
    .join('\n')
  if (!text) {
    message.warning('没有可复制的日志')
    return
  }
  try {
    await navigator.clipboard.writeText(text)
    message.success(`已复制 ${visible.value.length} 条日志`)
  } catch {
    message.error('复制失败（浏览器不允许）')
  }
}
</script>

<template>
  <div class="log-viewer">
    <div class="log-toolbar">
      <span class="dim mono">{{ visible.length }} / {{ (logs ?? []).length }} 条</span>
      <label class="log-toggle">
        <input v-model="onlyProblems" type="checkbox" />
        <span class="dim">仅警告+错误</span>
      </label>
      <button type="button" class="log-copy" @click="copyAll">复制</button>
    </div>

    <div v-if="!logs || !logs.length" class="log-empty dim">
      本次运行没有采集到日志
    </div>

    <div v-else ref="logBox" class="log-box">
      <div v-for="(l, i) in visible" :key="i" class="log-line" :class="levelClass(l.level)">
        <span class="log-time mono">{{ logTime(l.ts) }}</span>
        <span class="log-level mono">{{ l.level }}</span>
        <span class="log-logger mono">{{ shortLogger(l.logger) }}</span>
        <span class="log-text">{{ l.message }}</span>
      </div>
      <div v-if="!visible.length" class="log-empty dim">没有符合筛选条件的日志</div>
    </div>
  </div>
</template>

<style scoped>
.dim { color: var(--text-tertiary); }
.mono { font-family: var(--font-mono, monospace); }

.log-viewer {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.log-toolbar {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  font-size: var(--text-xs);
}

.log-toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  cursor: pointer;
}

.log-copy {
  border: 1px solid var(--border-hairline);
  background: var(--bg-surface);
  color: var(--text-secondary);
  border-radius: var(--r-sm);
  padding: 1px var(--sp-2);
  font-size: var(--text-xs);
  cursor: pointer;
}
.log-copy:hover {
  color: var(--accent-red);
  border-color: var(--border-strong);
}

.log-box {
  max-height: 260px;
  overflow-y: auto;
  border: 1px solid var(--border-hairline);
  border-radius: var(--r-sm);
  padding: var(--sp-2);
  background: var(--bg-inset);
  font-family: var(--font-mono, monospace);
  font-size: var(--text-xs);
}

.log-line {
  display: flex;
  gap: var(--sp-2);
  align-items: baseline;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.log-time {
  color: var(--text-tertiary);
  flex: 0 0 auto;
}
.log-level {
  flex: 0 0 auto;
  font-weight: 600;
  min-width: 52px;
}
.log-logger {
  color: var(--text-tertiary);
  flex: 0 0 auto;
  min-width: 88px;
}
.log-text {
  color: var(--text-primary);
  flex: 1 1 auto;
  min-width: 0;
}

.log-line--info .log-level { color: var(--text-secondary); }
.log-line--debug .log-level { color: var(--text-tertiary); }
.log-line--warn .log-level { color: var(--warning-text, #d97706); }
.log-line--error .log-level { color: var(--error-text, #f5222d); }
.log-line--error .log-text { color: var(--error-text, #f5222d); }

.log-empty {
  padding: var(--sp-3);
  text-align: center;
  font-size: var(--text-xs);
}
</style>
