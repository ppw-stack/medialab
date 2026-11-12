<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import {
  DashboardOutlined,
  CloudServerOutlined,
  ApiOutlined,
  SettingOutlined,
  LogoutOutlined,
  UserOutlined,
  DatabaseOutlined,
  SafetyOutlined,
  FolderOpenOutlined,
  CloudUploadOutlined,
  HeartOutlined,
  SwapOutlined,
  UploadOutlined,
  DatabaseFilled,
  MenuOutlined,
  FileImageOutlined,
  ShareAltOutlined,
} from '@ant-design/icons-vue'
import { useAuthStore } from '../stores/auth'
import { useResponsive } from '../composables/useResponsive'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const collapsed = ref(false)

const { isMobile } = useResponsive()

/**
 * 侧栏在手机上的展开态（抽屉）。
 *
 * 桌面端侧栏常驻；手机端改成**覆盖式抽屉**（`collapsedWidth: 0`），
 * 否则 280px 的侧栏会把内容区挤到只剩 95px（375px 视口实测）。
 */
const drawerOpen = ref(false)

/**
 * 侧边栏菜单（**两个业务域，两级菜单**）。
 *
 * 一级 = 业务域（辅种 / 媒体库），可点击展开/收起；
 * 二级 = 各域的叶子页面。
 *
 * 自动展开行为：当前路由所在的一级组始终展开，另一组可手动收起/展开。
 * 切换路由时强制展开目标组（避免点了二级菜单但一级是收起的）。
 *
 * 辅种域含「设置」（/settings，调度作业 + 环境变量两个 tab）；媒体库域
 * 的「签名密钥」用 /media-settings（避免与辅种域的 /settings 撞名）。
 */
const menuGroups = [
  {
    key: 'seed',
    title: '辅种',
    icon: SafetyOutlined,
    items: [
      { key: '/', icon: DashboardOutlined, label: '总览', sub: '仪表盘概览', iconColor: 'red' },
      { key: '/sites', icon: CloudServerOutlined, label: '站点', sub: 'NexusPHP 适配', iconColor: 'violet' },
      { key: '/downloaders', icon: ApiOutlined, label: '下载器与任务', sub: '实例 / 实时任务', iconColor: 'green' },
      { key: '/torrent-index', icon: DatabaseOutlined, label: '拉取索引', sub: '免费种源', iconColor: 'amber' },
      { key: '/cross-seed', icon: SafetyOutlined, label: '辅种统计', sub: '按下载器分组展示', iconColor: 'gold' },
      { key: '/settings', icon: SettingOutlined, label: '设置', sub: '调度作业 / 环境变量', iconColor: 'pink' },
    ],
  },
  {
    key: 'media',
    title: '媒体库',
    icon: DatabaseFilled,
    items: [
      { key: '/drivers', icon: DatabaseFilled, label: '存储源', sub: '网盘账号', iconColor: 'violet' },
      { key: '/library', icon: FolderOpenOutlined, label: '媒体库', sub: 'STRM 条目', iconColor: 'blue' },
      { key: '/migrations', icon: SwapOutlined, label: '迁移', sub: '115 → 光鸭', iconColor: 'amber' },
      { key: '/relays', icon: ShareAltOutlined, label: '转存', sub: '光鸭 → 光鸭', iconColor: 'cyan' },
      { key: '/scrape', icon: FileImageOutlined, label: '整理与刮削', sub: '下载 → 媒体库 → NFO', iconColor: 'purple' },
      { key: '/uploads', icon: UploadOutlined, label: '数据同步', sub: '本地 → 网盘', iconColor: 'green' },
      { key: '/health', icon: HeartOutlined, label: '健康监控', sub: '断链检查', iconColor: 'red' },
      { key: '/dav-users', icon: CloudUploadOutlined, label: 'WebDAV', sub: '账号与审计', iconColor: 'cyan' },
      { key: '/media-settings', icon: SettingOutlined, label: '签名密钥', sub: '中转签名轮换', iconColor: 'pink' },
    ],
  },
]

/** 全部菜单项（用于「当前页」标题查找）。 */
const allItems = computed(() => menuGroups.flatMap((g) => g.items))

const selectedKeys = computed(() => [route.path])
const activeMenu = computed(() => allItems.value.find((m) => m.key === route.path))

/**
 * 当前路由所属的一级组 key（用于自动展开）。
 * '/' 属于辅种域（总览）。
 */
function groupOfPath(path: string): string {
  const group = menuGroups.find((g) => g.items.some((it) => it.key === path))
  return group?.key ?? 'seed'
}

/** 展开的一级组 key 列表。初始展开当前路由所在组。 */
const openKeys = ref<string[]>([groupOfPath(route.path)])

/**
 * 路由变化时自动展开目标组。
 *
 * 用 watch 而非 computed：用户手动展开另一组的行为要保留（openKeys 是
 * 可写的 ref），只在路由切换时**追加**目标组到 openKeys，不覆盖用户
 * 已展开的其它组。
 */
watch(
  () => route.path,
  (path) => {
    const target = groupOfPath(path)
    if (!openKeys.value.includes(target)) {
      openKeys.value = [...openKeys.value, target]
    }
  },
)

function onMenuClick({ key }: { key: string }) {
  router.push(key)
  // 手机端点了菜单就收起抽屉（否则新页面被遮罩挡住）
  drawerOpen.value = false
}

async function handleLogout() {
  await auth.logout()
  router.push('/login')
}

</script>

<template>
  <a-layout style="min-height: 100vh">
    <!--
      侧栏：桌面常驻（280px），手机**完全隐藏**。

      ⚠️ **不能用** Ant Sider 的 `collapsedWidth: 0`——那只是把宽度收成 0，
      但侧栏自身是 `overflow: visible`，菜单内容（24px）仍会**溢出**到
      页面左上角（实测「站点适配器」标题被压在下面）。Ant 为此配的
      `zeroWidthTrigger`（黑色浮标）又会和我自己的 header 汉堡按钮
      **重复**。

      故手机端直接 `display: none` 侧栏，导航完全交给下方的
      `<a-drawer>`。
    -->
    <a-layout-sider
      v-model:collapsed="collapsed"
      collapsible
      :width="280"
      :class="{ 'layout-sider--mobile-hidden': isMobile }"
    >
      <div class="brand">
        <span class="brand__dot" />
        <template v-if="!collapsed">
          <span class="brand__name">MediaLab</span>
          <span class="brand__name-mono">v0.2</span>
        </template>
      </div>
      <a-menu
        mode="inline"
        v-model:open-keys="openKeys"
        :selected-keys="selectedKeys"
        @click="onMenuClick"
      >
        <a-sub-menu
          v-for="group in menuGroups"
          :key="group.key"
        >
          <template #title>
            <span class="menu-icon menu-icon--group">
              <component :is="group.icon" />
            </span>
            <span v-if="!collapsed" class="menu-group-title">{{ group.title }}</span>
          </template>
          <a-menu-item v-for="item in group.items" :key="item.key">
            <span class="menu-icon" :class="`menu-icon--${item.iconColor}`">
              <component :is="item.icon" />
            </span>
            <span v-if="!collapsed" class="menu-label">
              <span class="menu-label__name">{{ item.label }}</span>
              <span class="menu-label__sub">{{ item.sub }}</span>
            </span>
          </a-menu-item>
        </a-sub-menu>
      </a-menu>
    </a-layout-sider>
    <a-layout>
      <a-layout-header>
        <!-- 手机端汉堡按钮（侧栏收起时才有入口） -->
        <a-button
          v-if="isMobile"
          type="text"
          class="header-menu-btn"
          aria-label="打开菜单"
          @click="drawerOpen = true"
        >
          <MenuOutlined />
        </a-button>
        <div class="header-title">
          <span class="header-title__name">{{ activeMenu?.label ?? '总览' }}</span>
          <span class="header-title__sub">{{ activeMenu?.sub ?? '' }}</span>
        </div>
        <a-dropdown :trigger="['click']">
          <span class="user-chip">
            <a-avatar :size="28"><UserOutlined style="font-size: 13px" /></a-avatar>
            <span class="user-chip__name">{{ auth.username }}</span>
          </span>
          <template #overlay>
            <a-menu>
              <a-menu-item key="logout" @click="handleLogout">
                <LogoutOutlined />
                <span>退出登录</span>
              </a-menu-item>
            </a-menu>
          </template>
        </a-dropdown>
      </a-layout-header>
      <a-layout-content>
        <RouterView />
      </a-layout-content>
    </a-layout>

    <!--
      手机端的**抽屉菜单**。

      侧栏用 `collapsedWidth: 0` 收起来后，菜单内容就不可见了——
      需要另一处把同样的菜单渲染出来。这里复用同一个 `menuGroups` 数据
      与 `openKeys` 状态，逻辑不分叉。
    -->
    <a-drawer
      v-model:open="drawerOpen"
      placement="left"
      :width="280"
      :closable="false"
      :body-style="{ padding: 0, background: 'var(--bg-surface)' }"
      class="mobile-menu-drawer"
    >
      <div class="brand brand--drawer">
        <span class="brand__dot" />
        <span class="brand__name">MediaLab</span>
        <span class="brand__name-mono">v0.2</span>
      </div>
      <a-menu
        mode="inline"
        v-model:open-keys="openKeys"
        :selected-keys="selectedKeys"
        @click="onMenuClick"
      >
        <a-sub-menu v-for="group in menuGroups" :key="group.key">
          <template #title>
            <span class="menu-icon menu-icon--group">
              <component :is="group.icon" />
            </span>
            <span class="menu-group-title">{{ group.title }}</span>
          </template>
          <a-menu-item v-for="item in group.items" :key="item.key">
            <span class="menu-icon" :class="`menu-icon--${item.iconColor}`">
              <component :is="item.icon" />
            </span>
            <span class="menu-label">
              <span class="menu-label__name">{{ item.label }}</span>
              <span class="menu-label__sub">{{ item.sub }}</span>
            </span>
          </a-menu-item>
        </a-sub-menu>
      </a-menu>
    </a-drawer>
  </a-layout>
</template>

<style scoped>
/* 手机端汉堡按钮 */
.header-menu-btn {
  margin-right: var(--sp-1);
  flex: 0 0 auto;
  font-size: 16px;
}

/*
  手机端：侧栏**完全隐藏**（导航走抽屉）。

  不能靠 Ant 的 `collapsedWidth: 0`——那只是把宽度收成 0，侧栏自身
  `overflow: visible`，菜单内容仍会溢出到页面左上角。
*/
.layout-sider--mobile-hidden {
  display: none !important;
}

/* Ant 的 zeroWidthTrigger 浮标——与 header 汉堡重复，藏掉 */
:deep(.ant-layout-sider-zero-width-trigger) {
  display: none !important;
}

/* 抽屉：满高、内容可滚 */
:deep(.mobile-menu-drawer .ant-drawer-body) {
  padding: 0;
  overflow-y: auto;
}

/* 抽屉里的品牌条（无底部边框——抽屉自带边界） */
.brand--drawer {
  border-bottom: none;
}

.user-chip {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  cursor: pointer;
  padding: var(--sp-1) var(--sp-2);
  border-radius: var(--r-sm);
  transition: background var(--t-fast) var(--ease);
}
.user-chip:hover {
  background: var(--bg-inset);
}

.user-chip__name {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  font-weight: 500;
}

.menu-label {
  display: flex;
  flex-direction: column;
  line-height: 1.2;
  justify-content: center;
}

.menu-label__name {
  font-size: var(--text-sm);
  font-weight: 500;
}

.menu-label__sub {
  font-size: var(--text-2xs);
  color: var(--text-tertiary);
}

.ant-menu-item-selected .menu-label__sub {
  color: var(--accent-red);
  opacity: 0.7;
}

/* 一级菜单组标题 */
.menu-group-title {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--text-primary);
  letter-spacing: 0.02em;
}
</style>
