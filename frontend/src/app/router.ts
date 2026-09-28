import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import AppLayout from '@/layouts/AppLayout.vue'

declare module 'vue-router' {
  interface RouteMeta {
    /** 公开路由（无需登录）。 */
    public?: boolean
    /** 仅未登录可访问；已登录访问时重定向到仪表盘。 */
    guestOnly?: boolean
    /** 受保护路由，需要 active 会话。 */
    requiresAuth?: boolean
    /** 侧边菜单/占位页显示的标题。 */
    title?: string
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
        path: 'dashboard',
        name: 'dashboard',
        component: () => import('@/features/dashboard/DashboardPage.vue'),
        meta: { requiresAuth: true, title: '仪表盘' },
      },
      {
        path: 'host/processes',
        name: 'host-processes',
        component: () => import('@/features/host/ProcessesPage.vue'),
        meta: { requiresAuth: true, title: '进程管理' },
      },
      {
        path: 'host/services',
        name: 'host-services',
        component: () => import('@/features/host/ServicesPage.vue'),
        meta: { requiresAuth: true, title: '服务管理' },
      },
      {
        path: 'files',
        name: 'files',
        component: () => import('@/features/files/FilesPage.vue'),
        meta: { requiresAuth: true, title: '文件管理' },
      },
      {
        path: 'docker/containers',
        name: 'docker-containers',
        component: () => import('@/features/docker/ContainersPage.vue'),
        meta: { requiresAuth: true, title: '容器' },
      },
      {
        path: 'docker/images',
        name: 'docker-images',
        component: () => import('@/features/docker/ImagesPage.vue'),
        meta: { requiresAuth: true, title: '镜像' },
      },
      {
        path: 'docker/networks',
        name: 'docker-networks',
        component: () => import('@/features/docker/NetworksPage.vue'),
        meta: { requiresAuth: true, title: '网络' },
      },
      {
        path: 'docker/volumes',
        name: 'docker-volumes',
        component: () => import('@/features/docker/VolumesPage.vue'),
        meta: { requiresAuth: true, title: '卷' },
      },
      {
        path: 'docker/compose',
        name: 'docker-compose',
        component: () => import('@/features/docker/ComposePage.vue'),
        meta: { requiresAuth: true, title: 'Compose 项目' },
      },
      {
        path: 'docker/daemon',
        name: 'docker-daemon',
        component: () => import('@/features/docker/DaemonConfigPage.vue'),
        meta: { requiresAuth: true, title: 'Docker 配置' },
      },
      {
        path: 'website/sites',
        name: 'website-sites',
        component: () => import('@/features/website/SitesPage.vue'),
        meta: { requiresAuth: true, title: '站点' },
      },
      {
        path: 'website/certs',
        name: 'website-certs',
        component: () => import('@/features/website/CertsPage.vue'),
        meta: { requiresAuth: true, title: '证书' },
      },
      {
        path: 'website/firewall',
        name: 'website-firewall',
        component: () => import('@/features/website/FirewallPage.vue'),
        meta: { requiresAuth: true, title: '防火墙' },
      },
      {
        path: 'proxy',
        name: 'proxy',
        component: () => import('@/features/proxy/ProxyPage.vue'),
        meta: { requiresAuth: true, title: '代理管理' },
      },
      {
        path: 'tasks',
        name: 'tasks',
        component: () => import('@/features/tasks/TasksPage.vue'),
        meta: { requiresAuth: true, title: '任务中心' },
      },
      {
        path: 'terminal',
        name: 'terminal',
        component: () => import('@/features/terminal/TerminalPage.vue'),
        meta: { requiresAuth: true, title: 'root 终端' },
      },
      {
        path: 'audit',
        name: 'audit',
        component: () => import('@/features/audit/AuditPage.vue'),
        meta: { requiresAuth: true, title: '审计日志' },
      },
      {
        path: 'settings',
        name: 'settings',
        component: () => import('@/features/settings/SettingsPage.vue'),
        meta: { requiresAuth: true, title: '设置' },
      },
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
 * 2) 已登录访问 /login、/setup → 跳仪表盘。
 * 3) 受保护路由未登录 → 由 /bootstrap/state 决定去 /setup 还是 /login。
 */
router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.loaded) {
    await auth.fetchSession()
  }

  const isActive = auth.sessionState === 'active'

  if (isActive && to.meta.guestOnly) {
    return { name: 'dashboard' }
  }

  if (to.meta.requiresAuth && !isActive) {
    const { needs_setup } = await auth.fetchBootstrapState()
    return needs_setup ? { name: 'setup' } : { name: 'login' }
  }

  return true
})
