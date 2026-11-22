<script setup lang="ts" generic="T extends Record<string, any>">
/**
 * 表格包装：自动横向滚动 + 固定布局。
 *
 * 背景：全站 20 个表格里**只有 2 个**配了 `:scroll`（Ant 默认
 * `table-layout: auto` —— 浏览器按内容重新分配列宽，窄容器里把页面
 * 整个撑开）。实测 375px 视口下 `/sites` 文档宽 1503px、`/downloaders`
 * 1551px，而视口只有 375px。
 *
 * 本组件做两件事：
 *   ① 按列宽总和注入 `:scroll="{ x }"` —— 表格在窄屏里**横向滚动**，
 *      不再把页面撑破
 *   ② 注入 `table-layout: fixed` —— 列宽严格生效（`auto` 时列宽只是
 *      「建议值」，浏览器仍会按内容重分配，导致设了宽度也白设）
 *
 * **列宽怎么算**：显式 `width` 优先；没给 `width` 的列按
 * `minWidth` → 默认 120px 兜底。宁可多给一点（多出来的空白在滚动条
 * 右侧），也不要少给（少给会被压缩，文字截断）。
 */
import { computed, useSlots } from 'vue';

const props = withDefaults(
  defineProps<{
    columns: readonly any[];
    /** 列宽总和的**下限**——列少 / 列窄时保证一个合理的最小滚动宽度。 */
    minScrollX?: number;
    /**
     * 表格布局。默认 `fixed`（列宽严格生效）。
     *
     * `auto` 时列宽只是「建议值」，浏览器按内容重新分配——设了宽度也
     * 白设（表现为「列宽忽宽忽窄」「文字莫名换行」）。
     */
    tableLayout?: 'fixed' | 'auto';
  }>(),
  { minScrollX: 0, tableLayout: 'fixed' },
);

const slots = useSlots();

/** 单列占位宽——没给 width 也没给 minWidth 的列。 */
const DEFAULT_COL_WIDTH = 120;

/** 列宽总和。 */
const totalWidth = computed(() => {
  let sum = 0;
  for (const col of props.columns) {
    // 列组（children）按其子列累加
    if (Array.isArray((col as any).children)) {
      for (const child of (col as any).children) {
        sum += columnWidth(child);
      }
      continue;
    }
    sum += columnWidth(col);
  }
  return sum;
});

function columnWidth(col: any): number {
  const w = col?.width;
  if (typeof w === 'number' && w > 0) return w;
  if (typeof w === 'string') {
    const n = parseFloat(w);
    if (Number.isFinite(n) && n > 0) return n;
  }
  const mw = col?.minWidth;
  if (typeof mw === 'number' && mw > 0) return mw;
  return DEFAULT_COL_WIDTH;
}

/**
 * 横向滚动配置。
 *
 * **总是**注入 `:scroll="{ x }"`，x = 列宽总和。
 *
 * - 窄屏：表格在容器内横向滚动，**不把页面撑宽**（关键——否则
 *   Ant 的 `table-layout:auto` 会按内容撑开，整个页面出现横向滚动条，
 *   实测 `/sites` 文档宽 1503px、视口 375px）
 * - 桌面端：列宽总和 ≤ 容器时不会真的出现滚动条（`:scroll.x` 只是
 *   「最小宽度」）；> 容器时出现横滚——这是**正确行为**（表格内容
 *   本来就装不下，如 `/downloaders` 的 13 列）
 */
const scrollX = computed(() => ({
  x: Math.max(totalWidth.value, props.minScrollX),
}));
</script>

<template>
  <a-table :columns="columns" :scroll="{ x: scrollX }" :table-layout="tableLayout" v-bind="$attrs">
    <!--
      透传所有具名插槽（bodyCell / expandedRowRender / title 等）。

      Vue 3.3+ 支持 `<template v-for>` + 动态 slot 名（`#[name]`）——
      这样新增表格只需在本组件里写一次插槽转发，各页面不用重复列举。
    -->
    <template v-for="(_, name) in slots" #[name]="slotProps">
      <slot :name="name" v-bind="slotProps ?? {}" />
    </template>
  </a-table>
</template>
