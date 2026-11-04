import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from './stores/auth'

/**
 * 路由表。
 *
 * **两个业务域的页面在同一�� SPA 里**：
 *
 * - 辅种域 —— `/sites` `/downloaders` `/torrent-index`
 *   `/cross-seed` `/settings`
 * - 媒体库域 —— `/drivers` `/library` `/migrations` `/uploads` `/health`
 *   `/dav-users` `/media-settings`
 *   + `/offline`（离线下载；功能未启用时页面会说明怎么开）
 *
 * ⚠️ **`/settings` 归辅种域**（调度作业 + 环境变量两个 tab，功能多、
 * 改动频繁）；媒体库域的「签名密钥」页用 **`/media-settings`**。
 *
 * ⚠️ **面板的 WebDAV 账号页用 `/dav-users`，不能用 `/dav`**——
 * 后端把 `/dav` 挂给了 WebDAV **服务端**（`dav_mount_path`，且必须在
 * SPA 兜底之前挂载，见 `app/main.py` 的 `mount_dav`）。路径相同 →
 * WebDAV 先匹配，面板页面永远访问不到（浏览器打开 `/dav` 拿到的是
 * WebDAV 的 401，不是面板）。
 */
const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('./pages/Login.vue'),
  },
  {
    path: '/',
    component: () => import('./layouts/MainLayout.vue'),
    children: [
      // -- 辅种域 --
      { path: '', name: 'dashboard', component: () => import('./pages/Dashboard.vue') },
      { path: 'sites', name: 'sites', component: () => import('./pages/Sites.vue') },
      { path: 'downloaders', name: 'downloaders', component: () => import('./pages/Downloaders.vue') },
      { path: 'torrent-index', name: 'torrent-index', component: () => import('./pages/TorrentIndex.vue') },
      { path: 'cross-seed', name: 'cross-seed', component: () => import('./pages/CrossSeed.vue') },
      { path: 'settings', name: 'settings', component: () => import('./pages/Settings.vue') },

      // -- 媒体库域 --
      { path: 'drivers', name: 'drivers', component: () => import('./pages/media/Drivers.vue') },
      { path: 'library', name: 'library', component: () => import('./pages/media/Library.vue') },
      { path: 'migrations', name: 'migrations', component: () => import('./pages/media/Migrations.vue') },
      { path: 'offline', name: 'offline', component: () => import('./pages/media/Offline.vue') },
      { path: 'uploads', name: 'uploads', component: () => import('./pages/media/Uploads.vue') },
      { path: 'health', name: 'health', component: () => import('./pages/media/Health.vue') },
      // `/dav` 是 WebDAV 服务端路径（后端 mount_dav），面板页面必须避开
      { path: 'dav-users', name: 'dav', component: () => import('./pages/media/DavUsers.vue') },
      { path: 'media-settings', name: 'media-settings', component: () => import('./pages/media/Settings.vue') },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

let authChecked = false

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  if (!authChecked) {
    await auth.refresh()
    authChecked = true
  }

  if (!auth.authenticated && to.path !== '/login') {
    return '/login'
  }
  if (auth.authenticated && to.path === '/login') {
    return '/'
  }
  return true
})

export default router
