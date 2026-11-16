/**
 * fetch 封装（api-client 内部，H6 豁免面）：统一错误体解析（契约 §3）与超时。
 * 零框架依赖 —— 只依赖浏览器标准 API 与 types.ts。
 */

import type { ApiErrorCode, ApiErrorBody } from './types'

export class ApiError extends Error {
  readonly code: ApiErrorCode
  readonly status: number
  readonly details: Record<string, unknown> | null

  constructor(code: ApiErrorCode, message: string, status: number, details: Record<string, unknown> | null) {
    super(message || code)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.details = details
  }

  /** UI 层可读文案（错误码 + message） */
  toString(): string {
    return `[${this.code}] ${this.message}`
  }
}

export interface RequestOptions {
  method: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  path: string
  query?: Record<string, string | number | undefined>
  body?: unknown
  timeoutMs?: number
}

export function joinUrl(baseUrl: string, path: string): string {
  const base = baseUrl.replace(/\/+$/, '')
  return `${base}${path}`
}

export function buildUrl(baseUrl: string, path: string, query?: Record<string, string | number | undefined>): string {
  const url = joinUrl(baseUrl, path)
  if (!query) return url
  const params = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined) continue
    params.set(k, String(v))
  }
  const qs = params.toString()
  return qs ? `${url}?${qs}` : url
}

function errorFromResponse(status: number, payload: unknown): ApiError {
  const body = payload as Partial<ApiErrorBody> | null
  if (body && typeof body === 'object' && body.error && typeof body.error === 'object') {
    return new ApiError(
      (body.error.code ?? 'internal_error') as ApiErrorCode,
      body.error.message ?? '',
      status,
      body.error.details ?? null,
    )
  }
  // 非 §3 错误体（如网关/代理返回 HTML）按 internal_error 如实上报，不猜语义
  return new ApiError('internal_error', `HTTP ${status}（响应不是契约错误体）`, status, null)
}

/** 统一请求入口：超时（AbortController）、错误体解析、204 处理 */
export async function requestJson<T>(baseUrl: string, opts: RequestOptions): Promise<T> {
  const url = buildUrl(baseUrl, opts.path, opts.query)
  const controller = new AbortController()
  const timeoutMs = opts.timeoutMs ?? 10_000
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const res = await fetch(url, {
      method: opts.method,
      headers: opts.body === undefined ? undefined : { 'Content-Type': 'application/json; charset=utf-8' },
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      signal: controller.signal,
    })
    if (res.status === 204) return undefined as T
    const text = await res.text()
    let payload: unknown = null
    if (text) {
      try {
        payload = JSON.parse(text)
      } catch {
        payload = null
      }
    }
    if (!res.ok) throw errorFromResponse(res.status, payload)
    return payload as T
  } catch (err) {
    if (err instanceof ApiError) throw err
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new ApiError('timeout', `请求超时（${timeoutMs}ms）：${opts.method} ${opts.path}`, 0, null)
    }
    throw new ApiError('network_error', `网络请求失败：${opts.method} ${opts.path}（${String(err)}）`, 0, null)
  } finally {
    clearTimeout(timer)
  }
}

/** RFC 6266 Content-Disposition 文件名解析：filename*=UTF-8''… 优先，filename= 兜底 */
export function filenameFromDisposition(header: string | null, fallback: string): string {
  if (header) {
    const star = /filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)/.exec(header)
    if (star?.[1]) {
      try {
        return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ''))
      } catch {
        /* percent 编码异常时走 filename= 兜底 */
      }
    }
    const plain = /filename\s*=\s*"([^"]+)"/.exec(header) ?? /filename\s*=\s*([^;]+)/.exec(header)
    if (plain?.[1]) return plain[1].trim().replace(/^"|"$/g, '')
  }
  return fallback
}

/** 二进制请求入口（契约 §13.1 export.xlsx）：错误时响应体仍是 §3 JSON，成功体为 Blob */
export async function requestBlob(
  baseUrl: string,
  opts: RequestOptions,
  fallbackFilename: string,
): Promise<{ blob: Blob; filename: string }> {
  const url = buildUrl(baseUrl, opts.path, opts.query)
  const controller = new AbortController()
  const timeoutMs = opts.timeoutMs ?? 30_000
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const res = await fetch(url, { method: opts.method, signal: controller.signal })
    if (!res.ok) {
      let payload: unknown = null
      try {
        payload = JSON.parse(await res.text())
      } catch {
        payload = null
      }
      throw errorFromResponse(res.status, payload)
    }
    const blob = await res.blob()
    return { blob, filename: filenameFromDisposition(res.headers.get('Content-Disposition'), fallbackFilename) }
  } catch (err) {
    if (err instanceof ApiError) throw err
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new ApiError('timeout', `请求超时（${timeoutMs}ms）：${opts.method} ${opts.path}`, 0, null)
    }
    throw new ApiError('network_error', `网络请求失败：${opts.method} ${opts.path}（${String(err)}）`, 0, null)
  } finally {
    clearTimeout(timer)
  }
}
