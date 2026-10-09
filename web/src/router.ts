import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from './stores/auth'

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
      { path: '', name: 'dashboard', component: () => import('./pages/Dashboard.vue') },
      { path: 'sites', name: 'sites', component: () => import('./pages/Sites.vue') },
      { path: 'torrents', name: 'torrents', component: () => import('./pages/Torrents.vue') },
      { path: 'free-seed', name: 'free-seed', component: () => import('./pages/FreeSeed.vue') },
      { path: 'alerts', name: 'alerts', component: () => import('./pages/Alerts.vue') },
      { path: 'cleanup', name: 'cleanup', component: () => import('./pages/Cleanup.vue') },
      { path: 'audit', name: 'audit', component: () => import('./pages/AuditLog.vue') },
      { path: 'settings', name: 'settings', component: () => import('./pages/Settings.vue') },
      { path: 'downloaders', name: 'downloaders', component: () => import('./pages/Downloaders.vue') },
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
