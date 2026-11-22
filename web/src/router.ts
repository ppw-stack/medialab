import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router';
import { useAuthStore } from './stores/auth';

/**
 * 路由表。
 *
 * **两个业务域 + 一个「系统」**：
 *
 * - 辅种域 —— `/sites` `/downloaders` `/torrent-index` `/cross-seed`
 *   （`/` 重定向到 `/sites`）
 * - 媒体库域 —— `/drivers` `/aggregate` `/library` `/migrations` `/scrape`
 *   `/uploads` `/health` `/dav-users` `/media-settings`
 * - 系统 —— `/scheduler`（定时任务）/ `/settings`（设置）
 *
 * ⚠️ **`/scheduler` 与 `/settings` 是平级两个菜单**（2026-11-22 拆分）：
 * 此前「调度作业」与「环境变量」是 `/settings` 页里的两个 tab，
 * 找调度得先进「设置」——与它作为日常高频页面的地位不符。现拆开：
 * 定时任务按业务域分「辅种」「媒体库」两栏，设置只留部署级
 * 环境变量配置。
 *
 * ⚠️ **媒体库域的「签名密钥」页用 `/media-settings`**，不要并进
 * `/settings`——它是域内配置，不是部署级设置。
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
    component: () => import('./pages/LoginPage.vue'),
  },
  {
    path: '/',
    component: () => import('./layouts/MainLayout.vue'),
    children: [
      // -- 辅种域 --
      // 无「总览」页：根路径直接落到列表页（站点是辅种域的第一个页面）。
      { path: '', redirect: '/sites' },
      { path: 'sites', name: 'sites', component: () => import('./pages/SitesPage.vue') },
      {
        path: 'downloaders',
        name: 'downloaders',
        component: () => import('./pages/DownloadersPage.vue'),
      },
      {
        path: 'torrent-index',
        name: 'torrent-index',
        component: () => import('./pages/TorrentIndex.vue'),
      },
      { path: 'cross-seed', name: 'cross-seed', component: () => import('./pages/CrossSeed.vue') },

      // -- 系统 --
      // 定时任务（辅种 / 媒体库两栏）与设置（环境变量）是**平级两个菜单**
      { path: 'scheduler', name: 'scheduler', component: () => import('./pages/SchedulerPage.vue') },
      { path: 'settings', name: 'settings', component: () => import('./pages/SettingsPage.vue') },

      // -- 媒体库域 --
      { path: 'drivers', name: 'drivers', component: () => import('./pages/media/DriversPage.vue') },
      {
        path: 'aggregate',
        name: 'aggregate',
        component: () => import('./pages/media/AggregatePage.vue'),
      },
      { path: 'library', name: 'library', component: () => import('./pages/media/LibraryPage.vue') },
      {
        path: 'migrations',
        name: 'migrations',
        component: () => import('./pages/media/MigrationsPage.vue'),
      },
      { path: 'scrape', name: 'scrape', component: () => import('./pages/media/ScrapePage.vue') },
      { path: 'uploads', name: 'uploads', component: () => import('./pages/media/UploadsPage.vue') },
      { path: 'health', name: 'health', component: () => import('./pages/media/HealthPage.vue') },
      // `/dav` 是 WebDAV 服务端路径（后端 mount_dav），面板页面必须避开
      { path: 'dav-users', name: 'dav', component: () => import('./pages/media/DavUsers.vue') },
      {
        path: 'media-settings',
        name: 'media-settings',
        component: () => import('./pages/media/MediaSettingsPage.vue'),
      },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

let authChecked = false;

router.beforeEach(async (to) => {
  const auth = useAuthStore();

  if (!authChecked) {
    await auth.refresh();
    authChecked = true;
  }

  if (!auth.authenticated && to.path !== '/login') {
    return '/login';
  }
  if (auth.authenticated && to.path === '/login') {
    return '/';
  }
  return true;
});

export default router;
