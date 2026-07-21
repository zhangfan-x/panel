import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import AppLayout from '@/layouts/AppLayout.vue'
import PlaceholderPage from '@/components/PlaceholderPage.vue'

declare module 'vue-router' {
  interface RouteMeta {
    /** 公开路由（无需登录）。 */
    public?: boolean
    /** 仅未登录可访问；已登录访问时重定向到仪表盘。 */
    guestOnly?: boolean
    /** 受保护路由，需要 active 会话（守卫会处理 totp_enroll/mfa_pending 的中间态）。 */
    requiresAuth?: boolean
    /** 侧边菜单/占位页显示的标题。 */
    title?: string
  }
}

/** 生成一个占位路由记录（统一使用 PlaceholderPage）。 */
function placeholder(path: string, name: string, title: string): RouteRecordRaw {
  return {
    path,
    name,
    component: PlaceholderPage,
    meta: { requiresAuth: true, title },
  }
}

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/dashboard' },
  {
    path: '/setup',
    name: 'setup',
    component: () => import('@/features/auth/SetupPage.vue'),
    meta: { public: true, guestOnly: true, title: '初始化' },
  },
  {
    path: '/login',
    name: 'login',
    component: () => import('@/features/auth/LoginPage.vue'),
    meta: { public: true, guestOnly: true, title: '登录' },
  },
  {
    path: '/',
    component: AppLayout,
    children: [
      {
        path: 'security/totp',
        name: 'totp-enroll',
        component: () => import('@/features/auth/TotpEnrollPage.vue'),
        meta: { requiresAuth: true, title: '绑定两步验证' },
      },
      {
        path: 'dashboard',
        name: 'dashboard',
        component: () => import('@/features/dashboard/DashboardPage.vue'),
        meta: { requiresAuth: true, title: '仪表盘' },
      },
      placeholder('host/processes', 'host-processes', '进程管理'),
      placeholder('host/services', 'host-services', '服务管理'),
      placeholder('files', 'files', '文件管理'),
      placeholder('docker/containers', 'docker-containers', '容器'),
      placeholder('docker/images', 'docker-images', '镜像'),
      placeholder('docker/networks', 'docker-networks', '网络'),
      placeholder('docker/volumes', 'docker-volumes', '卷'),
      placeholder('docker/compose', 'docker-compose', 'Compose 项目'),
      placeholder('tasks', 'tasks', '任务中心'),
      placeholder('terminal', 'terminal', 'root 终端'),
      placeholder('audit', 'audit', '审计日志'),
      placeholder('settings', 'settings', '设置'),
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

/**
 * 全局守卫。
 * 1) 首次进入先确保会话已探测（fetchSession）。
 * 2) totp_enroll 会话强制停留在绑定页；mfa_pending 会话回登录页完成第二步。
 * 3) 已登录访问 /login、/setup → 跳仪表盘。
 * 4) 受保护路由未登录 → 由 /bootstrap/state 决定去 /setup 还是 /login。
 */
router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.loaded) {
    await auth.fetchSession()
  }

  const state = auth.sessionState
  const isActive = state === 'active'

  if (state === 'totp_enroll') {
    return to.name === 'totp-enroll' ? true : { name: 'totp-enroll' }
  }

  if (state === 'mfa_pending') {
    return to.name === 'login' ? true : { name: 'login' }
  }

  if (isActive && to.meta.guestOnly) {
    return { name: 'dashboard' }
  }

  if (to.meta.requiresAuth && !isActive) {
    const { needs_setup } = await auth.fetchBootstrapState()
    return needs_setup ? { name: 'setup' } : { name: 'login' }
  }

  return true
})
