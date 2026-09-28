import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { cancelJob, getJob, listJobs, retryJob } from '@/api/jobs'
import type { JobEventPayload, JobStatus, JobSummary } from '@/api/jobs'
import { useEventStream } from '@/composables/useEventStream'

/** 处于终态的任务状态（此后不再变化）。 */
const TERMINAL_STATUSES: readonly JobStatus[] = ['succeeded', 'failed', 'cancelled']

/**
 * 任务中心 store：任务列表缓存 + 全局抽屉开关状态。
 * 创建即订阅全局 SSE `job` 事件，把状态变更实时合并进列表缓存。
 */
export const useJobsStore = defineStore('jobs', () => {
  const jobs = ref<JobSummary[]>([])
  const total = ref(0)
  const loading = ref(false)
  /** 列表状态过滤（null 为全部）。 */
  const statusFilter = ref<JobStatus | null>(null)
  const limit = ref(20)
  const offset = ref(0)

  /** 任务抽屉（TaskDrawer）开关。 */
  const drawerOpen = ref(false)
  /** 当前展开日志的任务 id（抽屉内点击「日志」）。 */
  const activeLogJobId = ref<string | null>(null)

  /** 未结束的任务数（排队/运行/取消中），可用于角标。 */
  const runningCount = computed(
    () => jobs.value.filter((job) => !TERMINAL_STATUSES.includes(job.status)).length,
  )

  /** 按当前过滤/分页条件重新拉取任务列表。 */
  async function refresh(): Promise<void> {
    loading.value = true
    try {
      const res = await listJobs({
        status: statusFilter.value ?? undefined,
        limit: limit.value,
        offset: offset.value,
      })
      jobs.value = res.jobs
      total.value = res.total
    } finally {
      loading.value = false
    }
  }

  /** 把一条任务合并进缓存：已存在则整行替换，否则视条件插入列表顶部。 */
  function upsert(job: JobSummary): void {
    const index = jobs.value.findIndex((item) => item.id === job.id)
    if (index >= 0) {
      jobs.value.splice(index, 1, job)
      return
    }
    // 仅在第一页、且不与状态过滤冲突时插入新任务，避免污染分页/过滤视图。
    if (offset.value === 0 && (statusFilter.value === null || statusFilter.value === job.status)) {
      jobs.value.unshift(job)
      total.value += 1
    }
  }

  /** 拉取单个任务并合并进缓存；任务不存在（已清理）时静默忽略。 */
  async function refreshJob(id: string): Promise<void> {
    try {
      upsert(await getJob(id))
    } catch {
      // 任务可能已被清理或网络抖动：下一次整表刷新会纠正。
    }
  }

  /** 全局 SSE job 事件：就地更新已知任务；未知任务拉详情插入；终态补拉完整字段。 */
  function handleJobEvent(event: JobEventPayload): void {
    const index = jobs.value.findIndex((item) => item.id === event.job_id)
    if (index === -1) {
      // 新任务（可能由其他页面提交）：事件负载不含标题等字段，拉取详情后插入。
      void refreshJob(event.job_id)
      return
    }
    const job = jobs.value[index]
    if (!job) {
      return
    }
    job.status = event.status
    if (event.progress !== null) {
      job.progress = event.progress
    }
    if (event.stage !== null) {
      job.stage = event.stage
    }
    if (event.error_code !== null) {
      job.error_code = event.error_code
    }
    if (TERMINAL_STATUSES.includes(event.status)) {
      // 终态事件缺少 finished_at / error_message 等字段，补拉一次完整数据。
      void refreshJob(event.job_id)
    }
  }

  useEventStream().onJob(handleJobEvent)

  /** 请求取消任务，随后同步该任务的最新状态。 */
  async function cancel(id: string): Promise<void> {
    await cancelJob(id)
    await refreshJob(id)
  }

  /** 按原参数重试任务；返回新任务 id，并把新任务合并进列表。 */
  async function retry(id: string): Promise<string> {
    const { job_id } = await retryJob(id)
    await refreshJob(job_id)
    return job_id
  }

  /** 打开任务抽屉；可选地直接展开某任务的日志。 */
  function openDrawer(jobId?: string): void {
    drawerOpen.value = true
    if (jobId) {
      activeLogJobId.value = jobId
    }
  }

  /** 关闭任务抽屉并收起日志。 */
  function closeDrawer(): void {
    drawerOpen.value = false
    activeLogJobId.value = null
  }

  /** 切换抽屉内展开日志的任务（null 收起）。 */
  function showLog(jobId: string | null): void {
    activeLogJobId.value = jobId
  }

  return {
    jobs,
    total,
    loading,
    statusFilter,
    limit,
    offset,
    drawerOpen,
    activeLogJobId,
    runningCount,
    refresh,
    refreshJob,
    cancel,
    retry,
    openDrawer,
    closeDrawer,
    showLog,
  }
})
