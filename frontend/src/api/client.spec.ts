import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('@/api/auth', () => ({
  getSession: vi.fn(),
  login: vi.fn(),
  verifyTotp: vi.fn(),
  setupTotp: vi.fn(),
  confirmTotp: vi.fn(),
  logout: vi.fn(),
}))
vi.mock('@/api/bootstrap', () => ({
  getBootstrapState: vi.fn(),
  createOwner: vi.fn(),
}))
// mock router 单例：拦截器只读 currentRoute.meta.requiresAuth 并调用 push。
// 用 spy 断言跳转，避免真实 router 在多用例间累积导航状态、异步守卫时序污染断言。
vi.mock('@/app/router', () => ({
  router: {
    currentRoute: { value: { name: 'dashboard', meta: { requiresAuth: true } } },
    push: vi.fn(),
  },
}))

import * as authApi from '@/api/auth'
import { useAuthStore } from '@/stores/auth'
import { router } from '@/app/router'
import { client } from '@/api/client'
import type { SessionResponse } from '@/api/types'

/** axios 内部拦截器条目形状（仅取 rejected 回调用于测试）。 */
interface RejectedEntry {
  rejected?: (error: unknown) => unknown
}

/** 取出响应拦截器的 rejected 回调，以构造的错误直接驱动它（无需真实网络层）。 */
function responseRejectedHandler(): (error: unknown) => unknown {
  const stack = (client.interceptors.response as unknown as { handlers: RejectedEntry[] }).handlers
  const entry = stack.find((h) => h.rejected)
  if (!entry?.rejected) {
    throw new Error('no rejected response interceptor registered')
  }
  return entry.rejected
}

function activeSession(): SessionResponse {
  return {
    user: { id: 1, username: 'admin' },
    session_state: 'active',
    csrf_token: 'csrf',
    reauth_recent: false,
  }
}

function error401(url: string): unknown {
  return { config: { url }, response: { status: 401, data: {}, headers: {} } }
}

describe('api client 401 拦截器', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    vi.mocked(authApi.getSession).mockResolvedValue(activeSession())
    // 建立已登录（active）会话，作为“会话在使用中途失效”的前提。
    await useAuthStore().fetchSession()
  })

  it('非探测端点的 401 清空 store 并跳转登录页', async () => {
    const auth = useAuthStore()
    expect(auth.isAuthenticated).toBe(true)

    const err = error401('/system/info')
    await expect(responseRejectedHandler()(err)).rejects.toBe(err)

    // 修复前：store 保持陈旧 active，守卫会把 /login 弹回 dashboard（死循环）。
    expect(auth.sessionState).toBeNull()
    expect(vi.mocked(router.push)).toHaveBeenCalledWith({ name: 'login' })
  })

  it('/auth/sessions/{id} 的 401 不被误当作会话探测（精确匹配 /auth/session）', async () => {
    const auth = useAuthStore()

    const err = error401('/auth/sessions/5')
    await expect(responseRejectedHandler()(err)).rejects.toBe(err)

    expect(auth.sessionState).toBeNull()
    expect(vi.mocked(router.push)).toHaveBeenCalledWith({ name: 'login' })
  })

  it('会话探测端点 /auth/session 的 401 不清 store、不跳转（交给守卫）', async () => {
    const auth = useAuthStore()

    const err = error401('/auth/session')
    await expect(responseRejectedHandler()(err)).rejects.toBe(err)

    expect(auth.sessionState).toBe('active')
    expect(vi.mocked(router.push)).not.toHaveBeenCalled()
  })

  it('引导探测端点 /bootstrap/state 的 401 不清 store、不跳转', async () => {
    const auth = useAuthStore()

    const err = error401('/bootstrap/state')
    await expect(responseRejectedHandler()(err)).rejects.toBe(err)

    expect(auth.sessionState).toBe('active')
    expect(vi.mocked(router.push)).not.toHaveBeenCalled()
  })
})
