import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { stopEventStream, useEventStream } from './useEventStream'
import type { JobEventPayload } from '@/api/jobs'

/** 可编程的 EventSource 假实现：记录实例、允许手动触发 open/error/命名事件。 */
class FakeEventSource {
  static instances: FakeEventSource[] = []

  url: string
  withCredentials: boolean
  closed = false
  onopen: ((ev: Event) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  private listeners = new Map<string, Array<(ev: MessageEvent) => void>>()

  constructor(url: string, init?: EventSourceInit) {
    this.url = url
    this.withCredentials = init?.withCredentials ?? false
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: (ev: MessageEvent) => void): void {
    const list = this.listeners.get(type) ?? []
    list.push(listener)
    this.listeners.set(type, list)
  }

  close(): void {
    this.closed = true
  }

  emitOpen(): void {
    this.onopen?.(new Event('open'))
  }

  emitError(): void {
    this.onerror?.(new Event('error'))
  }

  emit(type: string, data: unknown): void {
    const event = new MessageEvent(type, { data: JSON.stringify(data) })
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event)
    }
  }
}

/** 取最近创建的连接实例。 */
function latest(): FakeEventSource {
  const source = FakeEventSource.instances[FakeEventSource.instances.length - 1]
  if (!source) {
    throw new Error('尚未创建任何 EventSource')
  }
  return source
}

describe('useEventStream', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeEventSource.instances = []
    vi.stubGlobal('EventSource', FakeEventSource)
  })

  afterEach(() => {
    stopEventStream()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('整个应用只建立一条 /api/v1/events 连接（携带 Cookie）', () => {
    useEventStream()
    useEventStream()
    expect(FakeEventSource.instances).toHaveLength(1)
    expect(latest().url).toBe('/api/v1/events')
    expect(latest().withCredentials).toBe(true)
  })

  it('按 topic 分发 metrics 与 job 事件', () => {
    const stream = useEventStream()
    const metrics: unknown[] = []
    const jobEvents: JobEventPayload[] = []
    stream.onMetrics((sample) => metrics.push(sample))
    stream.onJob((event) => jobEvents.push(event))

    latest().emitOpen()
    latest().emit('metrics', { cpu_percent: 12.5 })
    latest().emit('job', {
      job_id: 'j1',
      status: 'running',
      progress: 10,
      stage: null,
      error_code: null,
    })

    expect(metrics).toEqual([{ cpu_percent: 12.5 }])
    expect(jobEvents).toEqual([
      { job_id: 'j1', status: 'running', progress: 10, stage: null, error_code: null },
    ])
  })

  it('退订后不再接收事件', () => {
    const stream = useEventStream()
    const seen: unknown[] = []
    const off = stream.onMetrics((sample) => seen.push(sample))
    latest().emit('metrics', { seq: 1 })
    off()
    latest().emit('metrics', { seq: 2 })
    expect(seen).toEqual([{ seq: 1 }])
  })

  it('连接出错后按指数退避重连，连接成功后退避复位', () => {
    const stream = useEventStream()
    expect(stream.connected.value).toBe(false)
    latest().emitOpen()
    expect(stream.connected.value).toBe(true)

    // 第一次断开：关闭旧连接，1s 后重连。
    latest().emitError()
    expect(stream.connected.value).toBe(false)
    expect(latest().closed).toBe(true)
    expect(FakeEventSource.instances).toHaveLength(1)
    vi.advanceTimersByTime(999)
    expect(FakeEventSource.instances).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(FakeEventSource.instances).toHaveLength(2)

    // 重连未成功再次出错：退避翻倍为 2s。
    latest().emitError()
    vi.advanceTimersByTime(1000)
    expect(FakeEventSource.instances).toHaveLength(2)
    vi.advanceTimersByTime(1000)
    expect(FakeEventSource.instances).toHaveLength(3)

    // 打开成功后退避复位：再次出错只需等 1s。
    latest().emitOpen()
    latest().emitError()
    vi.advanceTimersByTime(1000)
    expect(FakeEventSource.instances).toHaveLength(4)
  })

  it('stopEventStream 关闭连接并停止重连', () => {
    useEventStream()
    latest().emitError()
    stopEventStream()
    vi.advanceTimersByTime(60_000)
    expect(FakeEventSource.instances).toHaveLength(1)
    expect(latest().closed).toBe(true)
  })
})
