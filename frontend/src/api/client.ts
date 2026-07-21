import axios from 'axios'
import type { AxiosError, InternalAxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'

import { useAuthStore } from '@/stores/auth'
import { router } from '@/app/router'
import type { Problem } from './types'

/**
 * 全局 axios 实例。
 * - baseURL 固定 /api/v1；withCredentials 让浏览器自动携带 HttpOnly 会话 cookie。
 * - 非安全方法自动注入 X-CSRF-Token（值取自 auth store，源自 GET /auth/session）。
 * - 响应拦截统一处理 401（跳登录）与 429（限速提示），其余错误把 problem 附到 error 抛给调用方。
 */
export const client = axios.create({
  baseURL: '/api/v1',
  withCredentials: true,
  headers: {
    Accept: 'application/json',
  },
})

/** 带上 problem 的错误类型，调用方可读取 error.problem 拿到结构化错误。 */
export interface ApiError extends AxiosError<Problem> {
  problem?: Problem
}

const SAFE_METHODS = new Set(['get', 'head', 'options'])

/** 会话/引导探测端点：其 401 属正常（未登录），交给守卫处理，拦截器不做跳转。 */
function isAuthProbe(url: string | undefined): boolean {
  if (!url) {
    return false
  }
  // 精确匹配 /auth/session，避免误伤 /auth/sessions/{id}（会话管理端点）。
  const path = url.split('?')[0] ?? ''
  return path.endsWith('/auth/session') || path.includes('/bootstrap/')
}

function readCsrfToken(): string | null {
  // 延迟到请求时读取，避免与 store → client 的模块循环依赖在加载期取值。
  try {
    return useAuthStore().csrfToken
  } catch {
    return null
  }
}

client.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const method = (config.method ?? 'get').toLowerCase()
  if (!SAFE_METHODS.has(method)) {
    const token = readCsrfToken()
    if (token) {
      config.headers.set('X-CSRF-Token', token)
    }
  }
  return config
})

client.interceptors.response.use(
  (response) => response,
  (error: ApiError) => {
    const response = error.response
    if (response) {
      const problem = response.data
      if (response.status === 401) {
        if (!isAuthProbe(error.config?.url)) {
          // 会话已失效：先清掉陈旧的 auth store，否则守卫会用过期的 active 状态把 /login 弹回，
          // 造成会话过期后永远回不到登录页（401→push→弹回 的死循环）。
          try {
            useAuthStore().reset()
          } catch {
            // pinia 尚未就绪（极早期请求）时忽略，守卫仍会按未登录处理。
          }
          if (router.currentRoute.value.meta.requiresAuth === true) {
            void router.push({ name: 'login' })
          }
        }
      } else if (response.status === 429) {
        const headerRetry = Number(response.headers['retry-after'])
        const retryAfter =
          problem?.retry_after ?? (Number.isFinite(headerRetry) ? headerRetry : undefined)
        ElMessage.error(
          retryAfter
            ? `尝试过于频繁，请 ${Math.ceil(retryAfter)} 秒后再试`
            : '尝试过于频繁，请稍后再试',
        )
      }
      error.problem = problem
    }
    return Promise.reject(error)
  },
)
