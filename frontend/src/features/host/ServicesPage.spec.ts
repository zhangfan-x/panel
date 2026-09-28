import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'

// 只 mock 网络函数，保留 formatJournalLine / serviceJournalStreamUrl 等纯函数的真实实现。
vi.mock('@/api/services', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/services')>()
  return {
    ...actual,
    listServices: vi.fn(),
    getService: vi.fn(),
    getServiceJournal: vi.fn(),
    runServiceAction: vi.fn(),
  }
})

import * as servicesApi from '@/api/services'
import type { ServiceDetail, ServiceJournalResponse, ServiceListResponse } from '@/api/services'
import ServicesPage from './ServicesPage.vue'

function listResponse(): ServiceListResponse {
  return {
    units: [
      {
        name: 'nginx.service',
        load: 'loaded',
        active: 'active',
        sub: 'running',
        description: 'Web 服务器',
        enabled: 'enabled',
      },
      {
        name: 'redis.service',
        load: 'loaded',
        active: 'failed',
        sub: 'failed',
        description: '缓存服务',
        enabled: 'disabled',
      },
      {
        name: 'backup.service',
        load: 'loaded',
        active: 'inactive',
        sub: 'dead',
        description: '备份任务',
        enabled: '',
      },
    ],
  }
}

function detailResponse(): ServiceDetail {
  return {
    name: 'nginx.service',
    active: 'active',
    sub: 'running',
    load: 'loaded',
    enabled: 'enabled',
    description: 'Web 服务器',
    main_pid: 1234,
    memory: 12582912,
    since: 'Tue 2026-07-21 08:00:00 CST',
    fragment_path: '/lib/systemd/system/nginx.service',
  }
}

function journalResponse(): ServiceJournalResponse {
  return {
    lines: [
      {
        ts: '2026-07-22T02:00:00+00:00',
        message: 'server started',
        priority: 6,
        unit: 'nginx.service',
      },
    ],
  }
}

function mountPage(): ReturnType<typeof mount> {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return mount(ServicesPage, {
    global: { plugins: [ElementPlus, [VueQueryPlugin, { queryClient }]] },
  })
}

/** 打开 nginx.service 的详情抽屉（点击第一行）。 */
async function openFirstRow(wrapper: ReturnType<typeof mount>): Promise<void> {
  vi.mocked(servicesApi.getService).mockResolvedValue(detailResponse())
  vi.mocked(servicesApi.getServiceJournal).mockResolvedValue(journalResponse())
  await wrapper.find('tbody tr').trigger('click')
  await flushPromises()
}

describe('ServicesPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = ''
  })

  it('渲染服务列表、状态标签与自启状态', async () => {
    vi.mocked(servicesApi.listServices).mockResolvedValue(listResponse())
    const wrapper = mountPage()
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('nginx.service')
    expect(text).toContain('active (running)')
    expect(text).toContain('failed (failed)')
    expect(text).toContain('enabled')
    expect(text).toContain('disabled')
    expect(text).toContain('共 3 / 3 个单元')
  })

  it('按运行状态过滤单元', async () => {
    vi.mocked(servicesApi.listServices).mockResolvedValue(listResponse())
    const wrapper = mountPage()
    await flushPromises()

    const failedRadio = wrapper
      .findAll('.el-radio-button')
      .find((radio) => radio.text() === 'failed')
    if (!failedRadio) {
      throw new Error('未找到 failed 状态过滤按钮')
    }
    await failedRadio.find('input').setValue()
    await flushPromises()

    expect(wrapper.text()).toContain('redis.service')
    expect(wrapper.text()).not.toContain('nginx.service')
    expect(wrapper.text()).toContain('共 1 / 3 个单元')
  })

  it('点击行打开详情抽屉并加载日志快照', async () => {
    vi.mocked(servicesApi.listServices).mockResolvedValue(listResponse())
    const wrapper = mountPage()
    await flushPromises()

    await openFirstRow(wrapper)

    expect(vi.mocked(servicesApi.getService)).toHaveBeenCalledWith('nginx.service')
    expect(vi.mocked(servicesApi.getServiceJournal)).toHaveBeenCalledWith('nginx.service', 200)
    const text = wrapper.text()
    expect(text).toContain('服务详情：nginx.service')
    expect(text).toContain('/lib/systemd/system/nginx.service')
    expect(text).toContain('12.0 MiB')
    expect(text).toContain('server started')
    expect(text).toContain('[INFO]')
  })

  it('「启动」无需确认，直接调用生命周期接口', async () => {
    vi.mocked(servicesApi.listServices).mockResolvedValue(listResponse())
    vi.mocked(servicesApi.runServiceAction).mockResolvedValue(undefined)
    const wrapper = mountPage()
    await flushPromises()
    await openFirstRow(wrapper)

    const startButton = wrapper.findAll('button').find((button) => button.text() === '启动')
    if (!startButton) {
      throw new Error('未找到「启动」按钮')
    }
    await startButton.trigger('click')
    await flushPromises()

    expect(vi.mocked(servicesApi.runServiceAction)).toHaveBeenCalledWith('nginx.service', 'start')
  })

  it('「停止」先弹确认框，确认后才调用接口', async () => {
    vi.mocked(servicesApi.listServices).mockResolvedValue(listResponse())
    vi.mocked(servicesApi.runServiceAction).mockResolvedValue(undefined)
    const wrapper = mountPage()
    await flushPromises()
    await openFirstRow(wrapper)

    const stopButton = wrapper.findAll('button').find((button) => button.text() === '停止')
    if (!stopButton) {
      throw new Error('未找到「停止」按钮')
    }
    await stopButton.trigger('click')
    await flushPromises()

    // 确认框未确认前不应调用接口。
    expect(vi.mocked(servicesApi.runServiceAction)).not.toHaveBeenCalled()
    expect(document.body.textContent).toContain('确定要停止服务 nginx.service 吗')

    const confirmButton = document.querySelector<HTMLButtonElement>(
      '.el-message-box__btns .el-button--primary',
    )
    if (!confirmButton) {
      throw new Error('未找到确认按钮')
    }
    confirmButton.click()
    await flushPromises()

    expect(vi.mocked(servicesApi.runServiceAction)).toHaveBeenCalledWith('nginx.service', 'stop')
  })
})
