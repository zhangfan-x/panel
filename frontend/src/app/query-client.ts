import { QueryClient } from '@tanstack/vue-query'

/** 全局 vue-query 客户端。自用面板：失败少量重试、窗口聚焦不自动刷新。 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 5_000,
    },
  },
})
