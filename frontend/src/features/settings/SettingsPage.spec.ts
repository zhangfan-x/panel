import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import type { Pinia } from 'pinia'
import ElementPlus from 'element-plus'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'

vi.mock('@/api/client', () => ({
  client: { post: vi.fn(), get: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/api/settings', () => ({
  getSystemSettings: vi.fn(),
  getOwnerProfile: vi.fn(),
  listSessions: vi.fn(),
  revokeSession: vi.fn(),
  changePassword: vi.fn(),
}))

import * as settingsApi from '@/api/settings'
import type { OwnerProfile, SystemSettings, UserSession } from '@/api/settings'
import { useAuthStore } from '@/stores/auth'
import SettingsPage from '@/features/settings/SettingsPage.vue'

let pinia: Pinia

function ownerProfile(overrides: Partial<OwnerProfile> = {}): OwnerProfile {
  return {
    username: 'admin',
    created_at: '2026-07-01T08:00:00+00:00',
    password_changed_at: '2026-07-10T08:00:00+00:00',
    ...overrides,
  }
}

function userSessions(): UserSession[] {
  return [
    {
      id: 1,
      created_at: '2026-07-20T08:00:00+00:00',
      last_seen_at: '2026-07-22T08:00:00+00:00',
      ip: '127.0.0.1',
      user_agent: 'TestBrowser/1.0',
      current: true,
    },
    {
      id: 2,
      created_at: '2026-07-19T08:00:00+00:00',
      last_seen_at: '2026-07-21T08:00:00+00:00',
      ip: '10.0.0.2',
      user_agent: 'OtherBrowser/2.0',
      current: false,
    },
  ]
}

function systemSettings(): SystemSettings {
  return {
    server: {
      host: '0.0.0.0',
      port: 25443,
      dev_http: false,
      tls_cert: '/etc/ownpanel/tls/cert.pem',
      tls_key: '/etc/ownpanel/tls/key.pem',
    },
    limits: {
      session_idle_minutes: 30,
      session_max_hours: 12,
      reauth_window_seconds: 60,
      login_max_attempts: 5,
      upload_max_bytes: 2 * 1024 * 1024 * 1024,
      job_concurrency: 2,
      audit_retention_days: 90,
      metric_retention_days: 7,
      terminal_idle_minutes: 10,
      terminal_max_minutes: 30,
    },
    docker: { compose_root: '/opt/compose' },
    version: '0.2.0-test',
  }
}

async function mountPage(): Promise<VueWrapper> {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  })
  const wrapper = mount(SettingsPage, {
    global: { plugins: [ElementPlus, pinia, [VueQueryPlugin, { queryClient }]] },
  })
  await flushPromises()
  await flushPromises()
  return wrapper
}

function findButton(wrapper: VueWrapper, text: string) {
  const btn = wrapper.findAll('button').find((b) => b.text() === text)
  if (!btn) {
    throw new Error(`未找到按钮：${text}`)
  }
  return btn
}

async function fillPasswordForm(wrapper: VueWrapper): Promise<void> {
  await wrapper.find('input[placeholder="请输入当前密码"]').setValue('OldPassword123!')
  await wrapper
    .find('input[placeholder="请输入新密码（至少 12 个字符）"]')
    .setValue('NewPassword12345')
  await wrapper.find('input[placeholder="请再次输入新密码"]').setValue('NewPassword12345')
}

describe('SettingsPage', () => {
  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    vi.clearAllMocks()
    vi.mocked(settingsApi.getOwnerProfile).mockResolvedValue(ownerProfile())
    vi.mocked(settingsApi.listSessions).mockResolvedValue(userSessions())
    vi.mocked(settingsApi.getSystemSettings).mockResolvedValue(systemSettings())
    vi.mocked(settingsApi.changePassword).mockResolvedValue(undefined)
    vi.mocked(settingsApi.revokeSession).mockResolvedValue(undefined)
  })

  it('加载后展示个人资料、会话列表与系统信息', async () => {
    const wrapper = await mountPage()

    // 个人资料
    expect(wrapper.text()).toContain('admin')
    // 会话管理：当前会话有标记且不可下线，其他会话可下线
    expect(wrapper.text()).toContain('当前会话')
    expect(wrapper.text()).toContain('OtherBrowser/2.0')
    const revokeButtons = wrapper.findAll('button').filter((b) => b.text() === '下线')
    expect(revokeButtons).toHaveLength(1)
    // 系统信息
    expect(wrapper.text()).toContain('0.2.0-test')
    expect(wrapper.text()).toContain('/opt/compose')
  })

  it('未开启重认证窗口时，提交改密先弹出身份验证对话框', async () => {
    const wrapper = await mountPage()

    await fillPasswordForm(wrapper)
    await findButton(wrapper, '修改密码').trigger('click')
    await flushPromises()

    expect(settingsApi.changePassword).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('验证身份')
  })

  it('重认证窗口有效时直接提交修改密码', async () => {
    const auth = useAuthStore()
    auth.reauthRecent = true
    const wrapper = await mountPage()

    await fillPasswordForm(wrapper)
    await findButton(wrapper, '修改密码').trigger('click')
    await flushPromises()

    expect(settingsApi.changePassword).toHaveBeenCalledWith({
      current_password: 'OldPassword123!',
      new_password: 'NewPassword12345',
    })
  })
})
