/**
 * 响应式断点（单一入口）。
 *
 * 背景：此前全站**零** `@media` 查询、零断点判断——侧栏恒占 280px，
 * 375px 视口下内容区只剩 95px（实测 13 个页面全是 95px），表格还会
 * 把页面撑出横向滚动（`/sites` 文档宽 1503px、`/downloaders` 1551px）。
 *
 * 约定：**所有**需要按屏幕宽度调整的组件都从这里取判断，不要各自
 * 写 `window.innerWidth`——那会散落成灾、且不响应窗口尺寸变化。
 *
 * 断点用 Ant 的标准档（与 `a-col` 的 `xs`/`sm`/`md`/`lg` 一致）：
 *   xs  < 576px   手机竖屏
 *   sm  < 768px   手机横屏 / 小平板
 *   md  < 992px   平板
 *   lg  ≥ 992px   桌面（本项目的「PC 端」基准）
 */
import { computed } from 'vue'
import { Grid } from 'ant-design-vue'

/** 桌面基准线——与 `lg` 断点一致。 */
export const DESKTOP_MIN = 992

/** 平板上限——`md` 断点。 */
export const TABLET_MAX = 992

export function useResponsive() {
  const screens = Grid.useBreakpoint()

  /**
   * 是否手机（< 768px）。
   *
   * 用 `sm` 而不是 `xs`：xs 的 576px 以下太窄（iPhone SE 是 375px，
   * 但折叠屏展开、某些安卓机是 540px），且 Ant 的 `xs` 语义是
   * 「超小屏」而非「手机」。
   */
  const isMobile = computed(() => screens.value.sm === false)

  /** 是否平板（768px ~ 992px）。 */
  const isTablet = computed(
    () => screens.value.sm === true && screens.value.md === false,
  )

  /** 是否桌面（≥ 992px）——本项目的主力形态。 */
  const isDesktop = computed(() => screens.value.md === true)

  /**
   * 是否「窄于桌面」——需要在手机上收紧间距 / 折叠侧栏 / 单列布局。
   *
   * 注意与 `isMobile` 区分：这个更宽（把平板也算进来），适合
   * 「减少 padding」这类温和调整；`isMobile` 更严，适合
   * 「改布局结构」这类激进调整。
   */
  const isCompact = computed(() => screens.value.md === false)

  return { screens, isMobile, isTablet, isDesktop, isCompact }
}
