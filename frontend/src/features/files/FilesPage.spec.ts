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

// 用桩组件替换编辑器：monaco-editor 无 main/exports 字段，vitest（mainFields=[]）
// 无法解析其包入口；生产构建（vite 6，走 module 字段）不受影响。
vi.mock('./FileEditor.vue', () => ({
  default: {
    name: 'FileEditor',
    props: ['modelValue', 'path'],
    template: '<div class="file-editor-stub" />',
  },
}))

// 只 mock 发请求的函数，纯工具函数（downloadUrl / guessArchiveFormat）保留真实实现。
vi.mock('@/api/files', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/files')>()
  return {
    ...actual,
    listDir: vi.fn(),
    statPath: vi.fn(),
    getContent: vi.fn(),
    putContent: vi.fn(),
    uploadFile: vi.fn(),
    mkdir: vi.fn(),
    renamePath: vi.fn(),
    copyPath: vi.fn(),
    chmodPath: vi.fn(),
    chownPath: vi.fn(),
    deletePath: vi.fn(),
    archivePaths: vi.fn(),
    extractArchive: vi.fn(),
  }
})

import * as filesApi from '@/api/files'
import type { FileListResponse } from '@/api/files'
import FilesPage from './FilesPage.vue'

function listResponse(): FileListResponse {
  return {
    path: '/',
    entries: [
      { name: 'etc', type: 'dir', size: 4096, mode: 0o755, uid: 0, gid: 0, mtime: 1753142400 },
      {
        name: 'hosts.txt',
        type: 'file',
        size: 2048,
        mode: 0o644,
        uid: 1000,
        gid: 1000,
        mtime: 1753142400,
      },
      {
        name: 'logo.png',
        type: 'file',
        size: 5 * 1024 * 1024, // 大于编辑器 2 MiB 上限：图片预览不受该限制
        mode: 0o644,
        uid: 1000,
        gid: 1000,
        mtime: 1753142400,
      },
    ],
  }
}

function mountPage(): ReturnType<typeof mount> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return mount(FilesPage, {
    global: { plugins: [ElementPlus, pinia, [VueQueryPlugin, { queryClient }]] },
  })
}

describe('FilesPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = '' // el-dialog teleport 到 body，避免用例间串扰
  })

  it('渲染目录条目：名称、类型、大小、权限与所有者', async () => {
    vi.mocked(filesApi.listDir).mockResolvedValue(listResponse())
    const wrapper = mountPage()
    await flushPromises()

    expect(vi.mocked(filesApi.listDir)).toHaveBeenCalledWith('/', false)
    const text = wrapper.text()
    expect(text).toContain('etc')
    expect(text).toContain('hosts.txt')
    expect(text).toContain('目录')
    expect(text).toContain('文件')
    expect(text).toContain('2.0 KiB')
    expect(text).toContain('755')
    expect(text).toContain('644 rw-r--r--')
    expect(text).toContain('1000:1000')
  })

  it('点击目录名进入子目录并重新请求列表', async () => {
    vi.mocked(filesApi.listDir).mockResolvedValue(listResponse())
    const wrapper = mountPage()
    await flushPromises()

    const dirLink = wrapper.findAll('a').find((link) => link.text() === 'etc')
    if (!dirLink) {
      throw new Error('未找到目录链接 etc')
    }
    await dirLink.trigger('click')
    await flushPromises()

    expect(vi.mocked(filesApi.listDir)).toHaveBeenLastCalledWith('/etc', false)
  })

  it('切换「显示隐藏文件」后带 show_hidden 重新请求', async () => {
    vi.mocked(filesApi.listDir).mockResolvedValue(listResponse())
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.find('.el-switch').trigger('click')
    await flushPromises()

    expect(vi.mocked(filesApi.listDir)).toHaveBeenLastCalledWith('/', true)
  })

  it('列表请求失败时展示错误告警', async () => {
    vi.mocked(filesApi.listDir).mockRejectedValue({
      problem: {
        type: 'about:blank',
        title: '路径不存在',
        status: 404,
        code: 'not_found',
      },
    })
    const wrapper = mountPage()
    await flushPromises()
    await flushPromises()

    expect(wrapper.text()).toContain('路径不存在')
  })

  it('点击图片文件打开内联预览而不是文本编辑器', async () => {
    vi.mocked(filesApi.listDir).mockResolvedValue(listResponse())
    const wrapper = mountPage()
    await flushPromises()

    const imageLink = wrapper.findAll('a').find((link) => link.text() === 'logo.png')
    if (!imageLink) {
      throw new Error('未找到图片链接 logo.png')
    }
    await imageLink.trigger('click')
    await flushPromises()

    // 未走 /files/content 的文本读取，而是对话框内的 inline 图片。
    expect(vi.mocked(filesApi.getContent)).not.toHaveBeenCalled()
    const img = wrapper.find('.el-image img')
    expect(img.exists()).toBe(true)
    expect(img.attributes('src')).toBe('/api/v1/files/download?path=%2Flogo.png&inline=1')
  })

  it('路径可编辑：输入绝对路径回车后跳转', async () => {
    vi.mocked(filesApi.listDir).mockResolvedValue(listResponse())
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.find('button[title="编辑路径"]').trigger('click')
    const input = wrapper.find('.files-page__path-input input')
    await input.setValue('/var/log//nginx/')
    await input.trigger('keyup.enter')
    await flushPromises()

    // 重复斜杠与尾斜杠被规范化。
    expect(vi.mocked(filesApi.listDir)).toHaveBeenLastCalledWith('/var/log/nginx', false)
  })

  it('目录行提供下载（zip）入口', async () => {
    vi.mocked(filesApi.listDir).mockResolvedValue(listResponse())
    const wrapper = mountPage()
    await flushPromises()

    const dirRow = wrapper.findAll('tr').find((row) => row.text().includes('etc'))
    if (!dirRow) {
      throw new Error('未找到目录行 etc')
    }
    expect(dirRow.text()).toContain('下载')
  })

  it('多选后「打包为 zip 下载」跳转到 download-zip 流式地址', async () => {
    vi.mocked(filesApi.listDir).mockResolvedValue(listResponse())
    const assign = vi.fn()
    vi.spyOn(window, 'location', 'get').mockReturnValue({
      ...window.location,
      assign,
    } as unknown as Location)

    const wrapper = mountPage()
    await flushPromises()

    // 勾选目录 etc 与文件 hosts.txt（el-checkbox 的实际输入框在 __original 上）。
    const boxes = wrapper.findAll('tbody .el-checkbox__original')
    await boxes[0]?.trigger('change')
    await boxes[1]?.trigger('change')
    await flushPromises()

    // 下拉内容 teleport 到 body 且惰性渲染，直接派发 command 事件更稳。
    const menu = wrapper.findAllComponents({ name: 'ElDropdown' })[0]
    if (!menu) {
      throw new Error('未找到下载下拉组件')
    }
    menu.vm.$emit('command', 'zip')
    await flushPromises()

    expect(assign).toHaveBeenCalledTimes(1)
    const url = assign.mock.calls[0]?.[0] as string
    expect(url).toContain('/api/v1/files/download-zip?')
    expect(url).toContain('path=%2F')
    expect(url).toContain('name=etc')
    expect(url).toContain('name=hosts.txt')
  })
})
