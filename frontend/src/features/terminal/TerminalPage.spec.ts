import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'

vi.mock('@/api/terminal', () => ({
  createTerminalSession: vi.fn(),
  terminalWsUrl: vi.fn(() => 'ws://127.0.0.1:5173/api/v1/terminal/ws'),
}))

import * as terminalApi from '@/api/terminal'
import ReauthDialog from '@/components/ReauthDialog.vue'
import TerminalPage from './TerminalPage.vue'

function mountPage(): ReturnType<typeof mount> {
  const pinia = createPinia()
  setActivePinia(pinia)
  return mount(TerminalPage, {
    global: { plugins: [ElementPlus, pinia] },
  })
}

describe('TerminalPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('初始渲染入口门并提示 root 权限风险', () => {
    const wrapper = mountPage()
    const text = wrapper.text()
    expect(text).toContain('打开 root 终端')
    expect(text).toContain('root 权限')
    expect(vi.mocked(terminalApi.createTerminalSession)).not.toHaveBeenCalled()
  })

  it('点击入口按钮先弹出重认证对话框，而非直接建会话', async () => {
    const wrapper = mountPage()
    const button = wrapper.findAll('button').find((b) => b.text() === '打开 root 终端')
    if (!button) {
      throw new Error('未找到「打开 root 终端」按钮')
    }
    await button.trigger('click')

    expect(wrapper.findComponent(ReauthDialog).props('modelValue')).toBe(true)
    expect(vi.mocked(terminalApi.createTerminalSession)).not.toHaveBeenCalled()
  })

  it('终端服务不可用（503 terminal.unavailable）时展示禁用态', async () => {
    vi.mocked(terminalApi.createTerminalSession).mockRejectedValue({
      problem: {
        type: 'about:blank',
        title: 'Terminal Service Unavailable',
        status: 503,
        code: 'terminal.unavailable',
      },
    })
    const wrapper = mountPage()
    wrapper.findComponent(ReauthDialog).vm.$emit('success')
    await flushPromises()

    expect(vi.mocked(terminalApi.createTerminalSession)).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('终端服务未运行')
  })
})
