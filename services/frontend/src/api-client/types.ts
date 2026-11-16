/**
 * API 类型定义 —— 单一事实源：开发仓库内部 API 契约文档
 *
 * 本文件手写对齐契约（ARCHITECTURE.md §5.2 规则 3）；后端契约测试
 * （services/backend/tests/test_contract_examples.py）以契约文档示例 JSON 为 golden。
 * 依据契约 §11.1：消费方必须忽略未知字段/未知 SSE type —— 因此解析处不做严格窄化。
 */

/* ---------------- §1 总则 / §2 健康检查 ---------------- */

/** §23（M-iOS）：'simulator' = iOS Simulator 采集链（xcrun simctl） */
export type ProviderMode = 'real' | 'synthetic' | 'simulator'

/**
 * backend.framework：契约 §2 为 'fastapi' | 'stdlib'（mock 前端形态已于
 * 2026-11-12 移除，不再出现在枚举）。
 */
export type BackendFramework = 'fastapi' | 'stdlib'

export interface Health {
  status: string
  version: string
  backend: { framework: BackendFramework; python: string }
  providers: { devices: ProviderMode; metrics: ProviderMode }
  uptimeSec: number
}

/* ---------------- §3 错误约定 ---------------- */

/** 契约 §3 错误码表。network_error / timeout 为 api-client 本地生成（非服务端契约码）。 */
export type ApiErrorCode =
  | 'validation_error'
  | 'device_not_found'
  | 'device_not_online'
  | 'device_ambiguous'
  | 'device_none_online'
  | 'adb_devices_failed'
  | 'adb_not_found'
  | 'session_not_found'
  | 'session_state_conflict'
  | 'metric_unavailable'
  | 'device_busy'
  | 'report_not_found'
  | 'not_implemented'
  | /** §21.1（M6）：adb tcpip/connect 执行失败 */ 'wifi_connect_failed'
  | /** §23（M-iOS）：simulator 模式 xcrun simctl 不可用/启动超时（§3 错误表 M-iOS 行） */ 'simctl_unavailable'
  | 'internal_error'
  | 'network_error'
  | 'timeout'

export interface ApiErrorBody {
  error: { code: ApiErrorCode; message: string; details?: Record<string, unknown> | null }
}

/* ---------------- §4 设备与应用 ---------------- */

export type DeviceState = 'online' | 'offline' | 'unauthorized'
/** §23.0（M-iOS）：'simulator' = iOS Simulator 直连（xcrun simctl） */
export type ConnectionMode = 'usb' | 'wifi' | 'simulator'

export interface DeviceInfo {
  os: string
  cpu: string
  resolution: string
  batteryLevel: number | null
  refreshRateHz: number | null
}

/**
 * §20.2（M6）设备 profile（可选字段，缺省 = 未下发）：勾选面板可用性判定输入之一
 * （§20.3 capabilities ∩ profile 交集判定；服务端 §5 available 仍是权威判定）。
 * `gpuSource` ∈ "kgsl" | null（null = 无 GPU 利用率源，驱动 §20.1 kgsl_missing）。
 */
export interface DeviceProfile {
  usb?: boolean
  wifi?: boolean
  /** §23.1（M-iOS）：simulator 连接模式标志（ios 设备恒 true，android 恒 false） */
  simulator?: boolean
  gpuSource?: string | null
  cpuCores?: number | null
  refreshRateHz?: number | null
}

export interface Device {
  id: string
  platform: string
  model: string
  serial: string
  state: DeviceState
  connection: ConnectionMode
  info: DeviceInfo | null
  /** §20.2（M6）：可选 profile；消费方必须容忍缺失（§11.1） */
  profile?: DeviceProfile
}

export interface DevicesResponse {
  mode: ProviderMode
  devices: Device[]
}

export interface ProcessEntry {
  pid: number
  name: string
  isMain?: boolean
}

export interface AppEntry {
  packageName: string
  name: string
  pid: number | null
  running: boolean | null
  isForeground: boolean | null
  processes: ProcessEntry[]
}

export interface DeviceAppsResponse {
  deviceId: string
  apps: AppEntry[]
}

/* ---------------- §5 指标注册表 ---------------- */

/** §21.2（M6）：'none' = 观察型指标（cpu.freq.mhz 频率值无好坏方向） */
export type MetricDirection = 'lower_better' | 'higher_better' | 'none'

/**
 * §20.1（M6）指标 capabilities（可选对象）：modes = 指标在哪些连接模式下有意义
 * （§23.0 M-iOS 起为 ⊆ ["usb","wifi","simulator"]）；platform = "android" | "ios"
 * （§23.0）；reason 仅在 available=false 时出现的机器可读码（人工文案由前端
 * 映射表渲染，§20.1）。
 */
export interface MetricCapabilities {
  modes?: ConnectionMode[]
  platform?: string
  reason?: string
}

export interface MetricDef {
  metricId: string
  unit: string
  direction: MetricDirection
  evidenceLevel: string
  available: boolean
  /** 契约 §6：intervalMs 缺省取 /api/metrics 各指标默认间隔（可选字段，缺省时用本地默认） */
  defaultIntervalMs?: number
  /** §20.1（M6）：可选能力描述；勾选面板可用性 = capabilities ∩ 设备 profile（§20.3） */
  capabilities?: MetricCapabilities
  /** §21.2/§21.3（M6）：可选口径声明/官方损耗提示文案（原文随 UI 渲染） */
  description?: string
}

export interface MetricsResponse {
  mode: ProviderMode
  metrics: MetricDef[]
}

/* ---------------- §6 会话 ---------------- */

export type SessionState = 'monitoring' | 'recording' | 'stopped' | 'saved' | 'discarded'

export interface MetricSelection {
  metricId: string
  intervalMs?: number
}

/** §21.5（M6）FPS 窗口选择：type 缺省 auto（游戏→surface_view / 应用→activity） */
export type FpsWindowType = 'activity' | 'surface_view' | 'auto'

export interface FpsWindow {
  type: FpsWindowType
}

/** §21.7（M6）实时日志级别；缺省（未提供）不发 log 消息（[I§2.1] ☐Log 勾选语义） */
export type LogLevel = 'verbose' | 'debug' | 'info' | 'warn' | 'error'

export interface CreateSessionRequest {
  deviceId: string
  packageName: string
  metrics?: MetricSelection[]
  interactive?: boolean
  launchOnCreate?: boolean
  /** §21.5（M6）：可选 FPS 窗口；非法 type → 400 validation_error */
  fpsWindow?: FpsWindow
  /** §21.6（M6）：可选目标子进程（必须属于该 app 的 processes[].name，否则 400） */
  processName?: string
  /** §21.7（M6）：可选实时日志级别；提供即开启 SSE log 流 */
  logLevel?: LogLevel
}

export interface Session {
  id: string
  state: SessionState
  deviceId: string
  packageName: string
  metrics: MetricSelection[]
  createdAt: number
  /** 详情快照：各 metricId 的最新值与样本数（契约 §6） */
  stats?: Record<string, { latest: number | null; samples: number }>
  /* --- §21（M6）回显字段（可选；始终返回的形态由服务端保证，消费方容忍缺失） --- */
  /** §21.5：缺省 {"type":"auto"} */
  fpsWindow?: FpsWindow
  /** §21.6：null = 主进程缺省 */
  processName?: string | null
  /** §21.7：null = 未开启日志流 */
  logLevel?: LogLevel | null
  /** 契约 §6.2：录制起点 epoch ms（null = 未进入 recording）；刷新接管后还原记录时长 */
  recordStartedAt?: number | null
}

export interface PatchMetricsRequest {
  enable?: MetricSelection[]
  disable?: string[]
}

/** 契约 §6/§13.2：save 请求。caseName 缺省时后端自动生成 PD_<时间戳>；
 * exportExcel（M2 可选扩展，§13.2 [I§2.6] Save(excel) 复选）为 true 时在 run 目录旁
 * 生成同名 .xlsx。消费方必须忽略未知字段（§11.1），故请求体不做严格窄化。 */
export interface SaveSessionRequest {
  caseName?: string
  exportExcel?: boolean
}

export interface SaveResult {
  sessionId: string
  runId: string
  state: SessionState
  runPath: string
  /** §13.2：仅 exportExcel=true 且生成成功时出现（xlsx 绝对路径） */
  exportPath?: string
  /** §13.2：仅导出失败时出现；与 exportPath 互斥。导出失败不使 save 失败 */
  exportError?: string
}

/* ---------------- §21.1 WiFi 模式（M6，R15；real-only） ---------------- */

export interface WifiModeRequest {
  action: 'enable' | 'connect'
  /** action="connect" 必填（缺 host → 400 validation_error） */
  host?: string
}

/** §21.1 响应：enable → {deviceId, action, state, port}；connect → {deviceId, serial,
 * action, host, port, connection}。两形态字段并集按可选建模（§11.1 容忍缺失）。 */
export interface WifiModeResult {
  deviceId: string
  action: 'enable' | 'connect'
  state?: string
  serial?: string
  host?: string
  port?: number
  connection?: ConnectionMode
}

/* ---------------- §23.1 sim-boot（M-iOS；simulator-only） ---------------- */

/** §23.1 POST /api/devices/{deviceId}/sim-boot `200 OK` 回执（与契约 golden 同形；
 * 已 Booted 设备幂等返回枚举态）。synthetic/real 模式 → 501 not_implemented +
 * SSE sim-boot.unavailable 诊断；503 simctl_unavailable（xcrun 缺失/启动超时）。 */
export interface SimBootResult {
  deviceId: string
  action: 'boot'
  /** simctl 枚举态映射（Booted→online，其余→offline；§23.1） */
  state: DeviceState
  [key: string]: unknown
}

/* ---------------- §21.4 StartupTiming（M6，R18）+ S3 startup capture（run 级旁路测量） ---------------- */

/** §21.4 GET /api/runs/{runId}/startup：ttidMs/ttfdMs 由 S3 启动捕获作业
 * （startup.ndjson）回填，未捕获/未观测到对应行时为 null 且 note 显式声明口径
 * （null ≠ 0，不填假零值）；run 无启动测量 → totalTimeMs:null + note。 */
export interface StartupTiming {
  runId: string
  totalTimeMs: number | null
  ttidMs: number | null
  ttfdMs: number | null
  note: string
}

/** §21.4 S3 实现期编排口：POST /api/runs/{runId}/startup/capture 请求体。
 * deviceSerial 从设备列表取（WiFi 行 id 带 -wifi 后缀时后端经 provider.adb_serial
 * 映射回 host:port，映射口径同 collector serial 参数）。 */
export interface StartupCaptureRequest {
  deviceSerial: string
  packageName?: string
}

/** §21.4 S3 捕获回执（= startup.ndjson 行 / perfdog.startup.v1）：
 * state=completed 已落盘（缺失值 null + note）；state=failed 未落盘（error 申报，
 * 无测量产物不伪造）。该作业会重新拉起 APP（[F§3.11]）。 */
export interface StartupCaptureResult {
  schemaVersion: string
  runId: string
  capturedAt: number
  deviceSerial: string
  packageName?: string | null
  state: 'completed' | 'failed'
  component?: string | null
  totalTimeMs: number | null
  launchState?: string | null
  ttidMs: number | null
  ttfdMs: number | null
  error?: string
  note: string
  [key: string]: unknown
}

/* ---------------- §7 指标数据 ---------------- */

export interface MetricPoint {
  ts: number
  value: number | null
}

export interface MetricSeries {
  metricId: string
  unit: string
  points: MetricPoint[]
}

export interface MetricsWindowQuery {
  metricIds?: string[]
  fromMs?: number
  toMs?: number
  maxPoints?: number
}

export interface MetricsWindow {
  sessionId: string
  fromMs: number
  toMs: number
  series: MetricSeries[]
  downsampled: boolean
  /** 契约 §7：触发降采样时附带 sampling: "lttb" */
  sampling?: string
}

/** perfeval.summary.v1 原样透出（契约 §7）；M1 前端只读 metric 级统计，不做强类型窄化 */
export interface SessionSummary {
  schemaVersion?: string
  generatedAt?: number
  metrics?: Array<{
    metricId: string
    unit?: string
    count?: number
    avg?: number | null
    min?: number | null
    max?: number | null
    latest?: number | null
  }>
  [key: string]: unknown
}

/* ---------------- §8 实时通道（SSE）消息 ---------------- */

export interface SseEnvelope {
  v: number
  seq: number
  ts: number
  type: string
  sessionId: string
}

export type MetricPhase = 'app_prepare' | 'interaction' | 'collection' | 'analysis'
export type PhaseState = 'started' | 'completed' | 'failed'
export type DiagnosticSeverity = 'info' | 'warn' | 'error'

export type SseMessage =
  | (SseEnvelope & { type: 'hello'; state: SessionState })
  | (SseEnvelope & {
      type: 'sample'
      metricId: string
      value: number | null
      unit: string
      tags: Record<string, unknown>
    })
  | (SseEnvelope & {
      type: 'phase'
      phase: MetricPhase
      state: PhaseState
      label: string | null
      detail: string | null
    })
  | (SseEnvelope & {
      type: 'diagnostic'
      code: string
      severity: DiagnosticSeverity
      message: string
      detail: string | null
    })
  | (SseEnvelope & { type: 'session.state'; state: SessionState })
  | (SseEnvelope & { type: 'metrics.config'; metrics: MetricSelection[] })
  | (SseEnvelope & { type: 'log'; level: string; source: string; line: string })
  | (SseEnvelope & { type: 'ping' })
  | (SseEnvelope & { type: 'gap'; fromSeq: number; toSeq: number })
  | SseMarkerMessage

/* ---------------- §9 报告与运行记录 ---------------- */

export interface RunSummary {
  runId: string
  caseName: string
  createdAt: number
  device: string | null
  packageName: string | null
  summaryAvailable: boolean
  path: string
}

export interface RunsResponse {
  runs: RunSummary[]
}

export interface RunArtifactsIndex {
  name: string
  kind: string
  sizeBytes?: number | null
}

/** 契约 §9：manifest 摘要 + summary.json（原样）+ artifacts 索引 */
export interface RunDetail extends RunSummary {
  manifest?: Record<string, unknown> | null
  summary?: SessionSummary | null
  artifacts?: RunArtifactsIndex[]
  /** mock 扩展（§11.1 可选响应字段）：来源会话 id，报告页据此拉全量曲线；live 形态可能缺失 */
  sessionId?: string
}

/** 契约 §9 / perfeval compare_summaries 的对比模式；缺省 same_app_devices（与 live
 * compare_service.DEFAULT_COMPARE_MODE 一致）。engine 前提：same_device_versions 要求
 * 同设备且应用版本变化（否则 device_mismatch / app_version_not_changed）；
 * same_app_devices 要求同应用且设备变化（否则 app_mismatch / device_not_changed）。 */
export type CompareMode = 'same_device_versions' | 'same_app_devices'

/** §9 S3 扩展（可选字段，§11.1 不升版本）：逐指标预算门禁，键口径随引擎
 * _budget_for_metric（warn/fail 回归阈值百分比小数、绝对值 ms 等标量叶值）。
 * defaults 组 + metrics[metricId] 组两层合并；嵌套容器后端 400 validation_error。 */
export interface CompareBudgetMetric {
  warnRegressionPct?: number
  failRegressionPct?: number
  warnRegressionAbs?: number
  failRegressionAbs?: number
  warnAbs?: number
  failAbs?: number
  minDeltaAbs?: number
  minRuns?: number
  recommendedRuns?: number
  [key: string]: unknown
}

export interface CompareBudget {
  defaults?: CompareBudgetMetric
  metrics?: Record<string, CompareBudgetMetric>
  [key: string]: unknown
}

/** §9 S3 扩展请求体：candidateRunIds（N 路 2–10 组，旧单数 candidateRunId 保留
 * 兼容；同给时以 candidateRunIds 为准）、budget（逐指标预算）、fromMs/toMs
 * （时间区间窗口化，epoch ms 闭区间，时戳域按 §18）。 */
export interface CompareRequest {
  baselineRunIds: string[]
  /** 旧单数候选（兼容保留；N 路请求由 candidateRunIds 承载） */
  candidateRunId: string
  /** §9 S3：N 路候选数组（每条各成一组，与 baselineRunIds 组成 2–10 组） */
  candidateRunIds?: string[]
  budget?: CompareBudget
  fromMs?: number
  toMs?: number
  /** 对比模式（可选；不传=服务端缺省 same_app_devices） */
  mode?: CompareMode
}

/* ---------------- §12 统计量端点（M2，perfdog.statistics.v1） ---------------- */

/** §12 数据基线三值：frameEvents=逐帧事件 / fpsSeries=fps 轮询点列 / none=两者皆无 */
export type FrameDataBasis = 'frameEvents' | 'fpsSeries' | 'none'

/** §12 fps 组：avg/var/std/drop/min/median/medRange + ge（阈值→占比）。
 * 基线为 none 时全组数值字段为 null（不填假零值，[R§8.6]）；fpsSeries 且间隔 ≠1000ms
 * 或间隔未知时 fps.note 声明节拍差（§12 计算规则 2）。 */
export interface FpsStats {
  avg: number | null
  var: number | null
  std: number | null
  drop: number | null
  min: number | null
  median: number | null
  medRange: number | null
  /** 键 = 阈值的十进制字符串（升序），值 = FPS≥阈值的点占比 %；无数据时为 null */
  ge: Record<string, number | null>
  note?: string
}

/** §12 frameTime 组（逐帧耗时统计，fpsSeries/none 基线不可计算 → 全 null + note） */
export interface FrameTimeStats {
  avg: number | null
  std: number | null
  var: number | null
  delta: number | null
  note?: string
}

/** §12 jank 组（双条件判定，[F§3.3]；per10min = Jank(/10min)，BigJank(/10min) 本期不单列） */
export interface JankStats {
  small: number | null
  big: number | null
  jank: number | null
  stutter: number | null
  per10min: number | null
  note?: string
}

/** §12 smooth 组：近似口径（官方未公开公式），note 恒带（§12 计算规则 1） */
export interface SmoothStats {
  value: number | null
  note: string
}

/** §12 lowFps 组：1%Low（帧耗时最长 1% 均值换算 fps，可精确复刻） */
export interface LowFpsStats {
  p1: number | null
  note?: string
}

/** §12 统计量载荷（契约示例 JSON 为 golden；§11.1 兼容：允许未知字段） */
export interface Statistics {
  schemaVersion: string
  runId?: string
  sessionId?: string
  frameDataBasis: FrameDataBasis
  /** fps 点列节拍声明；none 基线为 null（§12 计算规则 2） */
  frameSeriesIntervalMs: number | null
  /** 帧点列覆盖时长（首尾点差），非会话总时长（§12 计算规则 3） */
  durationMs: number
  /** §18：请求给出 fromMs/toMs 时顶层回显同名字段（§11.1 新增可选字段） */
  fromMs?: number
  toMs?: number
  fps: FpsStats
  frameTime: FrameTimeStats
  jank: JankStats
  smooth: SmoothStats
  lowFps: LowFpsStats
  [key: string]: unknown
}

/** §12 fpsGe 查询参数：FPS>=x[%] 阈值列表（正整数，缺省 18,25）。
 * §18（M5 R30/R31）扩展：fromMs/toMs 可选区间（epoch ms，闭区间含端点）；
 * 只给 fromMs（不给 toMs）= 从 fromMs 起至数据结束（§18：两参数各自可选）；
 * 请求给出区间时顶层回显同名字段。 */
export interface StatisticsQuery {
  fpsGe?: number[]
  fromMs?: number
  toMs?: number
}

/** §13.1 export.xlsx 响应元数据（二进制体由调用方接 Blob） */
export interface XlsxBlob {
  blob: Blob
  /** Content-Disposition 解析出的文件名（RFC 6266，filename* 优先）；解析失败回退 <runId>.xlsx */
  filename: string
}

/* ---------------- §14 case 库（M3，[I§2.9] 我的数据用例表） ---------------- */

/** §14 排序键：缺省 createdAt（[I§2.9] 按最新上传时间排列）；统计键排序时 null 恒排末尾 */
export type CaseSortKey =
  | 'createdAt'
  | 'avgFps'
  | 'smooth'
  | 'jankPer10min'
  | 'peakMemoryMb'
  | 'avgAppCpuPct'
  | 'avgPowerMa'

export type SortOrder = 'asc' | 'desc'

/** §14 统计快照：与 §12 统计层同源计算（avgFps/smooth/jankPer10min ← perfdog.statistics.v1，
 * peakMemoryMb/avgAppCpuPct/avgPowerMa ← samples 序列的 max/mean）；无数据为 null，不填 0 */
export interface CaseStats {
  avgFps: number | null
  smooth: number | null
  jankPer10min: number | null
  peakMemoryMb: number | null
  avgAppCpuPct: number | null
  avgPowerMa: number | null
}

/** §14 用例行（golden 形态）。run 目录（NDJSON/artifact）是事实源，本行是索引快照 */
export interface CaseRecord {
  runId: string
  caseName: string
  /** 设备标识：新 run 为 deviceId；[R§2.12] 兼容导入的旧 run 为 manifest 记录原文 */
  device: string
  packageName: string | null
  createdAt: number
  summaryAvailable: boolean
  stats: CaseStats
  path: string
}

export interface CasesResponse {
  cases: CaseRecord[]
  /** 筛选后总数（[I§2.9] 用例表分页 Total） */
  total: number
}

export interface CasesQuery {
  /** caseName 子串（不区分大小写） */
  query?: string
  /** 设备标识精确匹配 */
  device?: string
  sort?: CaseSortKey
  order?: SortOrder
  /** 1–200，缺省 50 */
  limit?: number
  offset?: number
}

/** §14 PATCH /api/cases/{runId}：只改显示名，不移动 run 目录，runId 不变 */
export interface RenameCaseRequest {
  caseName: string
}

/** §14 POST /api/cases/rebuild：按事实源重建索引（幂等），事实源消失的行物理清除 */
export interface RebuildCasesResponse {
  rebuiltAt: number
  total: number
}

/* ---------------- §9 对比（perfeval.compare.v1 透传 + M3 meta 扩展） ---------------- */

/** perfeval.compare.v1 指标项内的单侧序列摘要（core/_stats.py summarize_numeric_series 口径） */
export interface CompareSeriesSummary {
  n: number
  min: number | null
  max: number | null
  mean: number | null
  median: number | null
  value: number | null
  p75: number | null
  p90: number | null
  p95: number | null
  mad: number | null
  outlierCount: number
  outlierRatio: number
  confidence: string
  [key: string]: unknown
}

/** 单指标对比结论（engine.py _metric_output；§11.1 允许未知字段） */
export interface MetricCompare {
  priority?: string
  unit?: string | null
  direction?: string
  baseline: CompareSeriesSummary
  candidate: CompareSeriesSummary
  delta: {
    abs: number | null
    pct: number | null
    bootstrapCi95: [number, number] | null
  }
  budget?: Record<string, unknown>
  /** pass | warn | fail | unknown（样本量不足时 unknown，见 engine 判定序） */
  judgement: 'pass' | 'warn' | 'fail' | 'unknown'
  reason?: string | null
  confidence?: { level: string; bootstrapCi95: [number, number] | null; noise: string }
  reliability?: { score: number; level: string; reasons: string[] }
  evidence?: { level: string | null; artifacts: string[]; missingRuns: string[] }
  /** §9 S3 N 路扩展（可选，仅多候选响应）：逐对判定明细（groupIndex ≥1 组 vs 基线组） */
  pairs?: CompareMetricPair[]
  /** §9 S3 N 路扩展（可选，仅多候选响应）：逐组序列统计（N 路 UI 呈现面） */
  perGroup?: CompareMetricGroupStats[]
  [key: string]: unknown
}

/** §9 S3 N 路：单指标逐对判定元素（pairs[] 元素） */
export interface CompareMetricPair {
  groupIndex: number
  runIds: string[]
  judgement: 'pass' | 'warn' | 'fail' | 'unknown'
  reason?: string | null
  delta?: MetricCompare['delta']
  confidence?: MetricCompare['confidence']
  reliability?: MetricCompare['reliability']
  [key: string]: unknown
}

/** §9 S3 N 路：单指标逐组统计投影（perGroup[] 元素；无数据组各值 null 不填 0） */
export interface CompareMetricGroupStats {
  groupIndex: number
  n: number
  median: number | null
  mean: number | null
  min: number | null
  max: number | null
  p95: number | null
  [key: string]: unknown
}

/** 契约 §9：POST /api/reports/compare 响应——perfeval.compare.v1 原样透传体。
 * M3 扩展：允许顶层可选 `meta`（§11.1 新增可选字段不升版本），承载对比页展示信息。 */
export interface CompareResponse {
  schemaVersion: string
  compareId?: string
  mode?: string
  baselineGroup?: string
  candidateGroup?: string
  /** pass | warn | fail | unknown | invalid（前提校验失败，errors 携带码） */
  status: 'pass' | 'warn' | 'fail' | 'unknown' | 'invalid'
  /** 仅 invalid 时出现：app_mismatch / device_not_changed / app_version_not_changed 等 */
  errors?: string[]
  focusedWindows?: unknown
  metrics: Record<string, MetricCompare>
  /** §9 S3 N 路扩展（可选，仅 candidateRunIds 多候选时出现）：逐组元数据（index 0 =
   * 全局基线组）。engine.compare_summaries groups 2–10 组。 */
  groups?: CompareGroupMeta[]
  summary?: {
    baselineRuns: number
    candidateRuns: number
    /** §9 S3 N 路：各组 run 数（长度 = 组数；两路旧响应无该字段） */
    groupCounts?: number[]
    metricCount: number
    unknownMetrics: string[]
  }
  reliability?: { score: number; level: string; reasons: string[] }
  /** §9 M3 扩展（可选）：各 run 的展示信息（caseName/device/时长）+ 可比前提提示
   * （同设备/场景/时长/初始状态 [F§8]）。键与后端 compare_service 一致：
   * baseline/candidate 数组 + preconditionNote；§11.1 消费方必须忽略未知字段。
   * S3 扩展可选键：nWayNote / fromMs / toMs。 */
  meta?: {
    baseline?: CompareRunMeta[]
    candidate?: CompareRunMeta[]
    preconditionNote?: string
    nWayNote?: string
    fromMs?: number
    toMs?: number
    [key: string]: unknown
  }
  [key: string]: unknown
}

/** §9 S3 N 路：顶层 groups 元素（index 0 = 全局基线组） */
export interface CompareGroupMeta {
  index: number
  runIds: string[]
  runCount: number
  [key: string]: unknown
}

/** §9 meta 单 run 展示信息（后端 compare_service._run_meta 同形） */
export interface CompareRunMeta {
  runId: string
  caseName?: string
  device?: string | null
  packageName?: string | null
  durationMs?: number | null
  summaryAvailable?: boolean
  [key: string]: unknown
}

/* ---------------- §15 标记与标注（M5 sidecar `perfdog.markers.v1`；M4 回放展示） ---------------- */

/** §15.1 三类标记：label=场景段起点（色带）/ note=批注（图标）/ mark=瞬时标定（红竖线） */
export type MarkerKind = 'label' | 'note' | 'mark'

/**
 * 标记元素（§15.1 sidecar 行 / §15.3 run 读回 / §19 replay.markers 段同形同源）。
 * 落盘元素无 id（§15.1）；会话期读回（§15.2）才有服务端分配的 id —— 按 §11.1 全部可选。
 */
export interface ReplayMarker {
  tsMs: number
  kind: MarkerKind
  /** label 必填（场景名）；note 为 text；mark 无附加字段 */
  name?: string
  color?: string
  text?: string
  /** §15.2 会话期读回的操作句柄（落盘不存在）；未知字段忽略 */
  id?: string
  [key: string]: unknown
}

/** §15.3 GET /api/runs/{runId}/markers 体（= §19 replay.markers 段；无标记 → markers: []） */
export interface MarkersPayload {
  schemaVersion: string
  runId?: string
  markers: ReplayMarker[]
  [key: string]: unknown
}

/* ---------------- §15.2 会话标记端点（M5，R28/R29） ---------------- */

/** §15.2 POST /api/sessions/{id}/labels：tsMs 缺省 = 服务端当前 epoch ms */
export interface CreateLabelRequest {
  tsMs?: number
  name: string
  /** hex 色值；缺省由前端色板分配 */
  color?: string
}

/** §15.2 POST /api/sessions/{id}/notes：批注文字（双击曲线创建 / 双击已生成批注=DELETE 取消） */
export interface CreateNoteRequest {
  tsMs?: number
  text: string
}

/** §15.2 POST /api/sessions/{id}/marks：瞬时标定（红色竖线） */
export interface CreateMarkRequest {
  tsMs?: number
}

/** §15.2 三端点 201 Created 回执：会话期元素带服务端分配 id（DELETE 句柄，不落盘）。
 * 请求体未知字段忽略（§11.1），响应按 ReplayMarker（id 可选）读回。 */
export type SessionMarker = ReplayMarker & { id: string }

/** §15.2 GET /api/sessions/{sessionId}/markers：当前会话内存标记（含 id），按 tsMs 升序 */
export interface SessionMarkersResponse {
  sessionId: string
  markers: SessionMarker[]
  [key: string]: unknown
}

/** §15.2 SSE 新 type `marker`（标记增删经会话通道广播；action ∈ add | remove）。
 * remove 时 marker 仅含 id（§15.2 广播形态）。 */
export type SseMarkerMessage = SseEnvelope & {
  type: 'marker'
  action: 'add' | 'remove'
  marker: SessionMarker | { id: string }
}

/* ---------------- §16 录屏（M4）与 §17 工件服务（M4） ---------------- */

/** §17 artifacts 索引元素 kind（其余采集工件归 data） */
export type ArtifactKind = 'video' | 'screenshot' | 'data'

/** §17 GET /api/runs/{runId}/artifacts 元素（= §9 run 详情 artifacts 段 / §19 replay.artifacts 段） */
export interface ArtifactEntry {
  name: string
  kind: ArtifactKind
  bytes?: number | null
  sha256?: string
  collector?: string
  [key: string]: unknown
}

export interface RunArtifactsResponse {
  runId: string
  artifacts: ArtifactEntry[]
  [key: string]: unknown
}

/** §16.1 manifest context.clockAnchors 的 video 段（形状按契约原样；未知字段忽略） */
export interface ClockAnchorVideo {
  startEpochMs?: number
  firstFrameEpochMs?: number
  method?: string
  timeLimitS?: number
  segments?: unknown
  offsetMs?: number
  [key: string]: unknown
}

/** §16.1 clock anchors 四锚点形状（t0/video/markers/samplers；无锚点时整体为 null）。
 * 视频跳转公式 [R§2.9]：video.currentTime = (eventEpochMs - videoFirstFrameEpochMs) / 1000 */
export interface ClockAnchors {
  t0?: { t0EpochMs?: number; t0Source?: string; clockDomain?: string; [key: string]: unknown } | null
  video?: ClockAnchorVideo | null
  markers?: Array<{ name: string; epochMs: number }> | null
  samplers?: { startEpochMs?: number; source?: string; boottimeNs?: number; method?: string; [key: string]: unknown } | null
  [key: string]: unknown
}

/* ---------------- §22 深度分析与诊断（M7，R38/R50/R51） ---------------- */

/**
 * §22.1 帧元素（= perfeval `FrameRecord.to_dict()` 形状原样，契约「不加壳不改名」）。
 * 字段含义：durationMs=FTime；jankType/presentType 为 FrameTimeline 原文枚举。
 */
export interface FrameRecord {
  frameIndex: number
  tsEpochMs: number
  durationMs: number
  jank: boolean
  jankType: string | null
  presentType: string
  onTimeFinish: boolean
  expectedDurationMs: number
  processName?: string | null
  pid?: number | null
  [key: string]: unknown
}

/** §22.1 CPU 调度片段（trace sched；state ∈ Running/Runnable/Sleeping 等 trace 原文） */
export interface CpuSchedulingSlice {
  core: number
  tid: number
  threadName: string
  pid?: number | null
  state: string
  startEpochMs: number
  durationMs: number
  [key: string]: unknown
}

/** §22.1 M8+ 扩展：simpleperf stat 卡（parse_simpleperf_report_text 输出，报告面同源） */
export interface SimpleperfFaceStat {
  arch?: string | null
  events: Array<{ event: string; samples: number | null; eventCount: number | null }>
  mode: 'flat' | 'callgraph'
  basis: string
  entryCount: number
  maxDepth: number
  unparsedLines: number
  [key: string]: unknown
}

/** §22.1 M8+ 扩展：火焰图树节点（value = 该事件 overhead%，0 = 链上延续帧） */
export interface FlamegraphNode {
  name: string
  value: number
  children?: FlamegraphNode[]
  [key: string]: unknown
}

/** §22.1 M8+ 扩展：simpleperf 火焰图数据面（帧列表概览形态携带；无工件 → available=false + note） */
export interface SimpleperfFace {
  available: boolean
  source: string | null
  stat: SimpleperfFaceStat | null
  flamegraph: { tree: FlamegraphNode; basis: string; maxDepth: number } | null
  note: string | null
  [key: string]: unknown
}

/** §22.1 帧列表概览形态（缺省，无 ?frameIndex）；无 perfetto trace → 空结构 + note */
export interface DeepAnalysisOverview {
  runId: string
  traceAvailable: boolean
  /** 如 perfetto.frame_timeline；traceAvailable=false → null */
  source: string | null
  vsyncPeriodMs?: number | null
  frameCount: number
  jankCount: number
  jankRate: number | null
  frames: FrameRecord[]
  note: string | null
  /** M8+ 扩展：simpleperf 火焰图数据面（旧后端无该字段 → undefined） */
  simpleperf?: SimpleperfFace | null
  [key: string]: unknown
}

/** §22.1 单帧 Overview 卡形态（?frameIndex=N：单帧 + 该帧附近 CPU 调度片段） */
export interface DeepAnalysisFrameDetail {
  runId: string
  traceAvailable: boolean
  frame: FrameRecord
  cpuScheduling: CpuSchedulingSlice[]
  note: string | null
  [key: string]: unknown
}

export type DeepAnalysisResponse = DeepAnalysisOverview | DeepAnalysisFrameDetail

/** 形态判别（§22.1 两 golden 形态以 `frame` 字段存在性区分） */
export function isDeepAnalysisFrameDetail(r: DeepAnalysisResponse): r is DeepAnalysisFrameDetail {
  return typeof (r as DeepAnalysisFrameDetail).frame === 'object' && (r as DeepAnalysisFrameDetail).frame !== null
}

/** §22.2 GET /api/runs/{runId}/diagnostics 查询参数（均可缺省：anchor=frame.fps.mean、15、1000） */
export interface DiagnosticsQuery {
  /** 锚指标 metricId；未注册 → 400 validation_error */
  anchor?: string
  /** 相邻点下降百分比阈值（正数） */
  dropThresholdPct?: number
  /** 最小持续时长 ms（正整数） */
  minDurationMs?: number
}

/** §22.2 下降区间元素（锚指标序列上检测；fromMs/toMs 为 epoch ms） */
export interface DiagnosticsDropInterval {
  metricId: string
  fromMs: number
  toMs: number
  durationMs: number
  dropPct: number
  baseline: number
  trough: number
  [key: string]: unknown
}

/** §22.2 皮尔逊相关系数行（区间内采样点对齐；样本对 <3 的指标不出行——不填假相关） */
export interface DiagnosticsCorrelation {
  metricId: string
  pearson: number
  sampleCount: number
  [key: string]: unknown
}

/** §22.2 候选原因（score 主导项 |pearson|，具体公式 M7 实现期定；rank 按 score 排序） */
export interface DiagnosticsCandidate {
  rank: number
  metricId: string
  score: number
  /** 服务端规则生成的确定性文本（非 LLM） */
  evidence: string
  [key: string]: unknown
}

/** §22.2 根因诊断卡数据面（对标 analyze_root_cause；锚指标无数据 → 三数组空 + note） */
export interface DiagnosticsResponse {
  runId: string
  anchor: string
  params: { dropThresholdPct: number; minDurationMs: number }
  dropIntervals: DiagnosticsDropInterval[]
  correlations: DiagnosticsCorrelation[]
  candidates: DiagnosticsCandidate[]
  note: string | null
  [key: string]: unknown
}

/** §22.3 POST /api/sessions/{sessionId}/timed-screenshot 请求体（intervalMs 缺省 2000，500–60000） */
export interface TimedScreenshotRequest {
  intervalMs?: number
}

/** §22.3 202 Accepted 回执（real-only；synthetic → 501 not_implemented + SSE 诊断） */
export interface TimedScreenshotResult {
  sessionId: string
  intervalMs: number
  state: string
  [key: string]: unknown
}

/* ---------------- §19 回放聚合（M4，[I§2.5][I§4.4][I§4.6]） ---------------- */

/** §19 GET /api/runs/{runId}/replay?maxPoints=600（一次拉取：series+markers+artifacts+clockAnchors+summary） */
export interface ReplayResponse {
  runId: string
  caseName: string
  /** 样本序列覆盖时长（全指标首尾点差；无样本 → 0） */
  durationMs: number
  /** §7 形状，覆盖全部已采集指标 */
  series: MetricSeries[]
  downsampled: boolean
  /** 触发降采样时附带（同 §7） */
  sampling?: string
  /** §15 sidecar 读回；无标记 → markers: [] */
  markers: MarkersPayload
  /** §17 索引元素原样（缩略图条/视频对齐的数据源） */
  artifacts: ArtifactEntry[]
  /** manifest context.clockAnchors 原样；无锚点 → null（视频 currentTime 映射必需，[R§2.9]） */
  clockAnchors: ClockAnchors | null
  /** 引用不内联：statisticsUrl 相对路径，客户端拼 baseUrl 后取 §12 统计（fpsGe 阈值由客户端附加） */
  summary: { summaryAvailable: boolean; statisticsUrl: string }
  [key: string]: unknown
}
