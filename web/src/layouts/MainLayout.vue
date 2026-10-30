<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import {
  DashboardOutlined,
  CloudServerOutlined,
  ApiOutlined,
  DownloadOutlined,
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
} from '@ant-design/icons-vue'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const collapsed = ref(false)

/**
 * 侧边栏菜单（**两个业务域**）。
 *
 * 分组渲染：辅种域在上、媒体库域在下——两个域的操作路径完全不同，
 * 混在一列里会让用户找不到东西。
 */
const menuGroups = [
  {
    key: 'seed',
    title: '辅种',
    items: [
      { key: '/', icon: DashboardOutlined, label: '总览', sub: '仪表盘概览', iconColor: 'red' },
      { key: '/sites', icon: CloudServerOutlined, label: '站点', sub: 'NexusPHP 适配', iconColor: 'violet' },
      { key: '/downloaders', icon: ApiOutlined, label: '下载器', sub: 'qB · Transmission', iconColor: 'green' },
      { key: '/torrents', icon: DownloadOutlined, label: '任务', sub: '辅种运行中', iconColor: 'blue' },
      { key: '/torrent-index', icon: DatabaseOutlined, label: '拉取索引', sub: '免费种源', iconColor: 'amber' },
      { key: '/user-data', icon: UserOutlined, label: '用户数据', sub: '9 站用量/HR/消息', iconColor: 'cyan' },
      { key: '/cross-seed', icon: SafetyOutlined, label: '辅种统计', sub: '按下载器分组展示', iconColor: 'gold' },
    ],
  },
  {
    key: 'media',
    title: '媒体库',
    items: [
      { key: '/drivers', icon: DatabaseFilled, label: '存储源', sub: '网盘账号', iconColor: 'violet' },
      { key: '/library', icon: FolderOpenOutlined, label: '媒体库', sub: 'STRM 条目', iconColor: 'blue' },
      { key: '/migrations', icon: SwapOutlined, label: '迁移', sub: '115 → 光鸭', iconColor: 'amber' },
      { key: '/uploads', icon: UploadOutlined, label: '上传', sub: '本地 → 光鸭', iconColor: 'green' },
      { key: '/health', icon: HeartOutlined, label: '健康监控', sub: '断链检查', iconColor: 'red' },
      { key: '/dav', icon: CloudUploadOutlined, label: 'WebDAV', sub: '账号与审计', iconColor: 'cyan' },
      { key: '/media-settings', icon: SettingOutlined, label: '签名密钥', sub: '中转签名轮换', iconColor: 'pink' },
    ],
  },
]

/** 全部菜单项（用于「当前页」标题查找）。 */
const allItems = computed(() => menuGroups.flatMap((g) => g.items))

const selectedKeys = computed(() => [route.path])
const activeMenu = computed(() => allItems.value.find((m) => m.key === route.path))

function onMenuClick({ key }: { key: string }) {
  router.push(key)
}

async function handleLogout() {
  await auth.logout()
  router.push('/login')
}

</script>

<template>
  <a-layout style="min-height: 100vh">
    <a-layout-sider v-model:collapsed="collapsed" collapsible :width="280">
      <div class="brand">
        <span class="brand__dot" />
        <template v-if="!collapsed">
          <span class="brand__name">MediaLab</span>
          <span class="brand__name-mono">v0.2</span>
        </template>
      </div>
      <a-menu
        mode="inline"
        :selected-keys="selectedKeys"
        @click="onMenuClick"
      >
        <a-menu-item-group
          v-for="group in menuGroups"
          :key="group.key"
          :title="collapsed ? undefined : group.title"
        >
          <a-menu-item v-for="item in group.items" :key="item.key">
            <span class="menu-icon" :class="`menu-icon--${item.iconColor}`">
              <component :is="item.icon" />
            </span>
            <span v-if="!collapsed" class="menu-label">
              <span class="menu-label__name">{{ item.label }}</span>
              <span class="menu-label__sub">{{ item.sub }}</span>
            </span>
          </a-menu-item>
        </a-menu-item-group>
      </a-menu>
    </a-layout-sider>
    <a-layout>
      <a-layout-header>
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
  </a-layout>
</template>

<style scoped>
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
</style>
