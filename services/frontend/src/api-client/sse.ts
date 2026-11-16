/**
 * EventSource（SSE）封装（api-client 内部，H6 豁免面）。
 *
 * 契约 §8：event 字段 = 消息 type；id = 信封 seq（浏览器 EventSource 断线重连时
 * 自动携带 Last-Event-ID，服务端从 ring buffer 重放）；未知 type 必须忽略。
 */

import type { SseMessage } from './types'
import { joinUrl } from './http'

export interface MetricStreamHandlers {
  /** 分发后的类型化消息（已按契约 §8.3 过滤未知 type） */
  onMessage?: (msg: SseMessage) => void
  onOpen?: () => void
  /** 连接出错；浏览器 EventSource 会自动重连（含 Last-Event-ID），此处仅通知 UI 状态 */
  onError?: (info: { message: string }) => void
}

export interface MetricStream {
  close(): void
}

/** 契约 §8.3 的消息 type 全集；表外 type 一律忽略（兼容规则）。§15.2（M5）新增 marker。 */
const KNOWN_TYPES = [
  'hello',
  'sample',
  'phase',
  'diagnostic',
  'session.state',
  'metrics.config',
  'log',
  'ping',
  'gap',
  'marker',
] as const

export function openMetricStreamSse(
  baseUrl: string,
  sessionId: string,
  handlers: MetricStreamHandlers,
): MetricStream {
  const url = joinUrl(baseUrl, `/api/sessions/${encodeURIComponent(sessionId)}/events`)
  const es = new EventSource(url)

  es.onopen = () => handlers.onOpen?.()
  es.onerror = () => {
    // EventSource 自动重连；readyState===CONNECTING 表示重连中，CLOSED 表示已关闭
    const state = es.readyState === 2 ? '已关闭' : '重连中'
    handlers.onError?.({ message: `SSE 连接中断（${state}），自动重连带 Last-Event-ID` })
  }

  const dispatch = (ev: MessageEvent): void => {
    if (!(KNOWN_TYPES as readonly string[]).includes(ev.type)) return // §8.3 未知 type 忽略
    let payload: unknown = null
    try {
      payload = JSON.parse(ev.data)
    } catch {
      return // 非 JSON data 不分发
    }
    if (payload && typeof payload === 'object') {
      handlers.onMessage?.(payload as SseMessage)
    }
  }
  for (const t of KNOWN_TYPES) {
    es.addEventListener(t, dispatch as EventListener)
  }

  return {
    close() {
      es.close()
    },
  }
}
