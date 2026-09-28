/** SSE 事件回调：topic 为后端 `event:` 名，data 为解析后的 JSON（解析失败时为 null）。 */
export type SseEventHandler = (topic: string, data: unknown) => void

/** openSse 的可选项。 */
export interface SseOptions {
  /** 监听的命名事件列表（后端所有事件都带 `event:` 名）。 */
  topics?: string[]
  /** 连接建立回调。 */
  onOpen?: () => void
  /**
   * 连接错误回调。openSse 自身不关闭连接（浏览器会按默认策略重连），
   * 需要退避重连或按需关闭的调用方应在回调里调用返回的关闭函数。
   */
  onError?: () => void
}

/** 全局事件流地址（EventSource 不经 axios，需写全 /api/v1 前缀；认证走同源 Cookie）。 */
export const EVENTS_URL = '/api/v1/events'

/** 全局事件流上的命名事件（ready 为首帧握手，metrics/job 为业务事件）。 */
export const GLOBAL_TOPICS = ['ready', 'metrics', 'job'] as const

/** 把 SSE data 字段解析为 JSON；空串或非法 JSON 返回 null。 */
function parseSseData(raw: unknown): unknown {
  if (typeof raw !== 'string' || raw === '') {
    return null
  }
  try {
    return JSON.parse(raw) as unknown
  } catch {
    return null
  }
}

/**
 * 打开一条 SSE 连接（携带同源 Cookie），按命名事件分发；返回关闭函数。
 * EventSource 无法自定义请求头，鉴权完全依赖 HttpOnly 会话 Cookie。
 */
export function openSse(
  url: string,
  onEvent: SseEventHandler,
  options: SseOptions = {},
): () => void {
  const source = new EventSource(url, { withCredentials: true })
  for (const topic of options.topics ?? ['message']) {
    source.addEventListener(topic, (event) => {
      onEvent(topic, parseSseData((event as MessageEvent).data))
    })
  }
  source.onopen = () => {
    options.onOpen?.()
  }
  source.onerror = () => {
    options.onError?.()
  }
  return () => {
    source.close()
  }
}

/**
 * 打开全局事件流 GET /api/v1/events（metrics + job 多路复用），返回关闭函数。
 * 全应用应只保持一条连接——业务代码请使用 @/composables/useEventStream 的单例封装。
 */
export function openEventStream(
  onEvent: SseEventHandler,
  options: Omit<SseOptions, 'topics'> = {},
): () => void {
  return openSse(EVENTS_URL, onEvent, { ...options, topics: [...GLOBAL_TOPICS] })
}
