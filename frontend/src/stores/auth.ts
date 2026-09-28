import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import * as authApi from '@/api/auth'
import * as bootstrapApi from '@/api/bootstrap'
import type {
  BootstrapOwnerRequest,
  BootstrapStateResponse,
  SessionState,
  SessionUser,
} from '@/api/types'

/**
 * 认证会话状态。作为路由守卫与拦截器的单一事实来源。
 *
 * 约定：所有会导致会话状态变化的动作（login / createOwner）在主调用后再拉一次
 * fetchSession()，把 user / csrfToken / sessionState 与服务端对齐，
 * 从而保证后续非 GET 请求能带上正确的 CSRF token。
 */
export const useAuthStore = defineStore('auth', () => {
  const user = ref<SessionUser | null>(null)
  const sessionState = ref<SessionState | null>(null)
  const csrfToken = ref<string | null>(null)
  const reauthRecent = ref(false)
  /** 是否已完成首次会话探测（守卫据此决定是否需要先 fetchSession）。 */
  const loaded = ref(false)

  const isAuthenticated = computed(() => sessionState.value === 'active')

  function reset(): void {
    user.value = null
    sessionState.value = null
    csrfToken.value = null
    reauthRecent.value = false
  }

  /** 拉取会话；401 视为未登录（清空并标记 loaded），其他错误同样降级为未登录，避免卡住守卫。 */
  async function fetchSession(): Promise<void> {
    try {
      const data = await authApi.getSession()
      user.value = data.user
      sessionState.value = data.session_state
      csrfToken.value = data.csrf_token
      reauthRecent.value = data.reauth_recent
    } catch {
      reset()
    } finally {
      loaded.value = true
    }
  }

  async function fetchBootstrapState(): Promise<BootstrapStateResponse> {
    return bootstrapApi.getBootstrapState()
  }

  async function login(username: string, password: string): Promise<SessionState> {
    const res = await authApi.login({ username, password })
    sessionState.value = res.session_state
    await fetchSession()
    return sessionState.value ?? res.session_state
  }

  async function createOwner(body: BootstrapOwnerRequest): Promise<SessionState> {
    const res = await bootstrapApi.createOwner(body)
    sessionState.value = res.session_state
    await fetchSession()
    return sessionState.value ?? res.session_state
  }

  async function logout(): Promise<void> {
    try {
      await authApi.logout()
    } finally {
      reset()
      loaded.value = true
    }
  }

  return {
    user,
    sessionState,
    csrfToken,
    reauthRecent,
    loaded,
    isAuthenticated,
    reset,
    fetchSession,
    fetchBootstrapState,
    login,
    createOwner,
    logout,
  }
})
