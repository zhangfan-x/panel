import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'

// 枚举 @/api/website 的全部导出（vi.mock 工厂必须列全，否则 import 报错）。
vi.mock('@/api/website', () => ({
  listSites: vi.fn(),
  getSite: vi.fn(),
  writeSite: vi.fn(),
  toggleSite: vi.fn(),
  deleteSite: vi.fn(),
  testNginx: vi.fn(),
  reloadNginx: vi.fn(),
  getNginxStatus: vi.fn(),
  listCerts: vi.fn(),
  uploadCert: vi.fn(),
  generateSelfSigned: vi.fn(),
  deleteCert: vi.fn(),
}))

import * as api from '@/api/website'
import type { SiteListResponse } from '@/api/website'
import SitesPage from './SitesPage.vue'

function siteList(): SiteListResponse {
  return {
    sites: [
      {
        name: 'blog',
        enabled: true,
        server_names: ['blog.example.com'],
        listens: ['80'],
        upstreams: ['http://127.0.0.1:3000'],
      },
      { name: 'shop', enabled: false, server_names: [], listens: [], upstreams: [] },
    ],
  }
}

function mountPage(): ReturnType<typeof mount> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return mount(SitesPage, {
    global: { plugins: [ElementPlus, pinia, [VueQueryPlugin, { queryClient }]] },
  })
}

describe('SitesPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.listCerts).mockResolvedValue({ certs: [], cert_dir: '/etc/nginx/mypanel-certs' })
    vi.mocked(api.getNginxStatus).mockResolvedValue({
      active: 'active',
      enabled: 'enabled',
      version: 'nginx/1.18.0',
    })
  })

  it('渲染站点列表、启用标记与解析出的域名', async () => {
    vi.mocked(api.listSites).mockResolvedValue(siteList())
    const wrapper = mountPage()
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('blog')
    expect(text).toContain('shop')
    expect(text).toContain('blog.example.com')
    expect(text).toContain('已启用')
    expect(text).toContain('未启用')
  })

  it('点击「测试配置」调用 testNginx', async () => {
    vi.mocked(api.listSites).mockResolvedValue({ sites: [] })
    vi.mocked(api.testNginx).mockResolvedValue({ ok: true, output: 'syntax is ok' })
    const wrapper = mountPage()
    await flushPromises()

    const button = wrapper.findAll('button').find((b) => b.text() === '测试配置')
    if (!button) {
      throw new Error('未找到「测试配置」按钮')
    }
    await button.trigger('click')
    await flushPromises()

    expect(vi.mocked(api.testNginx)).toHaveBeenCalled()
  })

  it('列表请求失败时展示错误告警', async () => {
    vi.mocked(api.listSites).mockRejectedValue({
      problem: {
        type: 'about:blank',
        title: 'nginx 不可用',
        status: 502,
        code: 'helper.unavailable',
      },
    })
    const wrapper = mountPage()
    await flushPromises()
    await flushPromises()

    expect(wrapper.text()).toContain('nginx 不可用')
  })
})
