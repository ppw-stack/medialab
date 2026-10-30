import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from './stores/auth'

/**
 * 路由表。
 *
 * **两个业务域的页面在同一�� SPA 里**：
 *
 * - 辅种域 —— `/sites` `/downloaders` `/torrents` `/torrent-index`
 *   `/user-data` `/cross-seed`
 * - 媒体库域 —— `/drivers` `/library` `/migrations` `/uploads` `/health`
 *   `/dav` `/media-settings`
 *
 * ⚠️ **`/settings` 归辅种域**（调度作业 + 环境变量两个 tab，功能多、
 * 改动频繁）；媒体库域的「签名密钥」页用 **`/media-settings`**。
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
      { path: 'torrents', name: 'torrents', component: () => import('./pages/Torrents.vue') },
      { path: 'torrent-index', name: 'torrent-index', component: () => import('./pages/TorrentIndex.vue') },
      { path: 'user-data', name: 'user-data', component: () => import('./pages/UserData.vue') },
      { path: 'cross-seed', name: 'cross-seed', component: () => import('./pages/CrossSeed.vue') },
      { path: 'settings', name: 'settings', component: () => import('./pages/Settings.vue') },

      // -- 媒体库域 --
      { path: 'drivers', name: 'drivers', component: () => import('./pages/media/Drivers.vue') },
      { path: 'library', name: 'library', component: () => import('./pages/media/Library.vue') },
      { path: 'migrations', name: 'migrations', component: () => import('./pages/media/Migrations.vue') },
      { path: 'uploads', name: 'uploads', component: () => import('./pages/media/Uploads.vue') },
      { path: 'health', name: 'health', component: () => import('./pages/media/Health.vue') },
      { path: 'dav', name: 'dav', component: () => import('./pages/media/DavUsers.vue') },
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
