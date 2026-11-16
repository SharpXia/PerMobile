/**
 * api-client 工厂（H6：全部网络访问收敛于此模块）。
 *
 * 输出两样东西（ARCHITECTURE.md §5.2）：
 * 1. 类型化 REST 方法（listDevices/startSession/...）；
 * 2. 实时通道订阅 openMetricStream（live = EventSource SSE，mock = 前端内置合成流）。
 *
 * 本模块零框架依赖：不 import Vue/任何 UI 库，只依赖浏览器标准 API 与 types.ts。
 * 可替换性：换 UI 框架 = 只重写 views/components，本模块原样带走；换后端实现
 * （FastAPI ↔ stdlib）对 UI 透明（契约不变）。
 */

import type {
  CaseRecord,
  CasesQuery,
  CasesResponse,
  CompareRequest,
  CompareResponse,
  CreateLabelRequest,
  CreateMarkRequest,
  CreateNoteRequest,
  CreateSessionRequest,
  DeepAnalysisResponse,
  Device,
  DeviceAppsResponse,
  DevicesResponse,
  DiagnosticsQuery,
  DiagnosticsResponse,
  Health,
  MarkersPayload,
  MetricsResponse,
  MetricsWindow,
  MetricsWindowQuery,
  PatchMetricsRequest,
  RebuildCasesResponse,
  RenameCaseRequest,
  ReplayResponse,
  RunArtifactsResponse,
  RunDetail,
  RunsResponse,
  SaveResult,
  SaveSessionRequest,
  Session,
  SessionMarker,
  SessionMarkersResponse,
  SessionSummary,
  SimBootResult,
  StartupCaptureRequest,
  StartupCaptureResult,
  StartupTiming,
  Statistics,
  StatisticsQuery,
  TimedScreenshotRequest,
  TimedScreenshotResult,
  WifiModeRequest,
  WifiModeResult,
  XlsxBlob,
} from './types'
import { requestJson, requestBlob, buildUrl, joinUrl } from './http'
import { openMetricStreamSse } from './sse'
import type { MetricStream, MetricStreamHandlers } from './sse'

export interface ApiClientConfig {
  /** 同源托管时为 ''（契约 §1）；纯静态直开后端时可显式配 http://127.0.0.1:8737 */
  baseUrl?: string
}

export interface ApiClient {
  getHealth(): Promise<Health>
  listDevices(): Promise<DevicesResponse>
  getDevice(deviceId: string): Promise<Device>
  listDeviceApps(deviceId: string): Promise<DeviceAppsResponse>
  /** §21.1（M6，R15）：WiFi 模式切换（real-only；synthetic → 501 not_implemented + SSE wifi.unavailable 诊断） */
  setWifiMode(deviceId: string, req: WifiModeRequest): Promise<WifiModeResult>
  /** §23.1（M-iOS，S3）：iOS Simulator 启动（simulator-only；synthetic/real → 501 not_implemented；
   * 503 simctl_unavailable；bootstatus 阻塞上限 ~180s → 超时上限 200s） */
  simBoot(deviceId: string): Promise<SimBootResult>
  listMetrics(deviceId?: string): Promise<MetricsResponse>
  createSession(req: CreateSessionRequest): Promise<Session>
  listSessions(): Promise<Session[]>
  getSession(sessionId: string): Promise<Session>
  startRecording(sessionId: string): Promise<Session>
  stopRecording(sessionId: string): Promise<Session>
  patchSessionMetrics(sessionId: string, patch: PatchMetricsRequest): Promise<Session>
  saveSession(sessionId: string, req?: SaveSessionRequest): Promise<SaveResult>
  discardSession(sessionId: string): Promise<void>
  deleteSession(sessionId: string): Promise<void>
  getMetricsWindow(sessionId: string, q: MetricsWindowQuery): Promise<MetricsWindow>
  getSessionSummary(sessionId: string): Promise<SessionSummary>
  listRuns(): Promise<RunsResponse>
  getRun(runId: string): Promise<RunDetail>
  /** §21.4（M6，R18）：run 启动耗时（TotalTime=am start -W；TTID/TTFD 由 S3 启动捕获回填，未捕获 → null + note） */
  getRunStartup(runId: string): Promise<StartupTiming>
  /** §21.4 S3：run 级启动旁路测量（real-only；synthetic/simulator → 501 not_implemented；
   * 会重新拉起 APP，调用方须在无实时会话采集干扰时执行——契约 §21.4 编排既定） */
  captureRunStartup(runId: string, req: StartupCaptureRequest): Promise<StartupCaptureResult>
  compareReports(req: CompareRequest): Promise<CompareResponse>
  /** §12（M2）+ §18（M5 R30/R31）：run 统计量，perfdog.statistics.v1。fpsGe 缺省走
   * 服务端默认 18,25；fromMs/toMs 可选区间（闭区间含端点，只给 fromMs = 至数据结束） */
  getRunStatistics(runId: string, q?: StatisticsQuery): Promise<Statistics>
  /** §12（M2）+ §18（M5 R30/R31）：会话统计量（monitoring/recording/stopped/saved 缓冲现算，对标实时统计） */
  getSessionStatistics(sessionId: string, q?: StatisticsQuery): Promise<Statistics>
  /** §13.1（M2）：run 导出 xlsx 二进制（UI 经此拿 Blob 触发浏览器下载，不直接 fetch） */
  exportRunXlsx(runId: string): Promise<XlsxBlob>
  /* --- §14 case 库（M3，[I§2.9] 我的数据） --- */
  /** GET /api/cases：筛选（query 子串 / device 精确）+ 排序 + 分页；统计键排序 null 恒排末尾 */
  listCases(q?: CasesQuery): Promise<CasesResponse>
  /** PATCH /api/cases/{runId}：只改显示名，不移动 run 目录，runId 不变 */
  renameCase(runId: string, req: RenameCaseRequest): Promise<CaseRecord>
  /** DELETE /api/cases/{runId}：删事实源（run 目录）+ 索引行 + 同名 .xlsx；204 */
  deleteCase(runId: string): Promise<void>
  /** POST /api/cases/rebuild：按事实源重建索引（幂等；索引缺失/损坏时恢复） */
  rebuildCases(): Promise<RebuildCasesResponse>
  /* --- §22 深度分析与诊断（M7，R38/R50/R51） --- */
  /**
   * §22.1（M7，R50）：GET /api/runs/{runId}/deep-analysis —— 缺省返回帧列表概览
   * （DeepAnalysisOverview），传 frameIndex 返回单帧 Overview 卡 + CPU 调度片段
   * （DeepAnalysisFrameDetail，形态以 isDeepAnalysisFrameDetail 判别）。
   * 无 perfetto trace → 200 空结构 + note（不回退伪造）；frameIndex 越界 → 400。
   */
  getRunDeepAnalysis(runId: string, frameIndex?: number): Promise<DeepAnalysisResponse>
  /** §22.2（M7，R51）：GET /api/runs/{runId}/diagnostics —— 下降区间 + 皮尔逊相关 + 候选原因排序 */
  getRunDiagnostics(runId: string, q?: DiagnosticsQuery): Promise<DiagnosticsResponse>
  /**
   * §22.3（M7，R38）：POST /api/sessions/{sessionId}/timed-screenshot —— 深度分析定时截图
   * （real-only，仅 recording 可启动；synthetic → 501 not_implemented + SSE
   * timed-screenshot.unavailable 诊断；intervalMs 缺省 2000，500–60000）。
   */
  startTimedScreenshot(sessionId: string, req?: TimedScreenshotRequest): Promise<TimedScreenshotResult>
  /* --- §19 回放聚合 + §15/§17 回放支撑端点（M4，R37） --- */
  /** §19：GET /api/runs/{runId}/replay?maxPoints=600 —— series+markers+artifacts+clockAnchors+summary 一次拉取 */
  getRunReplay(runId: string, q?: { maxPoints?: number }): Promise<ReplayResponse>
  /** §15.3：GET /api/runs/{runId}/markers —— sidecar 读回（无 markers.ndjson → markers: []，200） */
  getRunMarkers(runId: string): Promise<MarkersPayload>
  /** §17：GET /api/runs/{runId}/artifacts —— run 目录 artifacts/ 扫描（<1KB 判废件不入索引） */
  listRunArtifacts(runId: string): Promise<RunArtifactsResponse>
  /**
   * §17：GET /api/runs/{runId}/video 的取流 URL（缺省首个 kind=video 工件；分段 run 以
   * name 选择分段）。返回纯 URL 字符串（零网络调用）：交给 <video src> 由浏览器媒体栈
   * 发起 Range 请求（H6 禁网面针对前端代码发起的网络访问，媒体元素取流同源走 Vite
   * proxy/静态托管，与 <img src> 同一豁免逻辑）。URL 不可用时由 UI 层 onError 如实降级。
   */
  getRunVideoUrl(runId: string, name?: string): string
  /** §17：GET /api/runs/{runId}/artifacts/{name} 的下载 URL（截图/数据工件；纯 URL，零网络调用） */
  getRunArtifactUrl(runId: string, name: string): string
  /* --- §15.2 会话标记端点（M5，R28/R29；monitoring|recording|stopped 可调用，否则 409） --- */
  /** §15.2 POST /api/sessions/{id}/labels：场景标签段起点（色带着色，持续到下次 label） */
  createSessionLabel(sessionId: string, req: CreateLabelRequest): Promise<SessionMarker>
  /** §15.2 POST /api/sessions/{id}/notes：批注（双击曲线创建；双击已有批注 = DELETE 取消） */
  createSessionNote(sessionId: string, req: CreateNoteRequest): Promise<SessionMarker>
  /** §15.2 POST /api/sessions/{id}/marks：瞬时标定（红色竖线，单击创建） */
  createSessionMark(sessionId: string, req: CreateMarkRequest): Promise<SessionMarker>
  /** §15.2 GET /api/sessions/{id}/markers：会话内存标记（含 id，tsMs 升序）；任意状态可读 */
  listSessionMarkers(sessionId: string): Promise<SessionMarkersResponse>
  /** §15.2 DELETE /api/sessions/{id}/markers/{markerId}：204 幂等（重复删除/未知 id 同样 204） */
  deleteSessionMarker(sessionId: string, markerId: string): Promise<void>
  openMetricStream(sessionId: string, handlers: MetricStreamHandlers): MetricStream
}

class LiveApiClient implements ApiClient {
  constructor(readonly baseUrl: string) {}

  getHealth(): Promise<Health> {
    return requestJson<Health>(this.baseUrl, { method: 'GET', path: '/api/health' })
  }

  listDevices(): Promise<DevicesResponse> {
    // 实机验证修复：real 模式含设备探测（首次 profile 探测秒级），10s 默认不够
    return requestJson<DevicesResponse>(this.baseUrl, {
      method: 'GET',
      path: '/api/devices',
      timeoutMs: 30_000,
    })
  }

  getDevice(deviceId: string): Promise<Device> {
    return requestJson<Device>(this.baseUrl, {
      method: 'GET',
      path: `/api/devices/${encodeURIComponent(deviceId)}`,
      timeoutMs: 30_000,
    })
  }

  listDeviceApps(deviceId: string): Promise<DeviceAppsResponse> {
    // 实机验证修复：real 模式 pm list + ps 在低端机/多包下秒级起步
    return requestJson<DeviceAppsResponse>(this.baseUrl, {
      method: 'GET',
      path: `/api/devices/${encodeURIComponent(deviceId)}/apps`,
      timeoutMs: 30_000,
    })
  }

  /* --- §21.1 WiFi 模式（M6，R15，real-only） --- */
  setWifiMode(deviceId: string, req: WifiModeRequest): Promise<WifiModeResult> {
    return requestJson<WifiModeResult>(this.baseUrl, {
      method: 'POST',
      path: `/api/devices/${encodeURIComponent(deviceId)}/wifi-mode`,
      body: req,
    })
  }

  /**
   * §23.1（M-iOS，S3 UI 暴露）：iOS Simulator 启动（simulator-only）。
   * `simctl boot` + `bootstatus -b` 阻塞至启动完成（上限 ~180s）→ 超时上限取 200s；
   * 已 Booted 设备幂等返回枚举态。错误走 §3 统一错误体：404 device_not_found、
   * 503 simctl_unavailable（xcrun 缺失/启动超时）、synthetic/real 模式 501
   * not_implemented（附 sim-boot.unavailable 诊断）。
   */
  simBoot(deviceId: string): Promise<SimBootResult> {
    return requestJson<SimBootResult>(this.baseUrl, {
      method: 'POST',
      path: `/api/devices/${encodeURIComponent(deviceId)}/sim-boot`,
      body: {},
      timeoutMs: 200_000,
    })
  }

  listMetrics(deviceId?: string): Promise<MetricsResponse> {
    return requestJson<MetricsResponse>(this.baseUrl, {
      method: 'GET',
      path: '/api/metrics',
      query: { deviceId },
    })
  }

  createSession(req: CreateSessionRequest): Promise<Session> {
    // real + launchOnCreate 时会话创建内联 app_prepare（install/launch 秒级起步）
    return requestJson<Session>(this.baseUrl, {
      method: 'POST',
      path: '/api/sessions',
      body: req,
      timeoutMs: 60_000,
    })
  }

  listSessions(): Promise<Session[]> {
    // 契约 §6：GET /api/sessions 返回列表；兼容包装 { sessions: [...] } 或裸数组
    return requestJson<Session[] | { sessions: Session[] }>(this.baseUrl, {
      method: 'GET',
      path: '/api/sessions',
    }).then((r) => (Array.isArray(r) ? r : r.sessions))
  }

  getSession(sessionId: string): Promise<Session> {
    return requestJson<Session>(this.baseUrl, {
      method: 'GET',
      path: `/api/sessions/${encodeURIComponent(sessionId)}`,
    })
  }

  startRecording(sessionId: string): Promise<Session> {
    return requestJson<Session>(this.baseUrl, {
      method: 'POST',
      path: `/api/sessions/${encodeURIComponent(sessionId)}/record/start`,
      body: {},
    })
  }

  stopRecording(sessionId: string): Promise<Session> {
    return requestJson<Session>(this.baseUrl, {
      method: 'POST',
      path: `/api/sessions/${encodeURIComponent(sessionId)}/record/stop`,
      body: {},
    })
  }

  patchSessionMetrics(sessionId: string, patch: PatchMetricsRequest): Promise<Session> {
    return requestJson<Session>(this.baseUrl, {
      method: 'PATCH',
      path: `/api/sessions/${encodeURIComponent(sessionId)}/metrics`,
      body: patch,
    })
  }

  saveSession(sessionId: string, req?: SaveSessionRequest): Promise<SaveResult> {
    // 契约 §6/§13.2：body 恒为对象；caseName 缺省时后端自动生成 PD_<时间戳>
    const body: SaveSessionRequest = {}
    if (req?.caseName && req.caseName.trim()) body.caseName = req.caseName.trim()
    if (req?.exportExcel !== undefined) body.exportExcel = req.exportExcel
    // real 模式 save 含工件 pull（录屏 mp4 可达百 MB 级）
    return requestJson<SaveResult>(this.baseUrl, {
      method: 'POST',
      path: `/api/sessions/${encodeURIComponent(sessionId)}/save`,
      body,
      timeoutMs: 60_000,
    })
  }

  async discardSession(sessionId: string): Promise<void> {
    await requestJson<void>(this.baseUrl, {
      method: 'POST',
      path: `/api/sessions/${encodeURIComponent(sessionId)}/discard`,
      body: {},
    })
  }

  async deleteSession(sessionId: string): Promise<void> {
    await requestJson<void>(this.baseUrl, {
      method: 'DELETE',
      path: `/api/sessions/${encodeURIComponent(sessionId)}`,
    })
  }

  getMetricsWindow(sessionId: string, q: MetricsWindowQuery): Promise<MetricsWindow> {
    return requestJson<MetricsWindow>(this.baseUrl, {
      method: 'GET',
      path: `/api/sessions/${encodeURIComponent(sessionId)}/metrics`,
      query: {
        metricIds: q.metricIds && q.metricIds.length > 0 ? q.metricIds.join(',') : undefined,
        fromMs: q.fromMs,
        toMs: q.toMs,
        maxPoints: q.maxPoints,
      },
    })
  }

  getSessionSummary(sessionId: string): Promise<SessionSummary> {
    return requestJson<SessionSummary>(this.baseUrl, {
      method: 'GET',
      path: `/api/sessions/${encodeURIComponent(sessionId)}/summary`,
    })
  }

  listRuns(): Promise<RunsResponse> {
    return requestJson<RunsResponse>(this.baseUrl, { method: 'GET', path: '/api/runs' })
  }

  getRun(runId: string): Promise<RunDetail> {
    return requestJson<RunDetail>(this.baseUrl, {
      method: 'GET',
      path: `/api/runs/${encodeURIComponent(runId)}`,
    })
  }

  /* --- §21.4 StartupTiming（M6，R18）+ S3 startup capture（run 级旁路测量，real-only） --- */
  getRunStartup(runId: string): Promise<StartupTiming> {
    return requestJson<StartupTiming>(this.baseUrl, {
      method: 'GET',
      path: `/api/runs/${encodeURIComponent(runId)}/startup`,
    })
  }

  /** §21.4 S3：run 级启动旁路测量（am start -W + logcat Displayed/Fully drawn 行解析，
   * 结果落 run 目录 startup.ndjson；synthetic/simulator → 501 not_implemented）。
   * 会重新拉起 APP —— 调用方须在无实时会话采集干扰时执行（契约 §21.4）。 */
  captureRunStartup(runId: string, req: StartupCaptureRequest): Promise<StartupCaptureResult> {
    return requestJson<StartupCaptureResult>(this.baseUrl, {
      method: 'POST',
      path: `/api/runs/${encodeURIComponent(runId)}/startup/capture`,
      body: req,
      timeoutMs: 60_000,
    })
  }

  /** §9（M3 + S3 扩展）：body 透传——baselineRunIds 数组（引擎跨 run 中位数 +
   * bootstrap）、candidateRunId 旧单数（兼容 mock 形态）、candidateRunIds N 路数组
   * （2–10 组，live 以该字段为准）、budget 逐指标预算、fromMs/toMs 时间区间窗口化。 */
  compareReports(req: CompareRequest): Promise<CompareResponse> {
    return requestJson<CompareResponse>(this.baseUrl, { method: 'POST', path: '/api/reports/compare', body: req })
  }

  getRunStatistics(runId: string, q?: StatisticsQuery): Promise<Statistics> {
    return requestJson<Statistics>(this.baseUrl, {
      method: 'GET',
      path: `/api/runs/${encodeURIComponent(runId)}/statistics`,
      query: {
        fpsGe: formatFpsGe(q?.fpsGe),
        fromMs: q?.fromMs,
        toMs: q?.toMs,
      },
    })
  }

  getSessionStatistics(sessionId: string, q?: StatisticsQuery): Promise<Statistics> {
    return requestJson<Statistics>(this.baseUrl, {
      method: 'GET',
      path: `/api/sessions/${encodeURIComponent(sessionId)}/statistics`,
      query: {
        fpsGe: formatFpsGe(q?.fpsGe),
        fromMs: q?.fromMs,
        toMs: q?.toMs,
      },
    })
  }

  exportRunXlsx(runId: string): Promise<XlsxBlob> {
    // 契约 §13.1：200 二进制 xlsx + Content-Disposition attachment（文件名 = run 目录名.xlsx）
    return requestBlob(
      this.baseUrl,
      { method: 'GET', path: `/api/runs/${encodeURIComponent(runId)}/export.xlsx` },
      `${runId.replace(/^run-/, '')}.xlsx`,
    )
  }

  /* --- §14 case 库（M3） --- */

  listCases(q?: CasesQuery): Promise<CasesResponse> {
    return requestJson<CasesResponse>(this.baseUrl, {
      method: 'GET',
      path: '/api/cases',
      query: {
        query: q?.query,
        device: q?.device,
        sort: q?.sort,
        order: q?.order,
        limit: q?.limit,
        offset: q?.offset,
      },
    })
  }

  renameCase(runId: string, req: RenameCaseRequest): Promise<CaseRecord> {
    return requestJson<CaseRecord>(this.baseUrl, {
      method: 'PATCH',
      path: `/api/cases/${encodeURIComponent(runId)}`,
      body: req,
    })
  }

  async deleteCase(runId: string): Promise<void> {
    // 契约 §14：后端一次删净事实源（run 目录）+ 索引行 + 同名 .xlsx（与前端删除
    // 确认框承诺同口径），无 purge 参数分叉。
    await requestJson<void>(this.baseUrl, {
      method: 'DELETE',
      path: `/api/cases/${encodeURIComponent(runId)}`,
    })
  }

  rebuildCases(): Promise<RebuildCasesResponse> {
    return requestJson<RebuildCasesResponse>(this.baseUrl, { method: 'POST', path: '/api/cases/rebuild', body: {} })
  }

  /* --- §22 深度分析与诊断（M7，R38/R50/R51） --- */

  getRunDeepAnalysis(runId: string, frameIndex?: number): Promise<DeepAnalysisResponse> {
    // 契约 §22.1：缺省=帧列表概览；?frameIndex=N=单帧 Overview 卡（越界 400 validation_error）
    return requestJson<DeepAnalysisResponse>(this.baseUrl, {
      method: 'GET',
      path: `/api/runs/${encodeURIComponent(runId)}/deep-analysis`,
      query: { frameIndex },
    })
  }

  getRunDiagnostics(runId: string, q?: DiagnosticsQuery): Promise<DiagnosticsResponse> {
    // 契约 §22.2：anchor/dropThresholdPct/minDurationMs 均可缺省（frame.fps.mean / 15 / 1000）
    return requestJson<DiagnosticsResponse>(this.baseUrl, {
      method: 'GET',
      path: `/api/runs/${encodeURIComponent(runId)}/diagnostics`,
      query: {
        anchor: q?.anchor,
        dropThresholdPct: q?.dropThresholdPct,
        minDurationMs: q?.minDurationMs,
      },
    })
  }

  startTimedScreenshot(sessionId: string, req?: TimedScreenshotRequest): Promise<TimedScreenshotResult> {
    // 契约 §22.3：202 {sessionId,intervalMs,state:"started"}；synthetic 501 + SSE 诊断；
    // 仅 recording 可启动（其余状态 409 session_state_conflict）；重复 POST 以新 intervalMs 重启
    return requestJson<TimedScreenshotResult>(this.baseUrl, {
      method: 'POST',
      path: `/api/sessions/${encodeURIComponent(sessionId)}/timed-screenshot`,
      body: { intervalMs: req?.intervalMs ?? 2000 },
    })
  }

  /* --- §19 回放聚合 + §15/§17 回放支撑（M4） --- */

  getRunReplay(runId: string, q?: { maxPoints?: number }): Promise<ReplayResponse> {
    // 契约 §19：maxPoints 上限 600 同 §7；触发降采样时顶层 downsampled + sampling
    return requestJson<ReplayResponse>(this.baseUrl, {
      method: 'GET',
      path: `/api/runs/${encodeURIComponent(runId)}/replay`,
      query: { maxPoints: q?.maxPoints },
    })
  }

  getRunMarkers(runId: string): Promise<MarkersPayload> {
    // 契约 §15.3：run 无 markers.ndjson → markers: []（200，无标记是常态不是错误）
    return requestJson<MarkersPayload>(this.baseUrl, {
      method: 'GET',
      path: `/api/runs/${encodeURIComponent(runId)}/markers`,
    })
  }

  listRunArtifacts(runId: string): Promise<RunArtifactsResponse> {
    // 契约 §17：artifacts/ 为空 → artifacts: []
    return requestJson<RunArtifactsResponse>(this.baseUrl, {
      method: 'GET',
      path: `/api/runs/${encodeURIComponent(runId)}/artifacts`,
    })
  }

  getRunVideoUrl(runId: string, name?: string): string {
    // 契约 §17：缺省服务首个 kind=video 工件；分段 run 以 ?name=video-seg002.mp4 选择。
    // 零网络调用 —— 返回的 URL 交给 <video src>，Range/206 由浏览器媒体栈处理。
    return buildUrl(this.baseUrl, `/api/runs/${encodeURIComponent(runId)}/video`, { name })
  }

  getRunArtifactUrl(runId: string, name: string): string {
    // 契约 §17：GET /api/runs/{runId}/artifacts/{name} 二进制原样（截图/数据工件）
    return buildUrl(this.baseUrl, `/api/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(name)}`)
  }

  /* --- §15.2 会话标记端点（M5 R28/R29） --- */

  createSessionLabel(sessionId: string, req: CreateLabelRequest): Promise<SessionMarker> {
    return requestJson<SessionMarker>(this.baseUrl, {
      method: 'POST',
      path: `/api/sessions/${encodeURIComponent(sessionId)}/labels`,
      body: req,
    })
  }

  createSessionNote(sessionId: string, req: CreateNoteRequest): Promise<SessionMarker> {
    return requestJson<SessionMarker>(this.baseUrl, {
      method: 'POST',
      path: `/api/sessions/${encodeURIComponent(sessionId)}/notes`,
      body: req,
    })
  }

  createSessionMark(sessionId: string, req: CreateMarkRequest): Promise<SessionMarker> {
    return requestJson<SessionMarker>(this.baseUrl, {
      method: 'POST',
      path: `/api/sessions/${encodeURIComponent(sessionId)}/marks`,
      body: req,
    })
  }

  listSessionMarkers(sessionId: string): Promise<SessionMarkersResponse> {
    return requestJson<SessionMarkersResponse>(this.baseUrl, {
      method: 'GET',
      path: `/api/sessions/${encodeURIComponent(sessionId)}/markers`,
    })
  }

  async deleteSessionMarker(sessionId: string, markerId: string): Promise<void> {
    // 契约 §15.2：204 幂等 —— 重复删除/未知 markerId 同样 204，无错误分支
    await requestJson<void>(this.baseUrl, {
      method: 'DELETE',
      path: `/api/sessions/${encodeURIComponent(sessionId)}/markers/${encodeURIComponent(markerId)}`,
    })
  }

  openMetricStream(sessionId: string, handlers: MetricStreamHandlers): MetricStream {
    return openMetricStreamSse(this.baseUrl, sessionId, handlers)
  }
}

/** §12 fpsGe：正整数列表 → 逗号分隔查询值；未指定时不传（服务端缺省 18,25） */
function formatFpsGe(fpsGe?: number[]): string | undefined {
  if (!fpsGe || fpsGe.length === 0) return undefined
  return fpsGe.map((n) => Math.trunc(n)).join(',')
}

export function createApiClient(config: ApiClientConfig = {}): ApiClient {
  // 2026-11-12 移除 mock 模式（用户决策：桌面 app 语境下假数据是干扰源）——
  // 永远直连后端；后端不可达由 store.init 的健康检查如实报错，不再静默落假数据
  return new LiveApiClient(config.baseUrl ?? '')
}

/** 供 sse/http 复用的 URL 工具（api-client 内部 API） */
export { joinUrl, buildUrl }
export { ApiError } from './http'
export type { MetricStream, MetricStreamHandlers } from './sse'
export * from './types'
