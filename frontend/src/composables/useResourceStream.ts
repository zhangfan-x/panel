import { readonly, ref } from 'vue'
import type { Ref } from 'vue'
import { tryOnScopeDispose } from '@vueuse/core'

import { openSse } from '@/api/events'

/** useResourceStream 的配置。 */
export interface ResourceStreamOptions {
  /** 监听的命名事件（对应后端 `event:` 名，如 ['job-log'] / ['journal']）。 */
  topics: string[]
  /** 每收到一条事件时回调（data 为解析后的 JSON，解析失败为 null）。 */
  onEvent: (topic: string, data: unknown) => void
  /** 连接出错时回调（此时流已被关闭；按需流不自动重连，由调用方决定是否重开）。 */
  onError?: () => void
}

/** useResourceStream 返回的控制接口。 */
export interface ResourceStreamApi {
  /** 当前是否有打开的流。 */
  active: Readonly<Ref<boolean>>
  /** 打开指定地址的流（已有流会先关闭）。 */
  open: (url: string) => void
  /** 关闭当前流（幂等）。 */
  close: () => void
}

/**
 * 按需资源流（journal / 容器日志 / 任务日志）：进入资源视图时 open、
 * 离开时 close；在组件 setup 中调用时随组件卸载自动关闭，避免连接泄漏。
 */
export function useResourceStream(options: ResourceStreamOptions): ResourceStreamApi {
  const active = ref(false)
  let closer: (() => void) | null = null

  function close(): void {
    closer?.()
    closer = null
    active.value = false
  }

  function open(url: string): void {
    close()
    active.value = true
    closer = openSse(url, options.onEvent, {
      topics: options.topics,
      onError: () => {
        // 服务端结束（如日志流播完）或网络断开：关闭并通知调用方。
        close()
        options.onError?.()
      },
    })
  }

  tryOnScopeDispose(close)

  return { active: readonly(active), open, close }
}
