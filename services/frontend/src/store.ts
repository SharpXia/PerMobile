/**
 * 监控 store（Vue reactive）。
 *
 * 规则（H6）：本文件与全部 UI 组件只 import src/api-client 的类型化方法，
 * UI 层不直接访问网络 —— 由 `npm run lint:isolation` 门禁兜底。
 */

import { inject, reactive, type InjectionKey } from 'vue'
import type {
  ApiClient,
  AppEntry,
  CaseRecord,
  CaseSortKey,
  CompareBudget,
  CompareMode,
  CompareResponse,
  CreateLabelRequest,
  CreateMarkRequest,
  CreateNoteRequest,
  CreateSessionRequest,
  DeepAnalysisResponse,
  Device,
  DiagnosticsQuery,
  DiagnosticsResponse,
  FpsWindowType,
  Health,
  LogLevel,
  MarkersPayload,
  MetricDef,
  MetricPoint,
  MetricsWindow,
  MetricsWindowQuery,
  RebuildCasesResponse,
  ReplayResponse,
  RunArtifactsResponse,
  RunDetail,
  RunSummary,
  Session,
  SessionMarker,
  SessionSummary,
  SortOrder,
  StartupCaptureRequest,
  StartupCaptureResult,
  StartupTiming,
  SseMessage,
  Statistics,
  StatisticsQuery,
  TimedScreenshotRequest,
  TimedScreenshotResult,
  WifiModeRequest,
} from './api-client'
import { ApiError } from './api-client'
import { DEFAULT_SELECTED_METRICS } from './metricMeta'
import { platformDefaultMetrics } from './capability'
import { defaultThresholds, THRESHOLD_DEFAULTS, type ThresholdState } from './thresholds'

export interface LogEntry {
  ts: number
  /** 'log' = §21.7（M6，R33）设备实时日志流（level 过滤 + crash/ANR 高亮的对象） */
  kind: 'phase' | 'diagnostic' | 'session' | 'system' | 'log'
  level: string
  text: string
  /** §21.7：设备日志原始来源 tag（logcat tag）；非 log 行为 null */
  source: string | null
}

/** 报告页数据源（契约 §12 两端点）：run=已保存记录；session=停止后保存前的统计预览 */
export type ReportSource = { kind: 'run'; runId: string; caseName?: string } | { kind: 'session'; sessionId: string }

/** 回放页数据源（R37，契约 §19 回放聚合只覆盖 run 侧——会话回放无对应端点，不伪装） */
export type ReplaySource = { kind: 'run'; runId: string; caseName?: string }

/** 深度分析页数据源（M7 R50，契约 §22.1 同为 run 侧端点——会话期无 trace 工件，不伪装） */
export type DeepSource = { kind: 'run'; runId: string; caseName?: string }

/** 我的数据页查询条件（契约 §14 GET /api/cases 参数的 UI 形态） */
export interface CaseFilterState {
  /** 用例名子串 */
  text: string
  /** 设备标识精确匹配；'' = 不筛 */
  device: string
  /** 项目（packageName）精确匹配；'' = 不筛（[I§2.9] 项目下拉） */
  packageName: string
  sort: CaseSortKey
  order: SortOrder
  limit: number
  offset: number
}

/** 对比池上限（[I§2.9]「最多支持5条用例同时对比」；契约 §9 M3 扩展：上限由前端执行） */
export const COMPARE_POOL_MAX = 5

/**
 * 设备矩阵会话运行时（M6，R22，契约 §20.4）：每设备一个轻量监控会话（接入即显示，
 * 不落盘），卡片独立开会话/停止；同一设备同时只允许一个活跃会话（device_busy，§20.4）。
 * 记录/保存完整流程仍走主监控区（单会话），矩阵会话是「同时看多台」的第二屏。
 */
export interface MatrixSessionRuntime {
  sessionId: string
  state: Session['state']
  packageName: string
  createdAt: number
  /** metricId（含 `metricId@tag` 形态）→ 最新值（卡片迷你读数） */
  latest: Record<string, number | null>
  samples: number
}

export interface MonitorState {
  baseUrl: string
  health: Health | null
  backendReachable: boolean
  connection: 'idle' | 'connecting' | 'open' | 'error'
  errorMsg: string | null
  devices: Device[]
  selectedDeviceId: string
  apps: AppEntry[]
  selectedApp: string
  metricDefs: MetricDef[]
  /** metricId -> intervalMs；缺项 = 未勾选（勾选同时控制采集与显示，[I§2.4]） */
  selected: Record<string, number>
  /** 图表显示顺序覆盖（displayKey 序列，含 `metricId@tag`；空 = 默认勾选序，UI 反馈 #4 拖拽排序） */
  chartOrder: string[]
  session: Session | null
  recordStartedAt: number
  /** 显示缓冲：metricId -> points（滚动窗口，与后端 5min 缓冲口径一致） */
  series: Record<string, MetricPoint[]>
  latest: Record<string, number | null>
  logs: LogEntry[]
  overlay: boolean
  logPanelOpen: boolean
  /** §15.2 会话期标记（含服务端 id；label/note/mark，tsMs 升序；M5 R28/R29） */
  markers: SessionMarker[]
  /** R31 阈值（前端本地状态，契约 §18 注：无持久化端点，刷新还原默认） */
  thresholds: ThresholdState
  runs: RunSummary[]
  runsLoading: boolean
  /** stopped 后的显式收口决策点（对标 PerfDog 保存对话框 [I§2.6]/[R§R39]） */
  pendingDecision: boolean
  /** 报告页（R41）：非 null 时展示「报告」Tab */
  report: ReportSource | null
  /** 回放页（R37，M4）：非 null 时展示「回放」Tab（主窗口复用，同布局切换数据源 [I§2.5]） */
  replay: ReplaySource | null
  /** 深度分析页（M7 R50，契约 §22.1）：非 null 时展示「深度分析」Tab；入口=报告页/回放页 */
  deep: DeepSource | null
  /** M7：诊断卡区间点击 → 回放定位的目标时刻（epoch ms；ReplayView 消费后即清） */
  replaySeekTs: number | null
  activeTab: 'monitor' | 'records' | 'cases' | 'compare' | 'review' | 'report' | 'replay' | 'deep'
  now: number
  /* --- M3：case 库（R42） --- */
  cases: CaseRecord[]
  casesTotal: number
  casesLoading: boolean
  caseFilter: CaseFilterState
  /* --- M3：对比池（R43） --- */
  comparePool: CaseRecord[]
  /** M3：评审页候选用例（独立于我的数据的筛选态，limit 200 全量窗口） */
  reviewCases: CaseRecord[]
  /* --- M6：设备矩阵（R22）+ 会话扩展选择（R19/R20/R33） --- */
  /** deviceId → 矩阵卡会话运行时（接入即显示，不落盘；同一设备一个活跃会话） */
  matrixSessions: Record<string, MatrixSessionRuntime>
  /** deviceId → WiFi 切换操作的瞬时反馈文案（§21.1；synthetic 显示不可用诊断） */
  wifiStatus: Record<string, string>
  /** deviceId → sim-boot 操作的瞬时反馈文案（§23.1；synthetic/real 显示 not_implemented 诊断） */
  simBootStatus: Record<string, string>
  /** R19（§21.5）：FPS 窗口选择（缺省 auto，创建会话时随请求下发） */
  selectedFpsWindow: FpsWindowType
  /** R20（§21.6）：目标子进程（'' = 主进程缺省） */
  selectedProcessName: string
  /** R33（§21.7）：会话日志级别（null = 不开启日志流，[I§2.1] ☐Log 勾选语义） */
  selectedLogLevel: LogLevel | null
  /* --- 会话接管（刷新恢复）：后端仍存活但本页面未接管的会话 --- */
  /** 后台遗留会话（monitoring|recording|stopped 且未接管）：可接管或停止放弃 */
  orphanSessions: Session[]
  /** 正在接管/停止的会话 id（按钮忙碌态；null = 空闲） */
  recoveryBusyId: string | null
}

export interface MonitorStore {
  state: MonitorState
  init(): Promise<void>
  setError(msg: string): void
  clearError(): void
  selectDevice(deviceId: string): Promise<void>
  selectApp(packageName: string): void
  /** 应用/进程列表手动刷新（下拉展开触发；UI 反馈 #3） */
  refreshApps(): Promise<void>
  /** 图表显示顺序覆盖（拖 ValueCards 触发；UI 反馈 #4：拖小卡片改下方图表顺序） */
  setChartOrder(order: string[]): void
  /* --- M6：设备矩阵（R22）+ WiFi（R15）+ 会话扩展选择（R19/R20/R33）+ Startup（R18） --- */
  /** §20.4：设备卡独立开会话（monitor-only；device_busy 按设备 id 判定） */
  startMatrixSession(deviceId: string): Promise<void>
  /** §20.4：停止并放弃矩阵卡会话（discard；monitor-only 不走保存流程，如实标注） */
  stopMatrixSession(deviceId: string): Promise<void>
  /* --- 会话接管（刷新/换标签页后恢复） --- */
  /** 接管后端既有会话（monitoring|recording|stopped）：回填选择、重连 SSE、校准曲线与标记 */
  adoptSession(sessionId: string): Promise<boolean>
  /** 停止并放弃后端遗留会话（recording 先 stop；saved/discarded 走 delete），不留孤儿采集 */
  closeBackendSession(sessionId: string): Promise<boolean>
  /** 重新扫描后端会话，刷新 orphanSessions（只读，不自动接管） */
  refreshBackendSessions(): Promise<void>
  /** §21.1：WiFi 模式切换（real-only；synthetic 501 + 诊断反馈） */
  setWifiMode(deviceId: string, req: WifiModeRequest): Promise<boolean>
  /** §23.1（S3）：iOS Simulator 启动（simulator-only；bootstatus 上限 ~180s；成功后刷新设备行） */
  simBoot(deviceId: string): Promise<boolean>
  /** R19：FPS 窗口选择（会话未建立时改选；随下一次 createSession 下发） */
  setFpsWindow(type: FpsWindowType): void
  /** R20：目标子进程选择（'' = 主进程缺省） */
  selectProcess(name: string): void
  /** R33：会话日志级别（null = 不开启日志流） */
  setLogLevel(level: LogLevel | null): void
  /** §21.4：run 启动耗时（报告页 R18 启动卡数据源） */
  getRunStartup(runId: string): Promise<StartupTiming>
  toggleMetric(metricId: string, checked: boolean): Promise<void>
  setMetricInterval(metricId: string, intervalMs: number): Promise<void>
  startMonitoring(): Promise<void>
  startRecording(): Promise<void>
  stopRecording(): Promise<void>
  requestStopMonitoring(): void
  reopenDecision(): void
  confirmSave(caseName?: string, exportExcel?: boolean): Promise<void>
  confirmDiscard(): Promise<void>
  keepDecisionPending(): void
  setOverlay(v: boolean): void
  loadRuns(): Promise<void>
  getRunDetail(runId: string): Promise<RunDetail>
  getRunStatistics(runId: string, q?: StatisticsQuery): Promise<Statistics>
  getSessionStatistics(sessionId: string, q?: StatisticsQuery): Promise<Statistics>
  getSessionDetail(sessionId: string): Promise<Session>
  getSessionSummary(sessionId: string): Promise<SessionSummary>
  getMetricsWindow(sessionId: string, q: MetricsWindowQuery): Promise<MetricsWindow>
  /* --- M5：标记（R28/R29，契约 §15.2）+ 阈值（R31，本地状态） --- */
  /** §15.2 POST /labels：场景标签段起点；name 缺省按 labelN 递增分配 */
  addLabel(req: CreateLabelRequest): Promise<SessionMarker | null>
  /** §15.2 POST /notes：批注（双击曲线） */
  addNote(req: CreateNoteRequest): Promise<SessionMarker | null>
  /** §15.2 POST /marks：瞬时标定（单击红竖线） */
  addMark(req: CreateMarkRequest): Promise<SessionMarker | null>
  /** §15.2 DELETE /markers/{id}（204 幂等；双击已生成批注=取消，label/mark 删除是契约自由度） */
  removeMarker(markerId: string): Promise<void>
  /** label 改名（双击色区）：契约无 rename 端点 → DELETE + 同 tsMs 同色重建（§15.2 自由度） */
  renameLabel(markerId: string, name: string): Promise<boolean>
  /** §15.2 GET /markers：全量读回（hello 重连/resync 时校准） */
  refreshMarkers(): Promise<void>
  /** 标记端点可编辑窗口：monitoring | recording | stopped（saved/discarded 409，不再打标） */
  markersEditable(): boolean
  /** R31：设置 FPS>=x 占比阈值（升序去重，正整数） */
  setFpsThresholds(list: number[]): void
  /** R31：设置 FrameTime>=_ ms 阈值 */
  setFtimeThreshold(ms: number): void
  /** R31：一键重置为默认（FPS>=18/25、FTime>=100ms） */
  resetThresholds(): void
  /** §13.1：取 xlsx 并触发浏览器下载（Blob 消费属 UI 层，网络访问仍在 api-client） */
  exportRun(runId: string): Promise<void>
  openRunReport(runId: string, caseName?: string): void
  openSessionReport(): void
  closeReport(): void
  /* --- M4：回放（R37，契约 §19；入口=我的数据行菜单/报告页 [I§2.5]） --- */
  openReplay(runId: string, caseName?: string, seekTsMs?: number): void
  closeReplay(): void
  /** M7：诊断卡区间点击 → 回放定位（ReplayView 数据就绪后取走即清） */
  consumeReplaySeek(): number | null
  /* --- M7：深度分析（R50，契约 §22.1；入口=报告页/回放页 [I§2.7]） --- */
  openDeep(runId: string, caseName?: string): void
  closeDeep(): void
  /** §22.1（M7 R50）：run 深度分析帧列表（缺省）/单帧 Overview 卡（?frameIndex） */
  getRunDeepAnalysis(runId: string, frameIndex?: number): Promise<DeepAnalysisResponse>
  /** §22.2（M7 R51）：根因诊断（下降区间 + 皮尔逊相关 + 候选原因排序） */
  getRunDiagnostics(runId: string, q?: DiagnosticsQuery): Promise<DiagnosticsResponse>
  /** §22.3（M7 R38）：深度分析定时截图（real-only；synthetic 501 + SSE timed-screenshot.unavailable） */
  startTimedScreenshot(sessionId: string, req?: TimedScreenshotRequest): Promise<TimedScreenshotResult>
  /** §19 回放聚合（一次拉取 series/markers/artifacts/clockAnchors/summary） */
  getRunReplay(runId: string, maxPoints?: number): Promise<ReplayResponse>
  /** §15.3 sidecar 读回（replay.markers 段同源；用于单独刷新标记） */
  getRunMarkers(runId: string): Promise<MarkersPayload>
  /** §17 工件索引（视频/截图下载 URL 由 getRunVideoUrl/getRunArtifactUrl 提供） */
  listRunArtifacts(runId: string): Promise<RunArtifactsResponse>
  /** §17 video 取流 URL（缺省首个 kind=video 工件；分段 ?name= 选择）——交给 <video src> */
  getRunVideoUrl(runId: string, name?: string): string
  /** §17 单工件下载 URL（截图/数据工件）——交给 <img src>/下载 */
  getRunArtifactUrl(runId: string, name: string): string
  /* --- M3：case 库 / 对比池 / 评审 --- */
  loadCases(): Promise<void>
  setCaseFilter(patch: Partial<CaseFilterState>): void
  toggleCaseSort(sortKey: CaseSortKey): void
  renameCase(runId: string, caseName: string): Promise<boolean>
  deleteCase(runId: string): Promise<boolean>
  rebuildCases(): Promise<void>
  /** 评审页候选用例（不带我的数据页的筛选态；limit 200 窗口） */
  loadReviewCandidates(): Promise<void>
  addToComparePool(rec: CaseRecord): void
  removeFromComparePool(runId: string): void
  clearComparePool(): void
  inComparePool(runId: string): boolean
  comparePoolFull(): boolean
  runCompareReview(
    baselineRunIds: string[],
    candidateRunId: string,
    mode?: CompareMode,
  ): Promise<CompareResponse>
  /** §9 S3：对比池全量送判定引擎（首条=全局基线，其余各成一组 N 路；budget 可选）。
   * body 同时带旧单数 candidateRunId（mock 形态兼容）与 candidateRunIds（live N 路）。 */
  runComparePool(opts?: { budget?: CompareBudget; fromMs?: number; toMs?: number }): Promise<CompareResponse>
  /** §21.4 S3：run 级启动旁路测量（real-only；synthetic 501；会重新拉起 APP） */
  captureRunStartup(runId: string, req: StartupCaptureRequest): Promise<StartupCaptureResult>
  clearLogs(): void
  sessionDurationSec(): number
  recordDurationSec(): number
}

export const STORE_KEY: InjectionKey<MonitorStore> = Symbol('perflab-monitor-store')

export function useStore(): MonitorStore {
  const store = inject(STORE_KEY)
  if (!store) throw new Error('MonitorStore 未注入（检查 main.ts provide）')
  return store
}

const DISPLAY_WINDOW_MS = 5.5 * 60 * 1000
const MAX_DISPLAY_POINTS = 2400
const MAX_LOGS = 200

export function createMonitorStore(api: ApiClient, baseUrl = ''): MonitorStore {
  const state = reactive<MonitorState>({
    baseUrl,
    health: null,
    backendReachable: false,
    connection: 'idle',
    errorMsg: null,
    devices: [],
    selectedDeviceId: '',
    apps: [],
    selectedApp: '',
    metricDefs: [],
    selected: {},
    chartOrder: [],
    session: null,
    recordStartedAt: 0,
    series: {},
    latest: {},
    logs: [],
    overlay: false,
    logPanelOpen: false,
    markers: [],
    thresholds: defaultThresholds(),
    runs: [],
    runsLoading: false,
    pendingDecision: false,
    report: null,
    replay: null,
    deep: null,
    replaySeekTs: null,
    activeTab: 'monitor',
    now: Date.now(),
    cases: [],
    casesTotal: 0,
    casesLoading: false,
    caseFilter: { text: '', device: '', packageName: '', sort: 'createdAt', order: 'desc', limit: 50, offset: 0 },
    comparePool: [],
    reviewCases: [],
    matrixSessions: {},
    wifiStatus: {},
    simBootStatus: {},
    selectedFpsWindow: 'auto',
    selectedProcessName: '',
    selectedLogLevel: null,
    orphanSessions: [],
    recoveryBusyId: null,
  })

  let stream: { close(): void } | null = null
  /** R22：deviceId → 矩阵卡会话 SSE 流（与主监控流互相独立） */
  const matrixStreams = new Map<string, { close(): void }>()
  let errorTimer: ReturnType<typeof setTimeout> | null = null

  // 时钟：驱动时长显示
  setInterval(() => {
    state.now = Date.now()
  }, 1000)

  // 后台会话轮询（10s，页面可见时）：其他标签页/上次刷新遗留的会话
  // 出现后自动进接管条，不必手动刷新页面才能发现
  setInterval(() => {
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return
    void refreshBackendSessions()
  }, 10_000)

  /* ---------- 基础 ---------- */

  function setError(msg: string): void {
    state.errorMsg = msg
    if (errorTimer) clearTimeout(errorTimer)
    errorTimer = setTimeout(() => {
      state.errorMsg = null
    }, 6000)
  }

  function clearError(): void {
    state.errorMsg = null
  }

  function errText(err: unknown): string {
    if (err instanceof ApiError) return err.toString()
    return String(err)
  }

  function pushLog(kind: LogEntry['kind'], level: string, text: string, source: string | null = null): void {
    state.logs.push({ ts: Date.now(), kind, level, text, source })
    if (state.logs.length > MAX_LOGS) state.logs.splice(0, state.logs.length - MAX_LOGS)
  }

  function pushPoint(metricId: string, ts: number, value: number | null): void {
    const arr = state.series[metricId] ?? (state.series[metricId] = [])
    arr.push({ ts, value })
    const cutoff = state.now - DISPLAY_WINDOW_MS
    while (arr.length > 0 && arr[0]!.ts < cutoff) arr.shift()
    if (arr.length > MAX_DISPLAY_POINTS) arr.splice(0, arr.length - MAX_DISPLAY_POINTS)
  }

  /* ---------- SSE 消息处理（契约 §8.3） ---------- */

  function handleMessage(msg: SseMessage): void {
    switch (msg.type) {
      case 'hello':
        state.connection = 'open'
        pushLog('system', 'info', `实时通道已连接（session ${msg.sessionId}，state=${msg.state}）`)
        void resync()
        break
      case 'sample': {
        // M6 带标签指标（§21.2）：per-core / per-thread 点按 `metricId@tag` 分曲线；
        // 无标签点仍按 metricId 单曲线（baseMetricId() 回查元数据）。
        const tagKey = typeof msg.tags.core === 'string' ? msg.tags.core : typeof msg.tags.thread === 'string' ? msg.tags.thread : null
        const seriesKey = tagKey ? `${msg.metricId}@${tagKey}` : msg.metricId
        pushPoint(seriesKey, msg.ts, msg.value)
        state.latest[seriesKey] = msg.value
        break
      }
      case 'phase':
        pushLog('phase', 'info', `phase ${msg.phase} ${msg.state}${msg.label ? ` · ${msg.label}` : ''}`)
        break
      case 'diagnostic':
        pushLog('diagnostic', msg.severity, `${msg.code}: ${msg.message}`)
        break
      case 'session.state': {
        if (state.session && state.session.state !== msg.state) {
          state.session.state = msg.state
          pushLog('session', 'info', `会话状态 → ${msg.state}`)
          if (msg.state === 'stopped') state.pendingDecision = true
        }
        break
      }
      case 'metrics.config': {
        if (state.session) state.session.metrics = msg.metrics
        reconcileSelection(msg.metrics)
        pushLog('system', 'info', `指标配置更新：${msg.metrics.map((m) => m.metricId).join(', ') || '（空）'}`)
        break
      }
      case 'log':
        // §21.7（M6，R33）：设备实时日志流（kind='log' 供 LogPanel 过滤与 crash/ANR 高亮）
        pushLog('log', msg.level, msg.line, msg.source)
        break
      case 'marker': {
        // §15.2 SSE 广播：多客户端同步色带/批注；remove 时 marker 仅含 id
        const m = msg.marker as SessionMarker
        if (msg.action === 'add') {
          if (!state.markers.some((x) => x.id === m.id)) {
            state.markers = [...state.markers, m].sort((a, b) => a.tsMs - b.tsMs)
          }
        } else if (state.markers.some((x) => x.id === m.id)) {
          state.markers = state.markers.filter((x) => x.id !== m.id)
        }
        break
      }
      case 'gap':
        pushLog('system', 'warn', `检测到重放缺口（${msg.fromSeq}→${msg.toSeq}），全量拉取校准`)
        void resync()
        break
      case 'ping':
      default:
        break // 未知 type / ping 忽略（契约 §8.3 兼容规则）
    }
  }

  /** 全量拉取校准（hello 重连 / gap 后）：以最近 5 分钟窗口覆盖显示缓冲 */
  async function resync(): Promise<void> {
    const s = state.session
    if (!s) return
    try {
      const win = await api.getMetricsWindow(s.id, {
        fromMs: Date.now() - 5 * 60 * 1000,
        maxPoints: 600,
      })
      const next: Record<string, MetricPoint[]> = {}
      for (const series of win.series) next[series.metricId] = series.points
      state.series = next
      await refreshMarkers() // §15.2：重连后标记全量校准（含其他客户端打的标）
    } catch (err) {
      pushLog('system', 'warn', `校准拉取失败（保留当前缓冲）：${errText(err)}`)
    }
  }

  function openStream(sessionId: string): void {
    closeStream()
    state.connection = 'connecting'
    stream = api.openMetricStream(sessionId, {
      onOpen: () => {
        state.connection = 'open'
      },
      onError: (info) => {
        state.connection = state.connection === 'open' ? 'error' : 'connecting'
        pushLog('system', 'warn', info.message)
      },
      onMessage: handleMessage,
    })
  }

  function closeStream(): void {
    if (stream) {
      stream.close()
      stream = null
    }
    state.connection = 'idle'
  }


  /* ---------- 会话接管（页面刷新/换标签页后恢复，契约 §6.2） ----------
   *
   * 背景：会话是后端内存态（monitoring/recording 继续采集），而页面刷新会丢掉
   * 前端 store —— 旧行为下刷新后既看不到正在跑的会话，也无法停止它，只能等后端
   * 重启（「后台会话变孤儿」）。本段做两件事：
   *   1. 刷新后自动接管本页创建的会话（localStorage 记 id）并校准曲线/标记；
   *   2. 把未接管的存活会话列进 orphanSessions，供用户显式接管或停止放弃。
   * 数据面不新造：曲线走既有 GET /sessions/{id}/metrics（5min 有界缓冲），
   * 标记走既有 GET /sessions/{id}/markers，时长走 detail 的 recordStartedAt。
   */

  const SESSION_STORAGE_KEY = 'pd.sessions.v1'

  interface PersistedSessions {
    main: string | null
    matrix: Record<string, string>
  }

  function readPersistedSessions(): PersistedSessions {
    try {
      const raw = window.localStorage.getItem(SESSION_STORAGE_KEY)
      if (!raw) return { main: null, matrix: {} }
      const parsed = JSON.parse(raw) as { main?: unknown; matrix?: unknown }
      const matrix: Record<string, string> = {}
      if (parsed.matrix && typeof parsed.matrix === 'object') {
        for (const [deviceId, id] of Object.entries(parsed.matrix as Record<string, unknown>)) {
          if (typeof id === 'string' && id) matrix[deviceId] = id
        }
      }
      return { main: typeof parsed.main === 'string' && parsed.main ? parsed.main : null, matrix }
    } catch {
      return { main: null, matrix: {} } // localStorage 不可用/损坏：退化为不跨刷新
    }
  }

  function persistSessions(): void {
    try {
      const matrix: Record<string, string> = {}
      for (const [deviceId, rt] of Object.entries(state.matrixSessions)) matrix[deviceId] = rt.sessionId
      window.localStorage.setItem(
        SESSION_STORAGE_KEY,
        JSON.stringify({ main: state.session?.id ?? null, matrix }),
      )
    } catch {
      // 配额/隐私模式：接管仍可用（靠后端列表兜底），不因持久化失败打断主流程
    }
  }

  function isLiveSession(s: Session): boolean {
    return s.state === 'monitoring' || s.state === 'recording' || s.state === 'stopped'
  }

  /** 矩阵卡会话 SSE：sample 更新卡片读数，session.state 同步卡片状态 */
  function openMatrixStream(deviceId: string, sessionId: string): void {
    matrixStreams.get(deviceId)?.close()
    matrixStreams.set(
      deviceId,
      api.openMetricStream(sessionId, {
        onOpen: () => {
          /* hello 到达前保持 connecting 语义由卡片 state 表达 */
        },
        onError: (info) => pushLog('system', 'warn', `[${deviceId}] ${info.message}`),
        onMessage: (msg) => {
          const rt = state.matrixSessions[deviceId]
          if (!rt || msg.sessionId !== sessionId) return
          if (msg.type === 'sample') {
            const tagKey =
              typeof msg.tags.core === 'string' ? msg.tags.core : typeof msg.tags.thread === 'string' ? msg.tags.thread : null
            rt.latest[tagKey ? `${msg.metricId}@${tagKey}` : msg.metricId] = msg.value
            rt.samples += 1
          } else if (msg.type === 'session.state') {
            rt.state = msg.state
            if (msg.state === 'stopped') void stopMatrixSession(deviceId)
          } else if (msg.type === 'diagnostic') {
            pushLog('diagnostic', msg.severity, `[${deviceId}] ${msg.code}: ${msg.message}`)
          }
        },
      }),
    )
  }

  /** 恢复矩阵卡运行时（刷新后重建卡片 + 重连 SSE；曲线读数由后续 sample 补齐） */
  function attachMatrixSession(deviceId: string, s: Session): void {
    state.matrixSessions[deviceId] = {
      sessionId: s.id,
      state: s.state,
      packageName: s.packageName,
      createdAt: s.createdAt,
      latest: {},
      samples: 0,
    }
    openMatrixStream(deviceId, s.id)
  }

  /** 接管为主监控会话：回填设备/应用/指标选择 + 重连 SSE（hello 触发全量校准） */
  async function attachMainSession(s: Session): Promise<void> {
    state.session = s
    state.recordStartedAt = s.recordStartedAt ?? 0
    state.selectedFpsWindow = s.fpsWindow?.type ?? 'auto'
    state.selectedProcessName = s.processName ?? ''
    state.selectedLogLevel = s.logLevel ?? null
    // 会话进行中不能走 selectDevice（它会重置应用与勾选），直接回填选择
    state.selectedDeviceId = s.deviceId
    state.selectedApp = s.packageName
    try {
      const res = await api.listDeviceApps(s.deviceId)
      state.apps = res.apps
    } catch (err) {
      // 设备离线/应用列表失败：会话本身仍可查看与收口，只记日志不中断接管
      pushLog('system', 'warn', `接管时会话设备应用列表拉取失败：${errText(err)}`)
    }
    await refreshMetrics(s.deviceId)
    reconcileSelection(s.metrics)
    state.series = {}
    state.latest = {}
    state.markers = []
    state.pendingDecision = s.state === 'stopped' // stopped：刷新前没走完收口决策
    openStream(s.id)
    pushLog(
      'system',
      'info',
      `已接管后台会话 ${s.id}（${s.state}，设备 ${s.deviceId}）：曲线与标记校准中`,
    )
    persistSessions()
  }

  /** 刷新恢复：先按 localStorage 认领，再兜底「唯一活跃会话」，其余进 orphanSessions */
  async function restoreSessions(): Promise<void> {
    let sessions: Session[]
    try {
      sessions = await api.listSessions()
    } catch (err) {
      // 后端不可达：不制造假状态（健康检查已给出错误提示）
      pushLog('system', 'warn', `会话接管跳过（后端不可达）：${errText(err)}`)
      return
    }
    const live = sessions.filter(isLiveSession)
    const persisted = readPersistedSessions()

    // 主会话：localStorage 认领优先；无记录时若只有一个活跃会话则视为「刷新前的那个」
    // （但它若本就是矩阵卡会话，按矩阵卡还原，不抢成主会话）
    const matrixIds = new Set(Object.values(persisted.matrix))
    const fallback = live.length === 1 && !matrixIds.has(live[0]!.id) ? live[0]! : null
    const main = state.session ?? live.find((s) => s.id === persisted.main) ?? fallback
    if (main && !state.session) {
      await attachMainSession(main)
    }

    // 矩阵卡：localStorage 记录 + 同一设备不重复；主会话所在设备已被占用
    for (const [deviceId, sessionId] of Object.entries(persisted.matrix)) {
      if (state.matrixSessions[deviceId]) continue
      if (state.session?.deviceId === deviceId) continue
      const s = live.find((x) => x.id === sessionId)
      if (s) attachMatrixSession(deviceId, s)
    }

    // 认领结果回写（含失效 id 的清理），保证下一次刷新仍能还原矩阵卡
    persistSessions()
    await refreshBackendSessions()
  }

  /** 扫描后端会话：未接管的存活会话进 orphanSessions（只读，不改后端状态） */
  async function refreshBackendSessions(): Promise<void> {
    try {
      const sessions = await api.listSessions()
      const attached = new Set<string>()
      if (state.session) attached.add(state.session.id)
      for (const rt of Object.values(state.matrixSessions)) attached.add(rt.sessionId)
      state.orphanSessions = sessions.filter((s) => isLiveSession(s) && !attached.has(s.id))
    } catch {
      // 保留旧列表：清空会让用户误以为孤儿会话已消失
    }
  }

  /** 接管指定会话（用户在遗留会话条上点「接管」） */
  async function adoptSession(sessionId: string): Promise<boolean> {
    if (state.session) {
      setError('已有接管中的会话；请先停止并收口，再接管其他会话')
      return false
    }
    state.recoveryBusyId = sessionId
    try {
      const s = await api.getSession(sessionId)
      if (!isLiveSession(s)) {
        setError(`会话 ${sessionId} 已收口（${s.state}），无可接管内容`)
        await refreshBackendSessions()
        return false
      }
      await attachMainSession(s)
      await refreshBackendSessions()
      return true
    } catch (err) {
      setError(errText(err))
      return false
    } finally {
      state.recoveryBusyId = null
    }
  }

  /**
   * 停止并放弃遗留会话（收口后端采集，避免孤儿进程）：
   * recording → record/stop → discard；monitoring/stopped → discard（stopped 会连同
   * staging run 目录一并删除，需保存请改用「接管」）；saved/discarded → delete。
   */
  async function closeBackendSession(sessionId: string): Promise<boolean> {
    state.recoveryBusyId = sessionId
    try {
      let s = await api.getSession(sessionId)
      if (s.state === 'recording') s = await api.stopRecording(sessionId)
      if (s.state === 'monitoring' || s.state === 'stopped') await api.discardSession(sessionId)
      else if (s.state === 'saved' || s.state === 'discarded') await api.deleteSession(sessionId)
      // 本页面正接管/挂着的就是它 → 同步清理本地状态与 SSE
      if (state.session?.id === sessionId) {
        teardownSession()
        applyDefaultSelection() // 与 confirmDiscard 同收口口径
      }
      for (const [deviceId, rt] of Object.entries(state.matrixSessions)) {
        if (rt.sessionId !== sessionId) continue
        matrixStreams.get(deviceId)?.close()
        matrixStreams.delete(deviceId)
        delete state.matrixSessions[deviceId]
      }
      pushLog('session', 'info', `已停止并放弃后台会话 ${sessionId}（采集已收口）`)
      await refreshBackendSessions()
      return true
    } catch (err) {
      setError(errText(err))
      return false
    } finally {
      state.recoveryBusyId = null
    }
  }

  /* ---------- 初始化 ---------- */

  // 指标面板勾选持久化（2026-11-12 桌面验收反馈：手动刷新不应重置已选指标，
  // 只应刷新状态）。存 localStorage，init 恢复；恢复先于 applyDefaultSelection——
  // 用户已选条目经 undefined 检查保留，未覆盖的默认项照常补齐。
  const SELECTION_STORAGE_KEY = 'pd.selected.metrics.v1'
  // init 恢复完成前禁止覆盖写：refreshDevices→selectDevice 内部的 refreshMetrics
  // 会带空 selected 走到 persist，把 localStorage 先清成 {}，restore 读到的即空
  // （勾选「刷新丢失」回归实录，2026-11-12）
  let selectionRestored = false

  function persistSelection(): void {
    if (!selectionRestored) return
    try {
      localStorage.setItem(SELECTION_STORAGE_KEY, JSON.stringify(state.selected))
    } catch {
      /* 存储不可用（隐私模式等）静默降级：选择仅存内存 */
    }
  }

  function restoreSelection(): void {
    try {
      const raw = localStorage.getItem(SELECTION_STORAGE_KEY)
      if (raw) {
        const parsed: unknown = JSON.parse(raw)
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
          for (const [id, interval] of Object.entries(parsed as Record<string, unknown>)) {
            if (typeof interval === 'number' && interval > 0) state.selected[id] = interval
          }
        }
      }
    } catch {
      /* 坏数据静默丢弃，走默认勾选 */
    }
    selectionRestored = true
  }

  async function init(): Promise<void> {
    try {
      state.health = await api.getHealth()
      state.backendReachable = true
    } catch (err) {
      state.backendReachable = false
      setError(`后端不可达：${errText(err)}。请先启动后端（python services/backend/run.py --port 8737）。`)
    }
    await refreshDevices()
    // 恢复先于 refreshMetrics：后者按 defs 可用性剔除勾选并 persist——若恢复在其后，
    // 启动期空 selected 会先把 localStorage 覆盖清空，恢复读到的即是空（回归实录）
    restoreSelection()
    await refreshMetrics()
    applyDefaultSelection()
    await loadRuns()
    // 页面刷新/换标签页后接管后端仍存活的会话（数据与状态一并恢复）
    await restoreSessions()
  }

  async function refreshDevices(): Promise<void> {
    try {
      const res = await api.listDevices()
      state.devices = res.devices
      const firstOnline = res.devices.find((d) => d.state === 'online') ?? res.devices[0]
      if (firstOnline && !res.devices.some((d) => d.id === state.selectedDeviceId)) {
        await selectDevice(firstOnline.id)
      }
    } catch (err) {
      setError(errText(err))
    }
  }

  async function refreshMetrics(deviceId?: string): Promise<void> {
    try {
      const res = await api.listMetrics(deviceId ?? (state.selectedDeviceId || undefined))
      state.metricDefs = res.metrics
      // 剔除不再可用的勾选
      for (const id of Object.keys(state.selected)) {
        const def = state.metricDefs.find((m) => m.metricId === id)
        if (!def || !def.available) delete state.selected[id]
      }
      persistSelection()
    } catch (err) {
      setError(errText(err))
    }
  }

  function applyDefaultSelection(): void {
    for (const id of DEFAULT_SELECTED_METRICS) {
      const def = state.metricDefs.find((m) => m.metricId === id)
      if (def && def.available && state.selected[id] === undefined) {
        state.selected[id] = def.defaultIntervalMs ?? 1000
      }
    }
    persistSelection()
  }

  /* ---------- 设备 / 应用 / 指标 ---------- */

  async function selectDevice(deviceId: string): Promise<void> {
    if (state.session && (state.session.state === 'monitoring' || state.session.state === 'recording')) {
      setError('会话进行中，不能切换设备（先停止并收口）')
      return
    }
    state.selectedDeviceId = deviceId
    state.selectedApp = ''
    state.selectedProcessName = '' // 换设备 → 子进程选择失效（§21.6 校验按 app 归属）
    try {
      const res = await api.listDeviceApps(deviceId)
      state.apps = res.apps
      const first = res.apps[0]
      state.selectedApp = first?.packageName ?? ''
    } catch (err) {
      state.apps = []
      setError(errText(err))
    }
    await refreshMetrics(deviceId)
  }

  function selectApp(packageName: string): void {
    state.selectedApp = packageName
    state.selectedProcessName = '' // 换应用 → 子进程选择失效（§21.6 校验按 app 归属）
  }

  /** 应用/进程列表刷新（UI 反馈 #3：下拉展开即拉最新进程态，不依赖换设备触发） */
  async function refreshApps(): Promise<void> {
    const deviceId = state.selectedDeviceId
    if (!deviceId) return
    try {
      const res = await api.listDeviceApps(deviceId)
      state.apps = res.apps
      // 已选应用不在新列表（卸载/进程消失）→ 回落首应用；在则保留用户选择
      if (state.selectedApp && !res.apps.some((a) => a.packageName === state.selectedApp)) {
        state.selectedApp = res.apps[0]?.packageName ?? ''
        state.selectedProcessName = ''
      }
    } catch (err) {
      setError(errText(err))
    }
  }

  /** 图表显示顺序覆盖（UI 反馈 #4：拖 ValueCards 小卡片 → 联动下方图表面板顺序）。
   *  只存 displayKey 序列；未知键（指标取消勾选）排序时按「缺省序在后」兜底。 */
  function setChartOrder(order: string[]): void {
    state.chartOrder = [...order]
  }

  /* ---------- M6：设备矩阵会话（R22，§20.4）/ WiFi（R15，§21.1）/ 会话扩展选择 ---------- */

  /** 矩阵卡会话（monitor-only）：默认采集集按设备平台/连接分流（S3）+ 该设备第一个应用，接入即显示 */
  async function startMatrixSession(deviceId: string): Promise<void> {
    const cur = state.matrixSessions[deviceId]
    if (cur && (cur.state === 'monitoring' || cur.state === 'recording')) {
      setError(`设备 ${deviceId} 已有活跃会话（同一设备同时只允许一个，§20.4 device_busy）`)
      return
    }
    // S3 缺省采集集平台分流（§20.3 落点兜底）：Android→FPS/CPU/MEM 三指标；
    // iOS 模拟器→宿主口径双指标（§23.2）；iOS 真机→xctrace 口径双指标（§26）。
    // 否则 iOS 设备携带 platform_unsupported 指标建会话会被 metric_unavailable 拒绝。
    const device = state.devices.find((d) => d.id === deviceId)
    const defaultMetrics = platformDefaultMetrics(device?.platform ?? '', device?.connection)
    // 目标应用：主区已选同设备应用则复用；否则取该设备应用列表第一个
    let packageName = state.selectedDeviceId === deviceId ? state.selectedApp : ''
    if (!packageName) {
      try {
        const res = await api.listDeviceApps(deviceId)
        packageName = res.apps[0]?.packageName ?? ''
      } catch (err) {
        setError(errText(err))
        return
      }
    }
    if (!packageName) {
      setError(`设备 ${deviceId} 无可用应用，不能开会话`)
      return
    }
    try {
      const s = await api.createSession({
        deviceId,
        packageName,
        metrics: defaultMetrics.map((metricId) => ({ metricId })),
        interactive: false,
        launchOnCreate: false,
      })
      // 每设备一条 SSE 流（与刷新接管共用 openMatrixStream，口径一致）
      attachMatrixSession(deviceId, { ...s, packageName })
      persistSessions()
      pushLog('system', 'info', `矩阵会话 ${s.id} 已建立（设备 ${deviceId} · monitor-only 不落盘）`)
    } catch (err) {
      setError(errText(err))
    }
  }

  /** 停止矩阵卡会话：discard（monitor-only 无落盘数据可保存，如实走放弃路径） */
  async function stopMatrixSession(deviceId: string): Promise<void> {
    const rt = state.matrixSessions[deviceId]
    if (!rt) return
    matrixStreams.get(deviceId)?.close()
    matrixStreams.delete(deviceId)
    try {
      if (rt.state === 'monitoring' || rt.state === 'recording') {
        await api.discardSession(rt.sessionId)
      } else {
        await api.deleteSession(rt.sessionId)
      }
    } catch (err) {
      setError(errText(err))
    }
    delete state.matrixSessions[deviceId]
    persistSessions()
  }

  /** §21.1 WiFi 模式切换：real-only；synthetic 501 not_implemented（诊断反馈 + 状态行） */
  async function setWifiMode(deviceId: string, req: WifiModeRequest): Promise<boolean> {
    state.wifiStatus[deviceId] = req.action === 'enable' ? '切换中（adb tcpip 5555）…' : `连接中（${req.host ?? ''}:5555）…`
    try {
      const res = await api.setWifiMode(deviceId, req)
      state.wifiStatus[deviceId] =
        res.action === 'enable'
          ? `已启用 TCP 模式（port ${res.port ?? 5555}），请用设备 IP 连接`
          : `已连接 ${res.host}:${res.port ?? 5555}（WiFi 行已入列表）`
      pushLog('system', 'info', `WiFi 模式：${state.wifiStatus[deviceId]}`)
      await refreshDevices() // 连接成功 → 设备列表出现 WiFi 行（§21.1 connection 字段）
      return true
    } catch (err) {
      const apiErr = err instanceof ApiError ? err : null
      // synthetic → 501：诊断文案对齐 §21.1（合成模式无 WiFi 通道）
      state.wifiStatus[deviceId] =
        apiErr?.code === 'not_implemented'
          ? '不可用：合成模式无 WiFi 通道（real-only 端点，契约 §21.1）'
          : `失败：${errText(err)}`
      pushLog('diagnostic', 'warn', `[${deviceId}] wifi.unavailable / ${state.wifiStatus[deviceId]}`)
      setError(state.wifiStatus[deviceId] ?? '')
      return false
    }
  }

  /**
   * §23.1 sim-boot（S3 UI 暴露面）：启动 iOS Simulator（simulator-only）。
   * 错误统一走 §3 错误体（404 device_not_found / 503 simctl_unavailable /
   * synthetic·real 501 not_implemented——对齐 setWifiMode 的诊断反馈先例）；
   * 成功后刷新设备列表，offline 设备行转 online。
   */
  async function simBoot(deviceId: string): Promise<boolean> {
    state.simBootStatus[deviceId] = '启动模拟器中（simctl boot + bootstatus，最长 ~180s）…'
    try {
      const res = await api.simBoot(deviceId)
      state.simBootStatus[deviceId] =
        res.state === 'online' ? '模拟器已启动（在线）' : `模拟器状态：${res.state}（未转在线，见后端诊断）`
      pushLog('system', 'info', `sim-boot：${state.simBootStatus[deviceId]}`)
      await refreshDevices() // 启动成功 → 设备行 offline→online（§23.1 state 映射）
      return true
    } catch (err) {
      const apiErr = err instanceof ApiError ? err : null
      state.simBootStatus[deviceId] =
        apiErr?.code === 'not_implemented'
          ? '不可用：sim-boot 为 simulator-only 能力（当前模式未实现，§23.1）'
          : `失败：${errText(err)}`
      pushLog('diagnostic', 'warn', `[${deviceId}] sim-boot.unavailable / ${state.simBootStatus[deviceId]}`)
      setError(state.simBootStatus[deviceId] ?? '')
      return false
    }
  }

  function setFpsWindow(type: FpsWindowType): void {
    state.selectedFpsWindow = type
  }

  function selectProcess(name: string): void {
    state.selectedProcessName = name
  }

  function setLogLevel(level: LogLevel | null): void {
    state.selectedLogLevel = level
  }

  function reconcileSelection(list: { metricId: string; intervalMs?: number }[]): void {
    const next: Record<string, number> = {}
    for (const m of list) {
      const def = state.metricDefs.find((d) => d.metricId === m.metricId)
      next[m.metricId] = m.intervalMs ?? def?.defaultIntervalMs ?? 1000
    }
    state.selected = next
    persistSelection()
  }

  async function toggleMetric(metricId: string, checked: boolean): Promise<void> {
    const def = state.metricDefs.find((m) => m.metricId === metricId)
    if (!def) return
    if (!def.available) {
      setError(`指标 ${metricId} 当前不可用（available=false，不写 0）`)
      return
    }
    const active = state.session && (state.session.state === 'monitoring' || state.session.state === 'recording')
    try {
      if (active && state.session) {
        // 运行中：勾选 → PATCH 启停（采集+显示一体，契约 §6）
        const patch = checked
          ? { enable: [{ metricId, intervalMs: state.selected[metricId] ?? def.defaultIntervalMs ?? 1000 }] }
          : { disable: [metricId] }
        const updated = await api.patchSessionMetrics(state.session.id, patch)
        state.session = updated
        reconcileSelection(updated.metrics)
      } else {
        if (checked) {
          if (state.selected[metricId] === undefined) {
            state.selected[metricId] = def.defaultIntervalMs ?? 1000
          }
        } else {
          delete state.selected[metricId]
        }
        persistSelection()
      }
    } catch (err) {
      setError(errText(err))
    }
  }

  async function setMetricInterval(metricId: string, intervalMs: number): Promise<void> {
    const active = state.session && (state.session.state === 'monitoring' || state.session.state === 'recording')
    if (active && state.session) {
      try {
        const updated = await api.patchSessionMetrics(state.session.id, { enable: [{ metricId, intervalMs }] })
        state.session = updated
        reconcileSelection(updated.metrics)
      } catch (err) {
        setError(errText(err))
      }
    } else {
      state.selected[metricId] = intervalMs
      persistSelection()
    }
  }

  /* ---------- 会话动作（状态机：monitoring→recording→stopped→saved|discarded） ---------- */

  async function startMonitoring(): Promise<void> {
    if (state.session && (state.session.state === 'monitoring' || state.session.state === 'recording')) return
    if (!state.selectedDeviceId || !state.selectedApp) {
      setError('请先选择设备与测试应用')
      return
    }
    const metrics = Object.entries(state.selected).map(([metricId, intervalMs]) => ({ metricId, intervalMs }))
    // M6 会话扩展（§21.5/§21.6/§21.7）：fpsWindow 恒带（缺省 auto）；processName/logLevel
    // 仅在用户选择时下发（logLevel 缺省不发 log 消息，[I§2.1] ☐Log 勾选语义）
    const req: CreateSessionRequest = {
      deviceId: state.selectedDeviceId,
      packageName: state.selectedApp,
      metrics,
      interactive: false,
      launchOnCreate: false,
      fpsWindow: { type: state.selectedFpsWindow },
    }
    if (state.selectedProcessName) req.processName = state.selectedProcessName
    if (state.selectedLogLevel) req.logLevel = state.selectedLogLevel
    try {
      const s = await api.createSession(req)
      state.session = s
      state.recordStartedAt = 0
      state.series = {}
      state.latest = {}
      openStream(s.id)
      persistSessions() // 刷新后可凭 id 重新接管
      pushLog('system', 'info', `会话 ${s.id} 已建立（monitoring：接入即实时显示，不落盘）`)
    } catch (err) {
      setError(errText(err))
    }
  }

  async function startRecording(): Promise<void> {
    const s = state.session
    if (!s || s.state !== 'monitoring') return
    try {
      const updated = await api.startRecording(s.id)
      state.session = updated
      state.recordStartedAt = Date.now()
      pushLog('session', 'info', '开始记录（落盘开始）')
    } catch (err) {
      setError(errText(err))
    }
  }

  async function stopRecording(): Promise<void> {
    const s = state.session
    if (!s || s.state !== 'recording') return
    try {
      const updated = await api.stopRecording(s.id)
      state.session = updated
      if (updated.state === 'stopped') state.pendingDecision = true
      pushLog('session', 'info', '停止记录（进入收口决策点）')
    } catch (err) {
      setError(errText(err))
    }
  }

  /** 停止监控（monitoring 态）：进入保存/放弃决策（契约 §6.1：monitoring 可直接收口） */
  function requestStopMonitoring(): void {
    state.pendingDecision = true
  }

  function reopenDecision(): void {
    state.pendingDecision = true
  }

  function keepDecisionPending(): void {
    state.pendingDecision = false
  }

  function teardownSession(): void {
    closeStream()
    state.session = null
    state.recordStartedAt = 0
    state.series = {}
    state.latest = {}
    state.markers = []
    state.pendingDecision = false
    persistSessions() // 收口后清掉持久化 id，避免下次刷新去认领已结束的会话
  }

  async function confirmSave(caseName?: string, exportExcel?: boolean): Promise<void> {
    const s = state.session
    if (!s) return
    try {
      // 契约 §13.2 [I§2.6]：Save(excel) 复选 → 请求可选字段 exportExcel
      const res = await api.saveSession(s.id, { caseName, exportExcel: exportExcel === true })
      pushLog('system', 'info', `已保存：${res.runId} → ${res.runPath}`)
      if (res.exportError) {
        // §13.2：导出失败不使 save 失败（run 已落盘），xlsx 可经 §13.1 重试
        pushLog('system', 'warn', `xlsx 导出失败（save 本身成功，可从报告页重试）：${res.exportError}`)
      }
      teardownSession()
      applyDefaultSelection()
      await loadRuns()
      // save 成功即自动登记 case 索引（契约 §14 注）；M3 页面打开中则同步刷新
      if (['cases', 'compare', 'review'].includes(state.activeTab)) await loadCases()
      state.activeTab = 'records'
      if (exportExcel === true) {
        // §13.2：勾选即随保存下载同名 .xlsx（服务端生成 + §13.1 下载同源）
        await exportRun(res.runId)
      }
    } catch (err) {
      setError(errText(err))
    }
  }

  async function confirmDiscard(): Promise<void> {
    const s = state.session
    if (!s) return
    try {
      await api.discardSession(s.id)
      pushLog('session', 'info', '已放弃本次数据')
      teardownSession()
      applyDefaultSelection()
    } catch (err) {
      setError(errText(err))
    }
  }

  /* ---------- 记录列表 ---------- */

  async function loadRuns(): Promise<void> {
    state.runsLoading = true
    try {
      const res = await api.listRuns()
      state.runs = res.runs
    } catch (err) {
      setError(errText(err))
    } finally {
      state.runsLoading = false
    }
  }

  /* ---------- M5：标记（R28/R29，契约 §15.2）+ 阈值（R31） ---------- */

  function markersEditable(): boolean {
    const st = state.session?.state
    return st === 'monitoring' || st === 'recording' || st === 'stopped'
  }

  async function addLabel(req: CreateLabelRequest): Promise<SessionMarker | null> {
    const s = state.session
    if (!s || !markersEditable()) return null
    try {
      const m = await api.createSessionLabel(s.id, req)
      if (!state.markers.some((x) => x.id === m.id)) {
        state.markers = [...state.markers, m].sort((a, b) => a.tsMs - b.tsMs)
      }
      return m
    } catch (err) {
      setError(errText(err))
      return null
    }
  }

  async function addNote(req: CreateNoteRequest): Promise<SessionMarker | null> {
    const s = state.session
    if (!s || !markersEditable()) return null
    try {
      const m = await api.createSessionNote(s.id, req)
      if (!state.markers.some((x) => x.id === m.id)) {
        state.markers = [...state.markers, m].sort((a, b) => a.tsMs - b.tsMs)
      }
      return m
    } catch (err) {
      setError(errText(err))
      return null
    }
  }

  async function addMark(req: CreateMarkRequest): Promise<SessionMarker | null> {
    const s = state.session
    if (!s || !markersEditable()) return null
    try {
      const m = await api.createSessionMark(s.id, req)
      if (!state.markers.some((x) => x.id === m.id)) {
        state.markers = [...state.markers, m].sort((a, b) => a.tsMs - b.tsMs)
      }
      return m
    } catch (err) {
      setError(errText(err))
      return null
    }
  }

  async function removeMarker(markerId: string): Promise<void> {
    const s = state.session
    if (!s) return
    try {
      // §15.2：204 幂等；乐观移除 + 失败回读（重连场景下 id 可能已被其他端删除）
      state.markers = state.markers.filter((x) => x.id !== markerId)
      await api.deleteSessionMarker(s.id, markerId)
    } catch (err) {
      setError(errText(err))
      await refreshMarkers()
    }
  }

  /**
   * label 改名（R28 双击色区）。契约 §15.2 无 rename 端点（POST/DELETE 两个动词）——
   * 改名 = DELETE 原标记 + 同 tsMs 同色重建（label/mark 删除是契约明示的自由度）。
   * DELETE 成功但 POST 失败时回读真实状态，不凭空造回原名。
   */
  async function renameLabel(markerId: string, name: string): Promise<boolean> {
    const s = state.session
    if (!s || !markersEditable()) return false
    const old = state.markers.find((x) => x.id === markerId)
    if (!old || old.kind !== 'label') return false
    const trimmed = name.trim()
    if (!trimmed) return false
    try {
      await api.deleteSessionMarker(s.id, markerId)
      state.markers = state.markers.filter((x) => x.id !== markerId)
      const created = await api.createSessionLabel(s.id, {
        tsMs: old.tsMs,
        name: trimmed,
        ...(old.color !== undefined ? { color: old.color } : {}),
      })
      state.markers = [...state.markers, created].sort((a, b) => a.tsMs - b.tsMs)
      return true
    } catch (err) {
      setError(errText(err))
      await refreshMarkers()
      return false
    }
  }

  async function refreshMarkers(): Promise<void> {
    const s = state.session
    if (!s) return
    try {
      const res = await api.listSessionMarkers(s.id)
      state.markers = res.markers
    } catch {
      /* 标记读回失败不阻塞校准主流程（保留当前标记缓冲） */
    }
  }

  /** R31：升序去重取正整数（空表回退默认；fpsGe 契约口径：正整数列表） */
  function setFpsThresholds(list: number[]): void {
    const clean = [...new Set(list.map((n) => Math.trunc(n)).filter((n) => Number.isFinite(n) && n > 0))].sort(
      (a, b) => a - b,
    )
    state.thresholds.fpsGe = clean.length > 0 ? clean : [...THRESHOLD_DEFAULTS.fpsGe]
  }

  function setFtimeThreshold(ms: number): void {
    if (!Number.isFinite(ms) || ms <= 0) return
    state.thresholds.ftimeMs = Math.trunc(ms)
  }

  function resetThresholds(): void {
    state.thresholds = defaultThresholds()
  }

  /* ---------- 报告页（R41）与导出下载（§13.1） ---------- */

  /** Blob 落地为浏览器下载（纯 DOM，无网络访问——网络仍在 api-client 内，H6） */
  function downloadBlob(blob: Blob, filename: string): void {
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 5_000)
  }

  async function exportRun(runId: string): Promise<void> {
    try {
      const { blob, filename } = await api.exportRunXlsx(runId)
      downloadBlob(blob, filename)
      pushLog('system', 'info', `已导出 Excel：${filename}（GET /api/runs/{id}/export.xlsx）`)
    } catch (err) {
      setError(`Excel 导出失败：${errText(err)}`)
    }
  }

  function openRunReport(runId: string, caseName?: string): void {
    state.report = { kind: 'run', runId, ...(caseName ? { caseName } : {}) }
    state.activeTab = 'report'
  }

  /** 停止后（保存前）打开会话统计预览：对标 PerfDog「停止→看统计→决定保存」[I§2.6] */
  function openSessionReport(): void {
    if (!state.session) return
    state.report = { kind: 'session', sessionId: state.session.id }
    state.activeTab = 'report'
  }

  function closeReport(): void {
    state.report = null
    state.activeTab = 'records'
  }

  /* ---------- M4：回放（R37，[I§2.5] 回放=主窗口复用，同布局切换数据源） ---------- */

  let replayPrevTab: MonitorState['activeTab'] = 'cases'

  /**
   * 入口：我的数据行菜单 / 报告页 / 深度分析诊断卡（run 侧；会话回放无 §19 端点，不提供入口）。
   * M7：seekTsMs 可选——诊断卡点击下降区间 → 回放打开后游标定位该时刻（复用 M4 时间定位）。
   */
  function openReplay(runId: string, caseName?: string, seekTsMs?: number): void {
    if (state.activeTab !== 'replay') replayPrevTab = state.activeTab
    state.replay = { kind: 'run', runId, ...(caseName ? { caseName } : {}) }
    state.replaySeekTs = typeof seekTsMs === 'number' && Number.isFinite(seekTsMs) ? seekTsMs : null
    state.activeTab = 'replay'
  }

  function closeReplay(): void {
    state.replay = null
    state.activeTab = replayPrevTab === 'replay' ? 'cases' : replayPrevTab
  }

  /** M7：ReplayView 数据就绪后消费定位时刻（取走即清，一次有效） */
  function consumeReplaySeek(): number | null {
    const ts = state.replaySeekTs
    state.replaySeekTs = null
    return ts
  }

  /* ---------- M7：深度分析（R50，[I§2.7]；入口=报告页/回放页） ---------- */

  let deepPrevTab: MonitorState['activeTab'] = 'report'

  function openDeep(runId: string, caseName?: string): void {
    if (state.activeTab !== 'deep') deepPrevTab = state.activeTab
    state.deep = { kind: 'run', runId, ...(caseName ? { caseName } : {}) }
    state.activeTab = 'deep'
  }

  function closeDeep(): void {
    state.deep = null
    state.activeTab = deepPrevTab === 'deep' ? 'report' : deepPrevTab
  }

  function setOverlay(v: boolean): void {
    state.overlay = v
  }

  /* ---------- M3：case 库（R42，契约 §14） / 对比池（R43） / 评审（R44） ---------- */

  /** 我的数据查询（筛选条变化后由视图显式调用；sort/order 切换即查） */
  async function loadCases(): Promise<void> {
    state.casesLoading = true
    try {
      const f = state.caseFilter
      // 契约 §14 服务端筛选参数仅 query（caseName 子串）/ device（精确）——「项目」（packageName）
      // 无服务端参数，故项目筛选在取回的窗口内前端过滤：取服务端上限 200 条窗口过滤，
      // total 同步改为窗口内过滤后的条数（视图会声明该边界，不伪装成全量分页）。
      const windowed = Boolean(f.packageName)
      const res = await api.listCases({
        query: f.text || undefined,
        device: f.device || undefined,
        sort: f.sort,
        order: f.order,
        limit: windowed ? 200 : f.limit,
        offset: windowed ? 0 : f.offset,
      })
      if (windowed) {
        state.cases = res.cases.filter((c) => c.packageName === f.packageName)
        state.casesTotal = state.cases.length
      } else {
        state.cases = res.cases
        state.casesTotal = res.total
      }
    } catch (err) {
      setError(errText(err))
    } finally {
      state.casesLoading = false
    }
  }

  function setCaseFilter(patch: Partial<CaseFilterState>): void {
    const before = state.caseFilter
    state.caseFilter = { ...before, ...patch }
    // 筛选/排序变化回到第一页；仅翻页时保持
    if (patch.offset === undefined && (patch.text !== undefined || patch.device !== undefined || patch.packageName !== undefined || patch.sort !== undefined || patch.order !== undefined)) {
      state.caseFilter.offset = 0
    }
  }

  /** 表头排序切换：同列点第二次翻转方向，异列重置为 desc（统计键缺省 desc 直观） */
  function toggleCaseSort(sortKey: CaseSortKey): void {
    if (state.caseFilter.sort === sortKey) {
      state.caseFilter.order = state.caseFilter.order === 'asc' ? 'desc' : 'asc'
    } else {
      state.caseFilter.sort = sortKey
      state.caseFilter.order = 'desc'
    }
    void loadCases()
  }

  async function renameCase(runId: string, caseName: string): Promise<boolean> {
    try {
      await api.renameCase(runId, { caseName })
      pushLog('system', 'info', `用例已改名：${runId} → ${caseName}（runId 不变）`)
      await loadCases()
      await loadRuns()
      return true
    } catch (err) {
      setError(errText(err))
      return false
    }
  }

  async function deleteCase(runId: string): Promise<boolean> {
    try {
      await api.deleteCase(runId)
      state.comparePool = state.comparePool.filter((c) => c.runId !== runId)
      pushLog('system', 'info', `用例已删除（事实源 run 目录 + 索引行 + 同名 .xlsx，契约 §14）：${runId}`)
      await Promise.all([loadCases(), loadRuns()])
      return true
    } catch (err) {
      setError(errText(err))
      return false
    }
  }

  async function rebuildCases(): Promise<void> {
    try {
      const res: RebuildCasesResponse = await api.rebuildCases()
      pushLog('system', 'info', `case 索引已重建：${res.total} 条（rebuiltAt=${res.rebuiltAt}）`)
      await loadCases()
    } catch (err) {
      setError(errText(err))
    }
  }

  /** 评审页候选用例：不带我的数据的筛选态（limit 200=服务端上限窗口），按上传时间倒序 */
  async function loadReviewCandidates(): Promise<void> {
    try {
      const res = await api.listCases({ sort: 'createdAt', order: 'desc', limit: 200, offset: 0 })
      state.reviewCases = res.cases
    } catch (err) {
      setError(errText(err))
    }
  }

  function addToComparePool(rec: CaseRecord): void {
    if (state.comparePool.some((c) => c.runId === rec.runId)) {
      setError('该用例已在对比池中')
      return
    }
    if (state.comparePool.length >= COMPARE_POOL_MAX) {
      // 上限由前端执行（契约 §9 M3 扩展 / [I§2.9] 最多 5 条）
      setError(`对比池最多 ${COMPARE_POOL_MAX} 条用例（PerfDog 同款约束），请先移除一条`)
      return
    }
    state.comparePool.push(rec)
    pushLog('system', 'info', `已加入对比池（${state.comparePool.length}/${COMPARE_POOL_MAX}）：${rec.caseName}`)
  }

  function removeFromComparePool(runId: string): void {
    state.comparePool = state.comparePool.filter((c) => c.runId !== runId)
  }

  function clearComparePool(): void {
    state.comparePool = []
  }

  function inComparePool(runId: string): boolean {
    return state.comparePool.some((c) => c.runId === runId)
  }

  function comparePoolFull(): boolean {
    return state.comparePool.length >= COMPARE_POOL_MAX
  }

  async function runCompareReview(
    baselineRunIds: string[],
    candidateRunId: string,
    mode?: CompareMode,
  ): Promise<CompareResponse> {
    // POST /api/reports/compare（契约 §9）：baseline 可多条（引擎 median + bootstrap），
    // candidate 单条；mode 随请求透传（不传=服务端缺省 same_app_devices）
    return api.compareReports({ baselineRunIds, candidateRunId, ...(mode ? { mode } : {}) })
  }

  /** §9 S3（compare-two-groups-only / compare-budget）：对比池全量送判定引擎。
   * 首条=全局基线组，其余各成一组（引擎 N 路 2–10 组；池上限 5 ≤ 10 恒成立）；
   * budget/fromMs/toMs 随请求透传。旧单数 candidateRunId 同步携带（mock 形态
   * compareReports 消费旧字段；live 后端以 candidateRunIds 为准）。 */
  async function runComparePool(opts?: {
    budget?: CompareBudget
    fromMs?: number
    toMs?: number
  }): Promise<CompareResponse> {
    const runIds = state.comparePool.map((c) => c.runId)
    if (runIds.length < 2) throw new Error('对比池需至少 2 条用例才能运行判定引擎')
    const [baseline, ...candidates] = runIds
    return api.compareReports({
      baselineRunIds: [baseline!],
      candidateRunId: candidates[0]!,
      candidateRunIds: candidates,
      ...(opts?.budget ? { budget: opts.budget } : {}),
      ...(opts?.fromMs !== undefined ? { fromMs: opts.fromMs } : {}),
      ...(opts?.toMs !== undefined ? { toMs: opts.toMs } : {}),
    })
  }

  function clearLogs(): void {
    state.logs = []
  }

  function sessionDurationSec(): number {
    if (!state.session) return 0
    return Math.max(0, Math.floor((state.now - state.session.createdAt) / 1000))
  }

  function recordDurationSec(): number {
    if (!state.recordStartedAt) return 0
    return Math.max(0, Math.floor((state.now - state.recordStartedAt) / 1000))
  }

  return {
    state,
    init,
    setError,
    clearError,
    selectDevice,
    selectApp,
    refreshApps,
    setChartOrder,
    startMatrixSession,
    stopMatrixSession,
    adoptSession,
    closeBackendSession,
    refreshBackendSessions,
    setWifiMode,
    simBoot,
    setFpsWindow,
    selectProcess,
    setLogLevel,
    getRunStartup: (runId: string) => api.getRunStartup(runId),
    toggleMetric,
    setMetricInterval,
    startMonitoring,
    startRecording,
    stopRecording,
    requestStopMonitoring,
    reopenDecision,
    confirmSave,
    confirmDiscard,
    keepDecisionPending,
    setOverlay,
    loadRuns,
    getRunDetail: (runId: string) => api.getRun(runId),
    getRunStatistics: (runId: string, q?: StatisticsQuery) => api.getRunStatistics(runId, q),
    getSessionStatistics: (sessionId: string, q?: StatisticsQuery) => api.getSessionStatistics(sessionId, q),
    getSessionDetail: (sessionId: string) => api.getSession(sessionId),
    getSessionSummary: (sessionId: string) => api.getSessionSummary(sessionId),
    getMetricsWindow: (sessionId: string, q: MetricsWindowQuery) => api.getMetricsWindow(sessionId, q),
    addLabel,
    addNote,
    addMark,
    removeMarker,
    renameLabel,
    refreshMarkers,
    markersEditable,
    setFpsThresholds,
    setFtimeThreshold,
    resetThresholds,
    exportRun,
    openRunReport,
    openSessionReport,
    closeReport,
    openReplay,
    closeReplay,
    consumeReplaySeek,
    openDeep,
    closeDeep,
    getRunDeepAnalysis: (runId: string, frameIndex?: number) => api.getRunDeepAnalysis(runId, frameIndex),
    getRunDiagnostics: (runId: string, q?: DiagnosticsQuery) => api.getRunDiagnostics(runId, q),
    startTimedScreenshot: (sessionId: string, req?: TimedScreenshotRequest) => api.startTimedScreenshot(sessionId, req),
    getRunReplay: (runId: string, maxPoints?: number) => api.getRunReplay(runId, { maxPoints }),
    getRunMarkers: (runId: string) => api.getRunMarkers(runId),
    listRunArtifacts: (runId: string) => api.listRunArtifacts(runId),
    getRunVideoUrl: (runId: string, name?: string) => api.getRunVideoUrl(runId, name),
    getRunArtifactUrl: (runId: string, name: string) => api.getRunArtifactUrl(runId, name),
    loadCases,
    setCaseFilter,
    toggleCaseSort,
    renameCase,
    deleteCase,
    rebuildCases,
    loadReviewCandidates,
    addToComparePool,
    removeFromComparePool,
    clearComparePool,
    inComparePool,
    comparePoolFull,
    runCompareReview,
    runComparePool,
    captureRunStartup: (runId: string, req: StartupCaptureRequest) => api.captureRunStartup(runId, req),
    clearLogs,
    sessionDurationSec,
    recordDurationSec,
  }
}
