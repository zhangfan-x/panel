import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useAuthStore } from '@/stores/auth'
import type { SessionResponse } from '@/api/types'

// 在 API 边界打桩，验证 store 的会话状态流转（等价于 mock 掉底层 axios 调用）。
vi.mock('@/api/auth', () => ({
  getSession: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
}))
vi.mock('@/api/bootstrap', () => ({
  getBootstrapState: vi.fn(),
  createOwner: vi.fn(),
}))

import * as authApi from '@/api/auth'
import * as bootstrapApi from '@/api/bootstrap'

function sessionResponse(overrides: Partial<SessionResponse> = {}): SessionResponse {
  return {
    user: { id: 1, username: 'admin' },
    session_state: 'active',
    csrf_token: 'csrf-token',
    reauth_recent: false,
    ...overrides,
  }
}

describe('auth store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('fetchSession 成功后写入用户、会话状态与 CSRF token', async () => {
    vi.mocked(authApi.getSession).mockResolvedValue(
      sessionResponse({ session_state: 'active', csrf_token: 'abc' }),
    )
    const auth = useAuthStore()
    expect(auth.loaded).toBe(false)

    await auth.fetchSession()

    expect(auth.user).toEqual({ id: 1, username: 'admin' })
    expect(auth.sessionState).toBe('active')
    expect(auth.csrfToken).toBe('abc')
    expect(auth.isAuthenticated).toBe(true)
    expect(auth.loaded).toBe(true)
  })

  it('fetchSession 遇到 401 时清空会话且仍标记 loaded', async () => {
    vi.mocked(authApi.getSession).mockRejectedValue({ response: { status: 401 } })
    const auth = useAuthStore()

    await auth.fetchSession()

    expect(auth.user).toBeNull()
    expect(auth.sessionState).toBeNull()
    expect(auth.csrfToken).toBeNull()
    expect(auth.isAuthenticated).toBe(false)
    expect(auth.loaded).toBe(true)
  })

  it('登录成功后会话为 active 并写入 CSRF token', async () => {
    const auth = useAuthStore()

    vi.mocked(authApi.login).mockResolvedValue({ session_state: 'active' })
    vi.mocked(authApi.getSession).mockResolvedValue(
      sessionResponse({ session_state: 'active', csrf_token: 'csrf-1' }),
    )
    const afterLogin = await auth.login('admin', 'a-very-strong-password')
    expect(afterLogin).toBe('active')
    expect(auth.sessionState).toBe('active')
    expect(auth.csrfToken).toBe('csrf-1')
    expect(auth.isAuthenticated).toBe(true)
    expect(vi.mocked(authApi.login)).toHaveBeenCalledWith({
      username: 'admin',
      password: 'a-very-strong-password',
    })
  })

  it('创建 Owner 后会话为 active', async () => {
    const auth = useAuthStore()
    vi.mocked(bootstrapApi.createOwner).mockResolvedValue({ session_state: 'active' })
    vi.mocked(authApi.getSession).mockResolvedValue(
      sessionResponse({ session_state: 'active', csrf_token: 'csrf-owner' }),
    )

    const state = await auth.createOwner({
      token: 'boot-token',
      username: 'admin',
      password: 'a-very-strong-password',
    })

    expect(state).toBe('active')
    expect(auth.sessionState).toBe('active')
    expect(auth.csrfToken).toBe('csrf-owner')
    expect(vi.mocked(bootstrapApi.createOwner)).toHaveBeenCalledOnce()
  })

  it('logout 清空所有会话状态', async () => {
    const auth = useAuthStore()
    vi.mocked(authApi.getSession).mockResolvedValue(sessionResponse())
    await auth.fetchSession()
    expect(auth.isAuthenticated).toBe(true)

    vi.mocked(authApi.logout).mockResolvedValue()
    await auth.logout()

    expect(auth.user).toBeNull()
    expect(auth.sessionState).toBeNull()
    expect(auth.csrfToken).toBeNull()
    expect(auth.isAuthenticated).toBe(false)
    expect(auth.loaded).toBe(true)
  })
})
