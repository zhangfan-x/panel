import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'

// 只替换网络函数，保留 formatBytes / shortId 等纯工具的真实实现。
vi.mock('@/api/docker', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/docker')>()
  return {
    ...actual,
    getContainerInspect: vi.fn(),
    getContainerStats: vi.fn(),
    containerAction: vi.fn(),
  }
})

import * as dockerApi from '@/api/docker'
import type { ContainerInspect, ContainerStats } from '@/api/docker'
import ContainerDetail from './ContainerDetail.vue'

function inspectResponse(): ContainerInspect {
  return {
    id: 'abcdef123456789000',
    name: 'web',
    image: 'nginx:alpine',
    image_id: 'sha256:deadbeefcafe0000',
    created: '2026-07-22T08:00:00Z',
    state: {
      status: 'running',
      running: true,
      paused: false,
      exit_code: 0,
      started_at: '2026-07-22T08:00:01Z',
      finished_at: '',
      error: '',
      pid: 1234,
      health: null,
    },
    restart_policy: 'always',
    cmd: ['nginx', '-g', 'daemon off;'],
    entrypoint: null,
    env: ['PATH=/usr/local/bin', 'DB_PASSWORD=s3cr3t-value'],
    labels: {},
    ports: { '80/tcp': [{ HostIp: '0.0.0.0', HostPort: '8080' }] },
    networks: {
      bridge: { ip_address: '172.17.0.2', gateway: '172.17.0.1', mac_address: '02:42:ac:11:00:02' },
    },
    mounts: [{ type: 'volume', source: '/var/lib/data', destination: '/data', rw: true }],
  }
}

function statsResponse(): ContainerStats {
  return {
    read: '2026-07-22T10:00:00Z',
    cpu_percent: 12.5,
    online_cpus: 4,
    mem_used: 150 * 1024 * 1024,
    mem_limit: 1024 * 1024 * 1024,
    mem_percent: 14.65,
    net_rx: 1005,
    net_tx: 2007,
    blk_read: 4096,
    blk_write: 8192,
    pids: 3,
  }
}

function mountDetail(): ReturnType<typeof mount> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return mount(ContainerDetail, {
    props: { containerId: 'abcdef123456789000' },
    global: { plugins: [ElementPlus, pinia, [VueQueryPlugin, { queryClient }]] },
  })
}

async function settle(): Promise<void> {
  // inspect 先就绪 → isRunning 打开后 stats 查询才会开始，故多次冲刷。
  for (let i = 0; i < 4; i += 1) {
    await flushPromises()
  }
}

describe('ContainerDetail', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('渲染 inspect 基本信息、端口、网络与命令', async () => {
    vi.mocked(dockerApi.getContainerInspect).mockResolvedValue(inspectResponse())
    vi.mocked(dockerApi.getContainerStats).mockResolvedValue(statsResponse())
    const wrapper = mountDetail()
    await settle()

    const text = wrapper.text()
    expect(text).toContain('web')
    expect(text).toContain('nginx:alpine')
    expect(text).toContain('运行中')
    expect(text).toContain('abcdef123456') // shortId(id)
    expect(text).toContain('8080->80/tcp')
    expect(text).toContain('172.17.0.2')
    expect(text).toContain('daemon off;')
  })

  it('实时统计条展示 CPU 与内存', async () => {
    vi.mocked(dockerApi.getContainerInspect).mockResolvedValue(inspectResponse())
    vi.mocked(dockerApi.getContainerStats).mockResolvedValue(statsResponse())
    const wrapper = mountDetail()
    await settle()

    expect(vi.mocked(dockerApi.getContainerStats)).toHaveBeenCalledWith('abcdef123456789000')
    const text = wrapper.text()
    expect(text).toContain('12.5%') // cpu_percent
    expect(text).toContain('150 MB') // formatBytes(mem_used)
  })

  it('敏感环境变量以密文输入展示，普通变量明文展示', async () => {
    vi.mocked(dockerApi.getContainerInspect).mockResolvedValue(inspectResponse())
    vi.mocked(dockerApi.getContainerStats).mockResolvedValue(statsResponse())
    const wrapper = mountDetail()
    await settle()

    // DB_PASSWORD 命中密钥正则 → 渲染 SecretInput（password 输入框）。
    expect(wrapper.find('input[type="password"]').exists()).toBe(true)
    // 普通变量明文可见。
    expect(wrapper.text()).toContain('/usr/local/bin')
  })

  it('点击「返回」派发 back 事件', async () => {
    vi.mocked(dockerApi.getContainerInspect).mockResolvedValue(inspectResponse())
    vi.mocked(dockerApi.getContainerStats).mockResolvedValue(statsResponse())
    const wrapper = mountDetail()
    await settle()

    const backButton = wrapper.findAll('button').find((button) => button.text() === '返回')
    if (!backButton) {
      throw new Error('未找到「返回」按钮')
    }
    await backButton.trigger('click')
    expect(wrapper.emitted('back')).toBeTruthy()
  })
})
