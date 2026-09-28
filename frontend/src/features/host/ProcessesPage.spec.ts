import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'

vi.mock('@/api/processes', () => ({
  listProcesses: vi.fn(),
  getProcess: vi.fn(),
  signalProcess: vi.fn(),
  reniceProcess: vi.fn(),
}))

import * as processesApi from '@/api/processes'
import type { ProcessDetail, ProcessListResponse } from '@/api/processes'
import { useAuthStore } from '@/stores/auth'
import ProcessesPage from './ProcessesPage.vue'

function listResponse(): ProcessListResponse {
  return {
    total: 2,
    processes: [
      {
        pid: 1234,
        ppid: 1,
        user: 'www-data',
        cpu: 12.5,
        mem: 3.2,
        status: 'running',
        started: '2026-07-22T08:00:00+00:00',
        name: 'nginx',
        cmdline: 'nginx: master process /usr/sbin/nginx',
      },
      {
        pid: 4321,
        ppid: 1234,
        user: 'root',
        cpu: 0.5,
        mem: 1.1,
        status: 'sleeping',
        started: '2026-07-22T09:30:00+00:00',
        name: 'bash',
        cmdline: '/bin/bash',
      },
    ],
  }
}

function detailResponse(): ProcessDetail {
  return {
    pid: 1234,
    ppid: 1,
    user: 'www-data',
    cpu: 12.5,
    mem: 3.2,
    status: 'running',
    started: '2026-07-22T08:00:00+00:00',
    name: 'nginx',
    cmdline: 'nginx: master process /usr/sbin/nginx',
    nice: 0,
    num_threads: 4,
    rss: 10485760,
    vms: 209715200,
    exe: '/usr/sbin/nginx',
    cwd: '/var/www',
    children: [4321],
  }
}

function mountPage(): ReturnType<typeof mount> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return mount(ProcessesPage, {
    global: { plugins: [ElementPlus, pinia, [VueQueryPlugin, { queryClient }]] },
  })
}

function findButton(wrapper: VueWrapper, text: string) {
  const btn = wrapper.findAll('button').find((b) => b.text() === text)
  if (!btn) {
    throw new Error(`未找到按钮：${text}`)
  }
  return btn
}

describe('ProcessesPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('按默认参数请求列表并渲染进程行与状态标签', async () => {
    vi.mocked(processesApi.listProcesses).mockResolvedValue(listResponse())
    const wrapper = mountPage()
    await flushPromises()

    expect(vi.mocked(processesApi.listProcesses)).toHaveBeenCalledWith({
      search: '',
      sort: 'cpu',
      order: 'desc',
      limit: 50,
      offset: 0,
    })
    const text = wrapper.text()
    expect(text).toContain('nginx')
    expect(text).toContain('bash')
    expect(text).toContain('www-data')
    expect(text).toContain('运行')
    expect(text).toContain('睡眠')
  })

  it('列表请求失败时展示错误告警', async () => {
    vi.mocked(processesApi.listProcesses).mockRejectedValue({
      problem: { type: 'about:blank', title: '进程列表不可用', status: 500, code: 'internal' },
    })
    const wrapper = mountPage()
    await flushPromises()
    await flushPromises()

    expect(wrapper.text()).toContain('进程列表不可用')
  })

  it('点击「详情」拉取进程详情并在抽屉中展示', async () => {
    vi.mocked(processesApi.listProcesses).mockResolvedValue(listResponse())
    vi.mocked(processesApi.getProcess).mockResolvedValue(detailResponse())
    const wrapper = mountPage()
    await flushPromises()

    await findButton(wrapper, '详情').trigger('click')
    await flushPromises()

    expect(vi.mocked(processesApi.getProcess)).toHaveBeenCalledWith(1234)
    const text = wrapper.text()
    expect(text).toContain('/usr/sbin/nginx')
    expect(text).toContain('/var/www')
    expect(text).toContain('工作目录')
  })

  it('重认证窗口已开启时：危险确认通过后直接发送 SIGTERM', async () => {
    vi.mocked(processesApi.listProcesses).mockResolvedValue(listResponse())
    vi.mocked(processesApi.signalProcess).mockResolvedValue({ ok: true })
    const wrapper = mountPage()
    await flushPromises()
    useAuthStore().reauthRecent = true

    await findButton(wrapper, '终止').trigger('click')
    await flushPromises()
    await wrapper.find('input[placeholder="nginx"]').setValue('nginx')
    await findButton(wrapper, '确认').trigger('click')
    await flushPromises()

    expect(vi.mocked(processesApi.signalProcess)).toHaveBeenCalledWith(1234, 'SIGTERM')
  })

  it('重认证窗口未开启时：危险确认通过后先弹出身份验证且不发请求', async () => {
    vi.mocked(processesApi.listProcesses).mockResolvedValue(listResponse())
    const wrapper = mountPage()
    await flushPromises()

    await findButton(wrapper, '终止').trigger('click')
    await flushPromises()
    await wrapper.find('input[placeholder="nginx"]').setValue('nginx')
    await findButton(wrapper, '确认').trigger('click')
    await flushPromises()

    expect(vi.mocked(processesApi.signalProcess)).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('验证身份')
  })
})
