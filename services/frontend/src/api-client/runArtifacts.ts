/**
 * run 侧标注客户端（契约 §15.2 的 run 侧扩展：POST /api/runs/{runId}/labels|notes|marks，
 * report-annotate-frontend 流）。
 *
 * 独立文件而非并入 api-client/index.ts —— index.ts/http.ts/store.ts 归契约/采集流独占
 * 维护（跨流共改禁），本流以新模块最小接入：仅 import http.ts 既有 requestJson 与
 * types.ts 既有类型（CreateLabelRequest/CreateNoteRequest/CreateMarkRequest/ReplayMarker），
 * 对既有模块零修改。mock 模式无后端 run 目录（runs 是 live 后端事实源，契约 §14），
 * 调用方在 run 数据源才启用（ReportView 已按 source.kind 门控）。
 */

import { requestJson } from './http'
import type { CreateLabelRequest, CreateMarkRequest, CreateNoteRequest, ReplayMarker } from './types'

/** run 侧 POST 201 回执元素：落盘同形（无 id —— id 是会话期 DELETE 句柄，§15.1 不落盘） */
export type RunMarkerElement = ReplayMarker

export interface RunArtifactsClient {
  /** POST /api/runs/{runId}/labels：场景标签段起点（run 目录 markers.ndjson 增量追加） */
  createRunLabel(runId: string, req: CreateLabelRequest): Promise<RunMarkerElement>
  /** POST /api/runs/{runId}/notes：批注 */
  createRunNote(runId: string, req: CreateNoteRequest): Promise<RunMarkerElement>
  /** POST /api/runs/{runId}/marks：瞬时标定（红竖线） */
  createRunMark(runId: string, req: CreateMarkRequest): Promise<RunMarkerElement>
}

export function createRunArtifactsClient(baseUrl = ''): RunArtifactsClient {
  return {
    createRunLabel(runId: string, req: CreateLabelRequest): Promise<RunMarkerElement> {
      return requestJson<RunMarkerElement>(baseUrl, {
        method: 'POST',
        path: `/api/runs/${encodeURIComponent(runId)}/labels`,
        body: req,
      })
    },

    createRunNote(runId: string, req: CreateNoteRequest): Promise<RunMarkerElement> {
      return requestJson<RunMarkerElement>(baseUrl, {
        method: 'POST',
        path: `/api/runs/${encodeURIComponent(runId)}/notes`,
        body: req,
      })
    },

    createRunMark(runId: string, req: CreateMarkRequest): Promise<RunMarkerElement> {
      return requestJson<RunMarkerElement>(baseUrl, {
        method: 'POST',
        path: `/api/runs/${encodeURIComponent(runId)}/marks`,
        body: req,
      })
    },
  }
}
