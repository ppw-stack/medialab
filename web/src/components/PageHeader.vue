<script setup lang="ts">
/**
 * 页面标题区（统一两个业务域的页面骨架）。
 *
 * 背景：辅种域用 `.page-header`（eyebrow + 大标题 + 副标题 + 右侧
 * 动作区），媒体库域用 `a-card` 的 `#title`——**两套不同的布局契约**。
 * 后果之一是手机端没法统一折叠：媒体库域没有「页面级标题」这个概念，
 * 标题混在卡片里。
 *
 * 统一后：两个域都用本组件，页面标题成为**稳定契约**，
 * 手机端可以据此做「滚动时收起标题、只留一行」的折叠。
 */
import { useResponsive } from '../composables/useResponsive';

defineProps<{
  /** 小标签（英文 + 分类），如「下载器 · DOWNLOADERS」。 */
  eyebrow?: string;
  /** 主标题。 */
  title: string;
  /** 副标题（一行说明）。 */
  sub?: string;
  /** 是否在手机端隐藏副标题（空间紧张时）。 */
  hideSubOnMobile?: boolean;
  /**
   * 紧凑模式——横向 padding 用 16px（媒体库域的 `.page` 容器窄）。
   * 辅种域默认 32px。
   */
  compact?: boolean;
}>();

const { isMobile } = useResponsive();
</script>

<template>
  <header class="page-header" :class="{ 'page-header--compact': compact }">
    <div class="page-header__main">
      <div v-if="eyebrow" class="page-header__eyebrow">{{ eyebrow }}</div>
      <h1 class="page-header__title">{{ title }}</h1>
      <div v-if="sub && !(hideSubOnMobile && isMobile)" class="page-header__sub">
        <slot name="sub">{{ sub }}</slot>
      </div>
    </div>
    <!-- 动作区：手机端换行到标题下方 -->
    <div v-if="$slots.actions" class="page-header__actions">
      <slot name="actions" />
    </div>
  </header>
</template>

<style scoped>
/*
  标题区布局：
  - 桌面：标题左、动作右（同一行，底部对齐）
  - 手机：动作换行到标题下方（flex-wrap + 主区占满宽）

  ⚠️ padding 必须写在这里——scoped 的 `.page-header` 带属性选择器，
  优先级**高于** global.css 的同名规则，global.css 里的 padding 会被
  覆盖掉。辅种域的 `.page-header` 有 32px 横向 padding，媒体库域的
  `.page` 是 16px——统一后由本组件自己决定。
*/
.page-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--sp-4);
  flex-wrap: wrap;
  padding: var(--sp-8) var(--sp-8) var(--sp-5);
}

.page-header__main {
  min-width: 0;
  flex: 1 1 auto;
  word-break: break-word;
}

/* 紧凑模式：媒体库域的容器只有 16px 内边距，32px 会显得空 */
.page-header--compact {
  padding: var(--sp-5) var(--sp-4) var(--sp-3);
}

.page-header__actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  flex: 0 0 auto;
}

/* 平板：横向 padding 收紧 */
@media (max-width: 991px) {
  .page-header {
    padding: var(--sp-5) var(--sp-5) var(--sp-4);
  }
}

/* 手机端：动作区占满一行，紧跟标题下方 */
@media (max-width: 767px) {
  .page-header {
    align-items: flex-start;
    flex-direction: column;
    gap: var(--sp-3);
    padding: var(--sp-4) var(--sp-3) var(--sp-3);
  }

  .page-header__actions {
    width: 100%;
  }
}
</style>
