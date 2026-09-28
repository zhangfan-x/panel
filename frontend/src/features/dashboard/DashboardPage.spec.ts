import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'

// 屏蔽全局 SSE 单例：记录 onMetrics 回调，测试里手动推送采样。
const streamState = vi.hoisted(() => ({
  metricsHandlers: [] as Array<(sample: unknown) => void>,
}))

vi.mock('@/composables/useEventStream', async () => {
  const { ref } = await import('vue')
  return {
    useEventStream: () => ({
      connected: ref(true),
      onMetrics: (handler: (sample: unknown) => void) => {
        streamState.metricsHandlers.push(handler)
        return () => {}
      },
      onJob: () => () => {},
    }),
    startEventStream: vi.fn(),
    stopEventStream: vi.fn(),
  }
})

// 图表组件桩：真实组件懒加载 ECharts，happy-dom 无 canvas，不在单测中渲染。
vi.mock('@/components/TimeSeriesChart.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    default: defineComponent({
      name: 'TimeSeriesChart',
      props: {
        series: { type: Array, default: () => [] },
        height: { type: Number, default: 260 },
        unit: { type: String, default: '' },
        yMax: { type: Number, default: undefined },
        area: { type: Boolean, default: false },
      },
      setup() {
        return () => h('div', { class: 'ts-chart-stub' })
      },
    }),
  }
})

vi.mock('@/api/system', () => ({
  getSystemInfo: vi.fn(),
  getOverview: vi.fn(),
  getMetrics: vi.fn(),
}))

import * as systemApi from '@/api/system'
import type { MetricSample, SystemOverview } from '@/api/system'
import DashboardPage from './DashboardPage.vue'

function sample(overrides: Partial<MetricSample> = {}): MetricSample {
  return {
    ts: '2026-07-22T10:00:00+00:00',
    cpu_percent: 32.5,
    per_cpu: [30, 35],
    load: [0.52, 0.4, 0.3],
    mem: { total: 8 * 1024 ** 3, used: 4 * 1024 ** 3, available: 4 * 1024 ** 3, percent: 50 },
    swap: { total: 2 * 1024 ** 3, used: 512 * 1024 ** 2, percent: 25 },
    disks: [
      {
        mount: '/',
        device: '/dev/sda1',
        fstype: 'ext4',
        total: 100 * 1024 ** 3,
        used: 40 * 1024 ** 3,
        percent: 40,
      },
    ],
    net: { rx_bytes_per_s: 1048576, tx_bytes_per_s: 524288, rx_total: 100, tx_total: 50 },
    ...overrides,
  }
}

function overviewResponse(): SystemOverview {
  return {
    host: {
      hostname: 'srv-01',
      kernel: '6.8.0-124-generic',
      os_release: 'Ubuntu 24.04 LTS',
      arch: 'x86_64',
      uptime_s: 86400,
      cpu_count: 2,
    },
    metrics: sample(),
    docker: { available: true, running: 2, total: 3 },
    systemd: { failed: 1, available: true, total: 120 },
    recent_jobs: [
      {
        id: 'j1',
        type: 'compose.up',
        title: '启动 Compose 项目 alpha',
        status: 'succeeded',
        created_at: '2026-07-22T09:00:00+00:00',
      },
    ],
    security_events: [
      {
        event_id: 'e1',
        ts: '2026-07-22T08:00:00+00:00',
        action: 'auth.login',
        resource: 'user:admin',
        result: 'denied',
        error_code: 'auth.invalid_credentials',
        source_ip: '10.0.0.8',
      },
    ],
  }
}

function mountPage(): ReturnType<typeof mount> {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return mount(DashboardPage, {
    global: { plugins: [ElementPlus, [VueQueryPlugin, { queryClient }]] },
  })
}

describe('DashboardPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    streamState.metricsHandlers.length = 0
  })

  it('渲染指标卡、状态徽章、磁盘、最近任务与安全事件', async () => {
    vi.mocked(systemApi.getOverview).mockResolvedValue(overviewResponse())
    vi.mocked(systemApi.getMetrics).mockResolvedValue({ samples: [sample()], source: 'live' })
    const wrapper = mountPage()
    await flushPromises()

    const text = wrapper.text()
    // 指标卡
    expect(text).toContain('CPU 使用率')
    expect(text).toContain('32.5')
    expect(text).toContain('内存')
    expect(text).toContain('4.0 GiB')
    expect(text).toContain('系统负载')
    expect(text).toContain('0.52')
    expect(text).toContain('1.0 MiB/s')
    // 状态徽章
    expect(text).toContain('实时推送已连接')
    expect(text).toContain('Docker 运行 2 / 3')
    expect(text).toContain('systemd 失败单元 1')
    // 磁盘卡
    expect(text).toContain('1 个挂载点')
    expect(text).toContain('40.0 GiB')
    // 最近任务与安全事件
    expect(text).toContain('启动 Compose 项目 alpha')
    expect(text).toContain('已成功')
    expect(text).toContain('auth.login')
    expect(text).toContain('denied')
    // 主机信息
    expect(text).toContain('srv-01')
    expect(text).toContain('Ubuntu 24.04 LTS')

    wrapper.unmount()
  })

  it('收到 SSE metrics 事件后实时更新最新数值', async () => {
    vi.mocked(systemApi.getOverview).mockResolvedValue(overviewResponse())
    vi.mocked(systemApi.getMetrics).mockResolvedValue({ samples: [sample()], source: 'live' })
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('32.5')

    for (const handler of streamState.metricsHandlers) {
      handler(sample({ ts: '2026-07-22T10:00:05+00:00', cpu_percent: 77.7 }))
    }
    await flushPromises()

    expect(wrapper.text()).toContain('77.7')
    wrapper.unmount()
  })

  it('概览请求失败时展示错误告警', async () => {
    vi.mocked(systemApi.getOverview).mockRejectedValue({
      problem: {
        type: 'about:blank',
        title: '后端暂不可用',
        status: 502,
        code: 'helper.unavailable',
      },
    })
    vi.mocked(systemApi.getMetrics).mockResolvedValue({ samples: [], source: 'history' })
    const wrapper = mountPage()
    await flushPromises()
    await flushPromises()

    expect(wrapper.text()).toContain('后端暂不可用')
    wrapper.unmount()
  })
})
