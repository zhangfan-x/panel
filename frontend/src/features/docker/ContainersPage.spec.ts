import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus, { ElDropdown } from 'element-plus'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'

// 只替换网络函数，保留 formatPorts / isDockerUnavailable 等纯工具的真实实现。
vi.mock('@/api/docker', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/docker')>()
  return {
    ...actual,
    getDockerInfo: vi.fn(),
    listContainers: vi.fn(),
    containerAction: vi.fn(),
    getAllContainerStats: vi.fn(),
  }
})

import * as dockerApi from '@/api/docker'
import type { AllContainerStatsResponse, ContainerListResponse, DockerInfo } from '@/api/docker'
import ContainersPage from './ContainersPage.vue'

function infoResponse(): DockerInfo {
  return {
    available: true,
    version: '27.0.1',
    api_version: '1.47',
    os: 'Ubuntu 24.04',
    kernel: '6.8.0',
    arch: 'x86_64',
    containers: 2,
    running: 1,
    paused: 0,
    stopped: 1,
    images: 3,
  }
}

function listResponse(): ContainerListResponse {
  return {
    containers: [
      {
        id: 'c1',
        name: 'web',
        image: 'nginx:alpine',
        state: 'running',
        status: 'Up 2 hours',
        ports: [{ IP: '0.0.0.0', PrivatePort: 80, PublicPort: 8080, Type: 'tcp' }],
        created: 1753150000,
        labels: {},
      },
      {
        id: 'c2',
        name: 'worker',
        image: 'busybox:latest',
        state: 'exited',
        status: 'Exited (0) 3 hours ago',
        ports: [],
        created: 1753140000,
        labels: {},
      },
    ],
  }
}

/** 批量 stats：仅运行中的 c1 有快照（c2 已退出,CPU/内存列应显示 —）。 */
function statsResponse(): AllContainerStatsResponse {
  return {
    stats: {
      c1: {
        read: '2026-07-22T10:00:00Z',
        cpu_percent: 12.3,
        online_cpus: 4,
        mem_used: 128 * 1024 * 1024,
        mem_limit: 1024 * 1024 * 1024,
        mem_percent: 12.5,
        net_rx: 0,
        net_tx: 0,
        blk_read: 0,
        blk_write: 0,
        pids: 5,
      },
    },
  }
}

function mountPage(): ReturnType<typeof mount> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return mount(ContainersPage, {
    global: { plugins: [ElementPlus, pinia, [VueQueryPlugin, { queryClient }]] },
  })
}

describe('ContainersPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('渲染容器列表:名称、镜像、状态、端口映射与 CPU/内存占用', async () => {
    vi.mocked(dockerApi.getDockerInfo).mockResolvedValue(infoResponse())
    vi.mocked(dockerApi.listContainers).mockResolvedValue(listResponse())
    vi.mocked(dockerApi.getAllContainerStats).mockResolvedValue(statsResponse())
    const wrapper = mountPage()
    await flushPromises()

    expect(vi.mocked(dockerApi.listContainers)).toHaveBeenCalledWith(true)
    const text = wrapper.text()
    expect(text).toContain('web')
    expect(text).toContain('nginx:alpine')
    expect(text).toContain('运行中')
    expect(text).toContain('worker')
    expect(text).toContain('已退出')
    expect(text).toContain('8080->80/tcp')
    // c1 运行中有快照:CPU 百分比 + 内存已用/百分比;c2 无快照,列显示 —。
    expect(text).toContain('12.3%')
    expect(text).toContain('128 MB')
    expect(text).toContain('12.5%')
    expect(text).toContain('—')
  })

  it('守护进程不可用时展示空态而非报错', async () => {
    vi.mocked(dockerApi.getDockerInfo).mockResolvedValue({ available: false })
    vi.mocked(dockerApi.getAllContainerStats).mockResolvedValue({ stats: {} })
    vi.mocked(dockerApi.listContainers).mockRejectedValue({
      problem: {
        type: 'about:blank',
        title: 'Docker Daemon Unavailable',
        status: 503,
        code: 'docker.unavailable',
      },
    })
    const wrapper = mountPage()
    await flushPromises()
    await flushPromises()

    expect(wrapper.text()).toContain('Docker 守护进程不可用')
    expect(wrapper.text()).not.toContain('Docker Daemon Unavailable')
  })

  it('经「更多」下拉点选「停止」对该容器调用 stop 动作', async () => {
    vi.mocked(dockerApi.getDockerInfo).mockResolvedValue(infoResponse())
    vi.mocked(dockerApi.listContainers).mockResolvedValue(listResponse())
    vi.mocked(dockerApi.getAllContainerStats).mockResolvedValue(statsResponse())
    vi.mocked(dockerApi.containerAction).mockResolvedValue(undefined)
    const wrapper = mountPage()
    await flushPromises()

    // 下拉菜单渲染在 body 传送门中,点击触发不便;直接对第一行(running 的 c1)
    // 的 ElDropdown 发出 command 事件,等价于用户展开后点选「停止」。
    const dropdown = wrapper.findComponent(ElDropdown)
    expect(dropdown.exists()).toBe(true)
    dropdown.vm.$emit('command', 'stop')
    await flushPromises()

    expect(vi.mocked(dockerApi.containerAction)).toHaveBeenCalledWith('c1', 'stop')
  })
})
