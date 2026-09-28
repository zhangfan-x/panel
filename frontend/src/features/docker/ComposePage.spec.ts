import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'

// 屏蔽全局 SSE 单例（jobs store 创建时会订阅），测试环境不真正连接。
vi.mock('@/composables/useEventStream', () => ({
  useEventStream: () => ({
    connected: { value: false },
    onMetrics: () => () => {},
    onJob: () => () => {},
  }),
  startEventStream: vi.fn(),
  stopEventStream: vi.fn(),
}))

vi.mock('@/api/compose', () => ({
  listComposeProjects: vi.fn(),
  importComposeProject: vi.fn(),
  getComposeProject: vi.fn(),
  getComposeFile: vi.fn(),
  putComposeFile: vi.fn(),
  runComposeAction: vi.fn(),
  deleteComposeProject: vi.fn(),
}))

import * as composeApi from '@/api/compose'
import type { ComposeProjectListResponse } from '@/api/compose'
import { useJobsStore } from '@/stores/jobs'
import ComposePage from './ComposePage.vue'

function listResponse(): ComposeProjectListResponse {
  return {
    compose_root: '/opt/compose',
    projects: [
      {
        name: 'alpha',
        path: '/opt/compose/alpha',
        imported: true,
        has_compose_file: true,
        compose_file: 'compose.yaml',
        created_at: '2026-07-22T10:00:00+00:00',
      },
      {
        name: 'beta',
        path: '/opt/compose/beta',
        imported: false,
        has_compose_file: false,
        compose_file: null,
        created_at: null,
      },
    ],
  }
}

function mountPage(): ReturnType<typeof mount> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return mount(ComposePage, {
    global: { plugins: [ElementPlus, pinia, [VueQueryPlugin, { queryClient }]] },
  })
}

describe('ComposePage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('渲染项目列表、来源标签与根目录', async () => {
    vi.mocked(composeApi.listComposeProjects).mockResolvedValue(listResponse())
    const wrapper = mountPage()
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('/opt/compose')
    expect(text).toContain('alpha')
    expect(text).toContain('beta')
    expect(text).toContain('已导入')
    expect(text).toContain('自动发现')
    expect(text).toContain('compose.yaml')
    expect(text).toContain('缺失')
  })

  it('点击「启动」提交任务并打开任务抽屉', async () => {
    vi.mocked(composeApi.listComposeProjects).mockResolvedValue(listResponse())
    vi.mocked(composeApi.runComposeAction).mockResolvedValue({ job_id: 'job-9' })
    const wrapper = mountPage()
    await flushPromises()

    const upButton = wrapper.findAll('button').find((button) => button.text() === '启动')
    if (!upButton) {
      throw new Error('未找到「启动」按钮')
    }
    await upButton.trigger('click')
    await flushPromises()

    expect(vi.mocked(composeApi.runComposeAction)).toHaveBeenCalledWith('alpha', 'up')
    const jobsStore = useJobsStore()
    expect(jobsStore.drawerOpen).toBe(true)
    expect(jobsStore.activeLogJobId).toBe('job-9')
  })

  it('自动发现行的「导入」预填对话框并提交导入请求', async () => {
    vi.mocked(composeApi.listComposeProjects).mockResolvedValue(listResponse())
    vi.mocked(composeApi.importComposeProject).mockResolvedValue({
      name: 'beta',
      path: '/opt/compose/beta',
      imported: true,
    })
    const wrapper = mountPage()
    await flushPromises()

    const rowImport = wrapper.findAll('button').find((button) => button.text() === '导入')
    if (!rowImport) {
      throw new Error('未找到行内「导入」按钮')
    }
    await rowImport.trigger('click')
    await flushPromises()

    // 对话框底部确认按钮与行内按钮同名，取文档流中最后一个。
    const submit = wrapper
      .findAll('button')
      .filter((button) => button.text() === '导入')
      .at(-1)
    if (!submit) {
      throw new Error('未找到导入对话框确认按钮')
    }
    await submit.trigger('click')
    await flushPromises()

    expect(vi.mocked(composeApi.importComposeProject)).toHaveBeenCalledWith({
      name: 'beta',
      path: '/opt/compose/beta',
    })
  })

  it('移除导入需原样输入项目名后才会发出删除请求', async () => {
    vi.mocked(composeApi.listComposeProjects).mockResolvedValue(listResponse())
    vi.mocked(composeApi.deleteComposeProject).mockResolvedValue(undefined)
    const wrapper = mountPage()
    await flushPromises()

    const removeButton = wrapper.findAll('button').find((button) => button.text() === '移除')
    if (!removeButton) {
      throw new Error('未找到「移除」按钮')
    }
    await removeButton.trigger('click')
    await flushPromises()

    // 未输入项目名前不应发出删除请求。
    expect(vi.mocked(composeApi.deleteComposeProject)).not.toHaveBeenCalled()

    const input = wrapper.find('input[placeholder="alpha"]')
    await input.setValue('alpha')
    const confirmButton = wrapper.findAll('button').find((button) => button.text() === '确认')
    if (!confirmButton) {
      throw new Error('未找到危险操作确认按钮')
    }
    await confirmButton.trigger('click')
    await flushPromises()

    expect(vi.mocked(composeApi.deleteComposeProject)).toHaveBeenCalledWith('alpha')
  })

  it('列表请求失败时展示错误告警', async () => {
    vi.mocked(composeApi.listComposeProjects).mockRejectedValue({
      problem: {
        type: 'about:blank',
        title: '后端暂不可用',
        status: 502,
        code: 'helper.unavailable',
      },
    })
    const wrapper = mountPage()
    await flushPromises()
    await flushPromises()

    expect(wrapper.text()).toContain('后端暂不可用')
  })
})
