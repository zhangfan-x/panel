import type { Problem } from './types'

/**
 * 从任意抛出的错误里提取给用户看的中文消息。
 * 优先用后端 problem 的 detail，其次 title，最后回退到默认文案。
 */
export function problemMessage(error: unknown, fallback = '请求失败，请稍后再试'): string {
  const problem = (error as { problem?: Problem } | null | undefined)?.problem
  if (problem) {
    return problem.detail ?? problem.title ?? fallback
  }
  return fallback
}
