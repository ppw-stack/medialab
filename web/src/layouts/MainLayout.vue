<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import {
  DashboardOutlined,
  CloudServerOutlined,
  DownloadOutlined,
  GiftOutlined,
  DeleteOutlined,
  SettingOutlined,
  LogoutOutlined,
  UserOutlined,
  ApiOutlined,
} from '@ant-design/icons-vue'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const collapsed = ref(false)

const menuItems = [
  { key: '/', icon: DashboardOutlined, label: '总览' },
  { key: '/sites', icon: CloudServerOutlined, label: '站点' },
  { key: '/downloaders', icon: ApiOutlined, label: '下载器' },
  { key: '/torrents', icon: DownloadOutlined, label: '任务' },
  { key: '/free-seed', icon: GiftOutlined, label: '免费种' },
  { key: '/cleanup', icon: DeleteOutlined, label: '清理' },
  { key: '/settings', icon: SettingOutlined, label: '设置' },
]

const selectedKeys = computed(() => [route.path])

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
    <a-layout-sider v-model:collapsed="collapsed" collapsible>
      <div
        :style="{
          height: '48px',
          margin: '12px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: '#1677ff',
          fontWeight: 'bold',
          fontSize: collapsed ? '16px' : '18px',
        }"
      >
        {{ collapsed ? 'SH' : 'SeedHelper' }}
      </div>
      <a-menu
        theme="dark"
        mode="inline"
        :selected-keys="selectedKeys"
        @click="onMenuClick"
      >
        <a-menu-item v-for="item in menuItems" :key="item.key">
          <component :is="item.icon" />
          <span>{{ item.label }}</span>
        </a-menu-item>
      </a-menu>
    </a-layout-sider>
    <a-layout>
      <a-layout-header
        :style="{
          display: 'flex',
          justifyContent: 'flex-end',
          alignItems: 'center',
          padding: '0 24px',
        }"
      >
        <a-dropdown :trigger="['click']">
          <span
            style="cursor: pointer; display: flex; align-items: center; gap: 8px"
          >
            <a-avatar><UserOutlined /></a-avatar>
            <a-typography-text style="color: white">{{ auth.username }}</a-typography-text>
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
      <a-layout-content
        :style="{ margin: '16px', padding: '24px', background: '#141414', borderRadius: '8px' }"
      >
        <RouterView />
      </a-layout-content>
    </a-layout>
  </a-layout>
</template>
