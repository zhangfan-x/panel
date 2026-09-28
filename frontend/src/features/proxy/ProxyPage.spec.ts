import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'

// 屏蔽按需 SSE 流，避免测试环境真正建立 EventSource 连接。
vi.mock('@/composables/useResourceStream', () => ({
  useResourceStream: () => ({ active: { value: false }, open: vi.fn(), close: vi.fn() }),
}))

// 桩替换懒加载 / 重型组件：ECharts 图表与重认证对话框（后者会牵出 axios/router）。
vi.mock('@/components/TimeSeriesChart.vue', () => ({
  default: {
    name: 'TimeSeriesChart',
    props: ['series', 'unit', 'height', 'area', 'yMax'],
    template: '<div class="ts-stub" />',
  },
}))
vi.mock('@/components/ReauthDialog.vue', () => ({
  default: {
    name: 'ReauthDialog',
    props: ['modelValue', 'title', 'message'],
    template: '<div class="reauth-stub" />',
  },
}))

vi.mock('@/api/proxy', () => ({
  getProxyStatus: vi.fn(),
  getProxyGroups: vi.fn(),
  getProxyService: vi.fn(),
  proxyServiceAction: vi.fn(),
  getConnections: vi.fn(),
  closeConnection: vi.fn(),
  selectNode: vi.fn(),
  testDelay: vi.fn(),
  testGroupDelay: vi.fn(),
  patchMode: vi.fn(),
  closeConnections: vi.fn(),
  getActiveRules: vi.fn(),
  getCustomRules: vi.fn(),
  addCustomRule: vi.fn(),
  deleteCustomRule: vi.fn(),
  getTrafficHistory: vi.fn(),
  getConfigFile: vi.fn(),
  putConfigFile: vi.fn(),
  getProfiles: vi.fn(),
  createProfile: vi.fn(),
  updateProfile: vi.fn(),
  deleteProfile: vi.fn(),
  refreshProfile: vi.fn(),
  activateProfile: vi.fn(),
  getExtend: vi.fn(),
  putExtend: vi.fn(),
  previewExtend: vi.fn(),
  parseTraffic: vi.fn(() => null),
  proxyTrafficStreamUrl: () => '/api/v1/proxy/traffic/stream',
}))

// 组件懒加载 monaco；测试环境解析不到，桩掉后退化路径与真实退化行为一致。
vi.mock('@/components/CodeEditor.vue', () => ({
  default: {
    name: 'CodeEditor',
    props: ['modelValue', 'language', 'readonly', 'height', 'fallbackRows', 'loading'],
    template: '<div class="code-editor-stub" />',
  },
}))

import * as proxyApi from '@/api/proxy'
import type { ProxyGroupsResponse, ProxyStatus } from '@/api/proxy'
import ProxyPage from './ProxyPage.vue'

function statusResponse(): ProxyStatus {
  return {
    version: 'mihomo-1.18.0',
    mode: 'rule',
    mixed_port: 7890,
    port: 0,
    socks_port: 0,
    redir_port: 0,
    allow_lan: false,
    log_level: 'info',
  }
}

function groupsResponse(): ProxyGroupsResponse {
  return {
    groups: [
      {
        name: 'GLOBAL',
        type: 'Selector',
        now: 'HK-01',
        all: [
          { name: 'HK-01', type: 'Shadowsocks', delay: 120, delay_of: '' },
          { name: 'JP-01', type: 'Vmess', delay: 0, delay_of: '' },
          // 嵌套的代理组：自身没有延迟记录，延迟继承自它当前选中的节点。
          { name: '自动选择', type: 'URLTest', delay: 88, delay_of: 'HK-02' },
        ],
      },
    ],
  }
}

function mountPage(): ReturnType<typeof mount> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return mount(ProxyPage, {
    global: { plugins: [ElementPlus, pinia, [VueQueryPlugin, { queryClient }]] },
  })
}

describe('ProxyPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(proxyApi.getProxyStatus).mockResolvedValue(statusResponse())
    vi.mocked(proxyApi.getProxyGroups).mockResolvedValue(groupsResponse())
    vi.mocked(proxyApi.getProxyService).mockResolvedValue({
      service: 'clash.service',
      running: true,
      state: 'active',
    })
    vi.mocked(proxyApi.getTrafficHistory).mockResolvedValue({
      samples: [],
      latest: { up: 0, down: 0 },
    })
    vi.mocked(proxyApi.getProfiles).mockResolvedValue({ profiles: [], active_id: '' })
    vi.mocked(proxyApi.getExtend).mockResolvedValue({ merge: '', script: '' })
    window.localStorage.clear()
  })

  it('渲染核心服务开关与运行状态', async () => {
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('核心服务')
    expect(wrapper.text()).toContain('运行中')
    expect(wrapper.find('.proxy-page__service .el-switch').exists()).toBe(true)
  })

  it('渲染控制器状态与模式切换选项', async () => {
    const wrapper = mountPage()
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('mihomo-1.18.0')
    expect(text).toContain('7890')
    expect(text).toContain('规则')
    expect(text).toContain('全局')
    expect(text).toContain('直连')
  })

  it('渲染代理组、节点及其延迟徽标', async () => {
    const wrapper = mountPage()
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('GLOBAL')
    expect(text).toContain('HK-01')
    expect(text).toContain('JP-01')
    expect(text).toContain('120 ms')
  })

  it('嵌套代理组显示继承来的延迟而不是「—」', async () => {
    const wrapper = mountPage()
    await flushPromises()

    const nested = wrapper.findAll('.proxy-page__node').find((n) => n.text().includes('自动选择'))
    if (!nested) {
      throw new Error('未找到嵌套组 自动选择')
    }
    expect(nested.text()).toContain('88 ms')
    expect(nested.text()).not.toContain('—')
  })

  it('点击测速对整组只发一个请求', async () => {
    vi.mocked(proxyApi.testGroupDelay).mockResolvedValue({
      delays: { 'HK-01': 150, 'JP-01': 210 },
      batched: true,
    })
    const wrapper = mountPage()
    await flushPromises()

    const testButton = wrapper.findAll('button').find((b) => b.text().includes('测速'))
    if (!testButton) {
      throw new Error('未找到测速按钮')
    }
    await testButton.trigger('click')
    await flushPromises()

    expect(vi.mocked(proxyApi.testGroupDelay)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(proxyApi.testGroupDelay)).toHaveBeenCalledWith('GLOBAL')
    expect(vi.mocked(proxyApi.testDelay)).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('150 ms')
    // 批量结果里没有的成员回落为「—」，不会残留旧的继承值。
    const nested = wrapper.findAll('.proxy-page__node').find((n) => n.text().includes('自动选择'))
    expect(nested?.text()).toContain('—')
  })

  it('点击非当前节点会调用切换接口', async () => {
    vi.mocked(proxyApi.selectNode).mockResolvedValue(undefined)
    const wrapper = mountPage()
    await flushPromises()

    const target = wrapper
      .findAll('.proxy-page__node')
      .find((node) => node.text().includes('JP-01'))
    if (!target) {
      throw new Error('未找到节点 JP-01')
    }
    await target.trigger('click')
    await flushPromises()

    expect(vi.mocked(proxyApi.selectNode)).toHaveBeenCalledWith('GLOBAL', 'JP-01')
  })

  it('点击节点的延迟徽标只重测该节点', async () => {
    vi.mocked(proxyApi.testDelay).mockResolvedValue({ delay: 42 })
    const wrapper = mountPage()
    await flushPromises()

    const target = wrapper
      .findAll('.proxy-page__node')
      .find((node) => node.text().includes('JP-01'))
    if (!target) {
      throw new Error('未找到节点 JP-01')
    }
    await target.find('.proxy-page__node-delay').trigger('click')
    await flushPromises()

    expect(vi.mocked(proxyApi.testDelay)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(proxyApi.testDelay)).toHaveBeenCalledWith('JP-01')
    expect(vi.mocked(proxyApi.testGroupDelay)).not.toHaveBeenCalled()
    // 点击延迟徽标不应顺带触发节点切换。
    expect(vi.mocked(proxyApi.selectNode)).not.toHaveBeenCalled()
    expect(target.text()).toContain('42 ms')
  })

  it('折叠代理组后隐藏节点并记住状态', async () => {
    const wrapper = mountPage()
    await flushPromises()

    const header = wrapper.find('.proxy-page__group-title')
    await header.trigger('click')
    await flushPromises()

    // v-show 折叠：节点仍在 DOM 中但不可见。
    expect(wrapper.find('.proxy-page__nodes').attributes('style')).toContain('display: none')
    expect(window.localStorage.getItem('proxy.groups.collapsed')).toContain('GLOBAL')

    // 重新挂载（等价于刷新页面）后仍保持折叠。
    const reopened = mountPage()
    await flushPromises()
    expect(reopened.find('.proxy-page__nodes').attributes('style')).toContain('display: none')
  })

  it('保存并应用后不再显示「未写入磁盘」的预览结果', async () => {
    vi.mocked(proxyApi.previewExtend).mockResolvedValue({
      content: 'mixed-port: 30010\n',
      logs: ['改了 5 个组'],
    })
    vi.mocked(proxyApi.putExtend).mockResolvedValue({ merge: '', script: '' })
    const wrapper = mountPage()
    await flushPromises()

    const tab = wrapper.findAll('.el-tabs__item').find((t) => t.text() === '扩展')
    if (!tab) {
      throw new Error('未找到「扩展」页签')
    }
    await tab.trigger('click')
    await flushPromises()

    const button = (label: string) => wrapper.findAll('button').find((b) => b.text() === label)
    await button('预览')?.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('预览结果（未写入磁盘）')
    expect(wrapper.text()).toContain('改了 5 个组')

    await button('保存并应用')?.trigger('click')
    await flushPromises()

    expect(vi.mocked(proxyApi.putExtend)).toHaveBeenCalledTimes(1)
    // 内容已经落盘了，「未写入磁盘」这句话不再成立。
    expect(wrapper.text()).not.toContain('预览结果（未写入磁盘）')
    expect(wrapper.text()).not.toContain('改了 5 个组')
  })
})
