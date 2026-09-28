import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

// 通过 vi.hoisted 提前创建收集器，供 mock 工厂捕获 store 注册的 job 事件回调。
const hoisted = vi.hoisted(() => ({
  jobHandlers: [] as Array<(event: unknown) => void>,
}))

vi.mock('@/composables/useEventStream', () => ({
  useEventStream: () => ({
    connected: { value: false },
    onMetrics: () => () => {},
    onJob: (handler: (event: unknown) => void) => {
      hoisted.jobHandlers.push(handler)
      return () => {}
    },
  }),
  startEventStream: vi.fn(),
  stopEventStream: vi.fn(),
}))

vi.mock('@/api/jobs', () => ({
  listJobs: vi.fn(),
  getJob: vi.fn(),
  cancelJob: vi.fn(),
  retryJob: vi.fn(),
}))

import * as jobsApi from '@/api/jobs'
import type { JobEventPayload, JobSummary } from '@/api/jobs'
import { useJobsStore } from './jobs'

function jobSummary(overrides: Partial<JobSummary> = {}): JobSummary {
  return {
    id: 'job-1',
    type: 'demo.sleep',
    title: '演示任务',
    status: 'running',
    progress: 40,
    stage: null,
    created_at: '2026-07-22T10:00:00+00:00',
    started_at: '2026-07-22T10:00:01+00:00',
    finished_at: null,
    error_code: null,
    error_message: null,
    resource_key: null,
    ...overrides,
  }
}

/** 模拟全局 SSE 推送一条 job 事件。 */
function emitJob(event: JobEventPayload): void {
  for (const handler of hoisted.jobHandlers) {
    handler(event)
  }
}

describe('useJobsStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    hoisted.jobHandlers.length = 0
  })

  it('refresh 按过滤/分页条件拉取列表并更新缓存', async () => {
    vi.mocked(jobsApi.listJobs).mockResolvedValue({ jobs: [jobSummary()], total: 1 })
    const store = useJobsStore()
    await store.refresh()
    expect(jobsApi.listJobs).toHaveBeenCalledWith({ status: undefined, limit: 20, offset: 0 })
    expect(store.jobs).toHaveLength(1)
    expect(store.total).toBe(1)
    expect(store.loading).toBe(false)

    store.statusFilter = 'failed'
    store.offset = 20
    await store.refresh()
    expect(jobsApi.listJobs).toHaveBeenLastCalledWith({ status: 'failed', limit: 20, offset: 20 })
  })

  it('收到已知任务的 job 事件时就地更新状态与进度', async () => {
    vi.mocked(jobsApi.listJobs).mockResolvedValue({ jobs: [jobSummary()], total: 1 })
    const store = useJobsStore()
    await store.refresh()

    emitJob({ job_id: 'job-1', status: 'running', progress: 80, stage: 'copy', error_code: null })
    expect(store.jobs[0]?.status).toBe('running')
    expect(store.jobs[0]?.progress).toBe(80)
    expect(store.jobs[0]?.stage).toBe('copy')
    expect(jobsApi.getJob).not.toHaveBeenCalled()
  })

  it('终态事件触发补拉完整任务数据', async () => {
    vi.mocked(jobsApi.listJobs).mockResolvedValue({ jobs: [jobSummary()], total: 1 })
    vi.mocked(jobsApi.getJob).mockResolvedValue(
      jobSummary({ status: 'succeeded', progress: 100, finished_at: '2026-07-22T10:01:00+00:00' }),
    )
    const store = useJobsStore()
    await store.refresh()

    emitJob({ job_id: 'job-1', status: 'succeeded', progress: null, stage: null, error_code: null })
    await flushPromises()
    expect(jobsApi.getJob).toHaveBeenCalledWith('job-1')
    expect(store.jobs[0]?.status).toBe('succeeded')
    expect(store.jobs[0]?.finished_at).toBe('2026-07-22T10:01:00+00:00')
  })

  it('未知任务的事件会拉取详情并插入列表顶部', async () => {
    vi.mocked(jobsApi.listJobs).mockResolvedValue({ jobs: [jobSummary()], total: 1 })
    vi.mocked(jobsApi.getJob).mockResolvedValue(jobSummary({ id: 'job-2', status: 'queued' }))
    const store = useJobsStore()
    await store.refresh()

    emitJob({ job_id: 'job-2', status: 'queued', progress: null, stage: null, error_code: null })
    await flushPromises()
    expect(store.jobs[0]?.id).toBe('job-2')
    expect(store.jobs).toHaveLength(2)
    expect(store.total).toBe(2)
  })

  it('状态过滤生效时不插入不匹配的新任务', async () => {
    vi.mocked(jobsApi.listJobs).mockResolvedValue({ jobs: [jobSummary()], total: 1 })
    vi.mocked(jobsApi.getJob).mockResolvedValue(jobSummary({ id: 'job-2', status: 'queued' }))
    const store = useJobsStore()
    await store.refresh()
    store.statusFilter = 'failed'

    emitJob({ job_id: 'job-2', status: 'queued', progress: null, stage: null, error_code: null })
    await flushPromises()
    expect(store.jobs).toHaveLength(1)
    expect(store.jobs[0]?.id).toBe('job-1')
  })

  it('cancel 调用取消接口并同步该任务的最新状态', async () => {
    vi.mocked(jobsApi.listJobs).mockResolvedValue({ jobs: [jobSummary()], total: 1 })
    vi.mocked(jobsApi.cancelJob).mockResolvedValue(undefined)
    vi.mocked(jobsApi.getJob).mockResolvedValue(jobSummary({ status: 'cancelled' }))
    const store = useJobsStore()
    await store.refresh()

    await store.cancel('job-1')
    expect(jobsApi.cancelJob).toHaveBeenCalledWith('job-1')
    expect(store.jobs[0]?.status).toBe('cancelled')
  })

  it('retry 返回新任务 id 并把新任务并入列表', async () => {
    vi.mocked(jobsApi.listJobs).mockResolvedValue({
      jobs: [jobSummary({ status: 'failed' })],
      total: 1,
    })
    vi.mocked(jobsApi.retryJob).mockResolvedValue({ job_id: 'job-9' })
    vi.mocked(jobsApi.getJob).mockResolvedValue(jobSummary({ id: 'job-9', status: 'queued' }))
    const store = useJobsStore()
    await store.refresh()

    const newId = await store.retry('job-1')
    expect(newId).toBe('job-9')
    expect(jobsApi.retryJob).toHaveBeenCalledWith('job-1')
    expect(store.jobs[0]?.id).toBe('job-9')
  })

  it('抽屉开关与日志展开状态', () => {
    const store = useJobsStore()
    expect(store.drawerOpen).toBe(false)

    store.openDrawer('job-1')
    expect(store.drawerOpen).toBe(true)
    expect(store.activeLogJobId).toBe('job-1')

    store.showLog(null)
    expect(store.activeLogJobId).toBeNull()

    store.openDrawer()
    store.showLog('job-2')
    expect(store.activeLogJobId).toBe('job-2')

    store.closeDrawer()
    expect(store.drawerOpen).toBe(false)
    expect(store.activeLogJobId).toBeNull()
  })
})
