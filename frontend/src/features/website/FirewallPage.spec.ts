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

// 纯工具函数 ruleFingerprint 保留真实实现：删除请求要用它算指纹。
vi.mock('@/api/firewall', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/firewall')>()
  return {
    ...actual,
    getFirewallStatus: vi.fn(),
    enableFirewall: vi.fn(),
    disableFirewall: vi.fn(),
    addFirewallRule: vi.fn(),
    deleteFirewallRule: vi.fn(),
    setFirewallDefault: vi.fn(),
    reloadFirewall: vi.fn(),
  }
})

import * as firewallApi from '@/api/firewall'
import type { FirewallStatus } from '@/api/firewall'
import FirewallPage from './FirewallPage.vue'

function statusResponse(overrides: Partial<FirewallStatus> = {}): FirewallStatus {
  return {
    installed: true,
    active: true,
    logging: 'on (low)',
    default_incoming: 'deny',
    default_outgoing: 'allow',
    default_routed: 'deny',
    rules: [
      { num: 1, to: '22/tcp', action: 'ALLOW IN', from: 'Anywhere', comment: '', v6: false },
      {
        num: 2,
        to: '3389/tcp',
        action: 'ALLOW IN',
        from: 'Anywhere',
        comment: 'rdp',
        v6: false,
      },
    ],
    ssh_ports: [22],
    panel_port: 25443,
    protected_ports: [22, 25443],
    uncovered_ports: [25443],
    ...overrides,
  }
}

function mountPage(): ReturnType<typeof mount> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return mount(FirewallPage, {
    global: { plugins: [ElementPlus, pinia, [VueQueryPlugin, { queryClient }]] },
  })
}

describe('FirewallPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = ''
    vi.mocked(firewallApi.getFirewallStatus).mockResolvedValue(statusResponse())
  })

  it('渲染状态、默认策略与规则列表', async () => {
    const wrapper = mountPage()
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('防火墙 (ufw)')
    expect(text).toContain('22/tcp')
    expect(text).toContain('3389/tcp')
    expect(text).toContain('on (low)')
  })

  it('未覆盖的受保护端口给出显著告警', async () => {
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('受保护端口 25443 当前没有放行规则')
  })

  it('受保护端口的规则带 🔒 标记，普通规则没有', async () => {
    const wrapper = mountPage()
    await flushPromises()

    const rows = wrapper.findAll('tbody tr')
    const sshRow = rows.find((r) => r.text().includes('22/tcp'))
    const rdpRow = rows.find((r) => r.text().includes('3389/tcp'))
    expect(sshRow?.text()).toContain('受保护')
    expect(rdpRow?.text()).not.toContain('受保护')
  })

  it('删除规则会带上指纹，防止 ufw 重排序号后误删', async () => {
    vi.mocked(firewallApi.deleteFirewallRule).mockResolvedValue(statusResponse())
    const wrapper = mountPage()
    await flushPromises()

    const rdpRow = wrapper.findAll('tbody tr').find((r) => r.text().includes('3389/tcp'))
    const deleteButton = rdpRow?.findAll('button').find((b) => b.text().includes('删除'))
    if (!deleteButton) {
      throw new Error('未找到删除按钮')
    }
    await deleteButton.trigger('click')
    await flushPromises()

    expect(vi.mocked(firewallApi.deleteFirewallRule)).toHaveBeenCalledWith(
      2,
      '3389/tcp|ALLOW IN|Anywhere',
      false,
    )
  })

  it('后端报断连风险(409)时弹确认框，确认后带 confirm_lockout 重试', async () => {
    vi.mocked(firewallApi.deleteFirewallRule)
      .mockRejectedValueOnce({
        problem: { code: 'conflict', detail: '删除该规则后 SSH/面板端口 22 将没有放行规则' },
      })
      .mockResolvedValueOnce(statusResponse())

    const wrapper = mountPage()
    await flushPromises()

    const sshRow = wrapper.findAll('tbody tr').find((r) => r.text().includes('22/tcp'))
    const deleteButton = sshRow?.findAll('button').find((b) => b.text().includes('删除'))
    await deleteButton?.trigger('click')
    await flushPromises()

    // 第一次被拒，风险对话框出现且尚未重试。
    expect(wrapper.text()).toContain('将没有放行规则')
    expect(vi.mocked(firewallApi.deleteFirewallRule)).toHaveBeenCalledTimes(1)

    const confirm = wrapper.findAll('button').find((b) => b.text().includes('知晓风险'))
    if (!confirm) {
      throw new Error('未找到风险确认按钮')
    }
    await confirm.trigger('click')
    await flushPromises()

    expect(vi.mocked(firewallApi.deleteFirewallRule)).toHaveBeenLastCalledWith(
      1,
      '22/tcp|ALLOW IN|Anywhere',
      true,
    )
  })

  it('未安装 ufw 时整页降级为提示而不是报错', async () => {
    vi.mocked(firewallApi.getFirewallStatus).mockResolvedValue(
      statusResponse({ installed: false, active: false, rules: [] }),
    )
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('本机未安装 ufw')
  })
})
