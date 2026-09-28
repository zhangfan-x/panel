import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'

vi.mock('@/components/ReauthDialog.vue', () => ({
  default: {
    name: 'ReauthDialog',
    props: ['modelValue', 'title', 'message'],
    template: '<div class="reauth-stub" />',
  },
}))

vi.mock('@/api/docker', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/docker')>()
  return {
    ...actual,
    getDaemonConfig: vi.fn(),
    putDaemonConfig: vi.fn(),
    restartDaemon: vi.fn(),
    listContainers: vi.fn(),
  }
})

import * as dockerApi from '@/api/docker'
import type { DaemonConfig } from '@/api/docker'
import DaemonConfigPage from './DaemonConfigPage.vue'

const RAW = JSON.stringify(
  {
    'registry-mirrors': ['https://m.example.com'],
    proxies: { 'http-proxy': 'http://127.0.0.1:7890' },
    // 表单不认识的字段，必须在往返中原样保留
    'default-address-pools': [{ base: '172.24.0.0/13', size: 24 }],
    'exec-opts': ['native.cgroupdriver=systemd'],
  },
  null,
  2,
)

function configResponse(overrides: Partial<DaemonConfig> = {}): DaemonConfig {
  return {
    path: '/etc/docker/daemon.json',
    exists: true,
    content: RAW,
    parsed: JSON.parse(RAW),
    parse_error: '',
    mtime: 1753142400,
    restart_pending: false,
    ...overrides,
  }
}

function mountPage(): ReturnType<typeof mount> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return mount(DaemonConfigPage, {
    global: { plugins: [ElementPlus, pinia, [VueQueryPlugin, { queryClient }]] },
  })
}

describe('DaemonConfigPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = ''
    vi.mocked(dockerApi.getDaemonConfig).mockResolvedValue(configResponse())
    vi.mocked(dockerApi.listContainers).mockResolvedValue({ containers: [] })
  })

  it('把已有配置回填到表单', async () => {
    const wrapper = mountPage()
    await flushPromises()

    const inputs = wrapper.findAll('input')
    const values = inputs.map((i) => (i.element as HTMLInputElement).value)
    expect(values).toContain('http://127.0.0.1:7890')
    const textareas = wrapper.findAll('textarea')
    expect(textareas.some((t) => (t.element as HTMLTextAreaElement).value.includes('m.example.com')))
      .toBe(true)
  })

  it('改表单时保留表单未覆盖的字段', async () => {
    vi.mocked(dockerApi.putDaemonConfig).mockResolvedValue({
      ok: true,
      path: '/etc/docker/daemon.json',
      content: '{}',
      backup: 'daemon.json.bak-20260801000000',
    })
    const wrapper = mountPage()
    await flushPromises()

    // 改 HTTP 代理
    const proxyInput = wrapper
      .findAll('input')
      .find((i) => (i.element as HTMLInputElement).value === 'http://127.0.0.1:7890')
    await proxyInput?.setValue('http://127.0.0.1:1080')
    await flushPromises()

    const saveButton = wrapper.findAll('button').find((b) => b.text() === '保存')
    await saveButton?.trigger('click')
    await flushPromises()

    const sent = vi.mocked(dockerApi.putDaemonConfig).mock.calls[0]?.[0] as string
    const parsed = JSON.parse(sent)
    expect(parsed.proxies['http-proxy']).toBe('http://127.0.0.1:1080')
    // 冷门字段没被表单抹掉
    expect(parsed['exec-opts']).toEqual(['native.cgroupdriver=systemd'])
    expect(parsed['default-address-pools']).toEqual([{ base: '172.24.0.0/13', size: 24 }])
  })

  it('清空字段会把该键从 JSON 中删除而不是留空串', async () => {
    vi.mocked(dockerApi.putDaemonConfig).mockResolvedValue({
      ok: true,
      path: '/etc/docker/daemon.json',
      content: '{}',
      backup: '',
    })
    const wrapper = mountPage()
    await flushPromises()

    const proxyInput = wrapper
      .findAll('input')
      .find((i) => (i.element as HTMLInputElement).value === 'http://127.0.0.1:7890')
    await proxyInput?.setValue('')
    await flushPromises()

    const saveButton = wrapper.findAll('button').find((b) => b.text() === '保存')
    await saveButton?.trigger('click')
    await flushPromises()

    const parsed = JSON.parse(vi.mocked(dockerApi.putDaemonConfig).mock.calls[0]?.[0] as string)
    expect(parsed.proxies).toBeUndefined()
  })

  it('配置比守护进程新时提示需要重启', async () => {
    vi.mocked(dockerApi.getDaemonConfig).mockResolvedValue(
      configResponse({ restart_pending: true }),
    )
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('需要重启 Docker')
  })

  it('daemon.json 无法解析时保留原文并提示去原始 JSON 修复', async () => {
    vi.mocked(dockerApi.getDaemonConfig).mockResolvedValue(
      configResponse({ content: '{"oops": ', parsed: {}, parse_error: '第 1 行 …' }),
    )
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('无法解析')
  })

  it('重启确认框说明会影响多少个运行中的容器', async () => {
    vi.mocked(dockerApi.listContainers).mockResolvedValue({
      containers: [
        { id: 'a', name: 'c1', image: 'nginx', state: 'running', status: 'Up', ports: [], created: 0, labels: {} },
        { id: 'b', name: 'c2', image: 'redis', state: 'running', status: 'Up', ports: [], created: 0, labels: {} },
      ],
    })
    const wrapper = mountPage()
    await flushPromises()

    const restartButton = wrapper.findAll('button').find((b) => b.text() === '重启 Docker')
    await restartButton?.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('重启当前 2 个运行中的容器')
  })
})
