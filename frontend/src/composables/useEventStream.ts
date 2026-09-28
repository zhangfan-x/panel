import { readonly, ref } from 'vue'
import type { Ref } from 'vue'
import { tryOnScopeDispose } from '@vueuse/core'

import { openEventStream } from '@/api/events'
import type { JobEventPayload } from '@/api/jobs'

/** metrics 事件回调（负载为后端 MetricSampleData JSON，具体类型由调用方断言）。 */
export type MetricsHandler = (sample: unknown) => void

/** job 事件回调。 */
export type JobEventHandler = (event: JobEventPayload) => void

/** useEventStream 返回的订阅接口。 */
export interface EventStreamApi {
  /** 当前是否已连接（收到 open 后为 true，出错后为 false）。 */
  connected: Readonly<Ref<boolean>>
  /** 订阅 metrics 事件；返回退订函数（组件作用域内会自动退订）。 */
  onMetrics: (handler: MetricsHandler) => () => void
  /** 订阅 job 事件；返回退订函数（组件作用域内会自动退订）。 */
  onJob: (handler: JobEventHandler) => () => void
}

/** 重连退避：1s 起步、每次翻倍、封顶 30s；连接成功后复位。 */
const BASE_DELAY_MS = 1_000
const MAX_DELAY_MS = 30_000

// —— 模块级单例状态：整个应用只保持一条 /api/v1/events 连接。 ——
const metricsHandlers = new Set<MetricsHandler>()
const jobHandlers = new Set<JobEventHandler>()
const connected = ref(false)

let closeStream: (() => void) | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let attempt = 0
let started = false

/** 按 topic 分发到已注册的回调。 */
function dispatch(topic: string, data: unknown): void {
  if (topic === 'metrics') {
    for (const handler of metricsHandlers) {
      handler(data)
    }
    return
  }
  if (topic === 'job' && data && typeof data === 'object') {
    const event = data as JobEventPayload
    for (const handler of jobHandlers) {
      handler(event)
    }
  }
  // 'ready' 仅为服务端首帧握手，无需分发。
}

function scheduleReconnect(): void {
  if (reconnectTimer !== null || !started) {
    return
  }
  const delay = Math.min(MAX_DELAY_MS, BASE_DELAY_MS * 2 ** attempt)
  attempt += 1
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    if (started) {
      connect()
    }
  }, delay)
}

function connect(): void {
  if (typeof EventSource === 'undefined') {
    // 非浏览器环境（如未 stub 的测试）：保持断开，不抛错。
    return
  }
  closeStream?.()
  closeStream = openEventStream(dispatch, {
    onOpen: () => {
      connected.value = true
      attempt = 0
    },
    onError: () => {
      // 主动关闭并按指数退避重连，不依赖浏览器默认的固定间隔重连。
      connected.value = false
      closeStream?.()
      closeStream = null
      scheduleReconnect()
    },
  })
}

/** 启动全局事件流（幂等；首个调用方触发建连）。 */
export function startEventStream(): void {
  if (started) {
    return
  }
  started = true
  connect()
}

/** 关闭全局事件流并清理重连定时器（登出/测试用）。 */
export function stopEventStream(): void {
  started = false
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  closeStream?.()
  closeStream = null
  connected.value = false
  attempt = 0
}

/**
 * 全局事件流组合函数：懒启动单例连接（metrics + job 多路复用），
 * 提供按 topic 的订阅接口；在组件 setup 中调用时退订随作用域自动完成。
 */
export function useEventStream(): EventStreamApi {
  startEventStream()

  function onMetrics(handler: MetricsHandler): () => void {
    metricsHandlers.add(handler)
    const off = (): void => {
      metricsHandlers.delete(handler)
    }
    tryOnScopeDispose(off)
    return off
  }

  function onJob(handler: JobEventHandler): () => void {
    jobHandlers.add(handler)
    const off = (): void => {
      jobHandlers.delete(handler)
    }
    tryOnScopeDispose(off)
    return off
  }

  return { connected: readonly(connected), onMetrics, onJob }
}
