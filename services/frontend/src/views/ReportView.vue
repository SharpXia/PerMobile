<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useStore } from '../store'
import { metricMeta } from '../metricMeta'
import { cardTone, fpsTabCards, groupNote, type StatCardDef } from '../statCards'
import StatCardMatrix from '../components/StatCardMatrix.vue'
import type { StatCardVM } from '../components/StatCardMatrix.vue'
import MetricChart from '../components/MetricChart.vue'
import type { BrushRange, ChartMarker, ChartSeries } from '../components/MetricChart.vue'
import RangeStatCard from '../components/RangeStatCard.vue'
import LabelStatsTable from '../components/LabelStatsTable.vue'
import type { SegmentRowVM } from '../components/LabelStatsTable.vue'
import RootCauseCard from '../components/RootCauseCard.vue'
import { createRunArtifactsClient } from '../api-client/runArtifacts'
import type { MetricPoint, ReplayMarker, SessionSummary, StartupTiming, Statistics } from '../api-client'

/**
 * 报告页（R41，对标 [I§2.9] Web Case 报告页 + [I§4.4] 数值卡体系三层）：
 * - 首屏 = 汇总层数值卡矩阵（每指标 Tab 顶部，数据源 /statistics，契约 §12）；
 *   R31：fpsGe 阈值输入联动（请求参数 + cardTone 达标着色，阈值 = store.thresholds）；
 * - 其次 = Label 分段统计表（R32）：行 = All + 每个 label 段，列 = 全部统计指标，
 *   「参数显示设置」列勾选只影响显示不影响导出（导出走 M2 Excel 通道 [I§4.2]）；
 *   段区间 = 打标时刻 → 下次打标（[F§6]），每段一次 §12/§18 区间查询；
 * - 然后 = 全量曲线（R30 框选区间统计：MetricChart brush → §18 区间统计端点 →
 *   RangeStatCard，MonitorView 同款交互移植；选区跨图由共享 brushRange 同步渲染）；
 * - run 侧标注（§15.2 run 侧扩展 POST /runs/{id}/labels|notes|marks）：live 模式
 *   下报告页可添加备注/标记（页面局部状态，不走 store——runArtifacts.ts 独立客户端）；
 *   区间统计卡 Save 把区间统计写为一条 run 侧备注（[I§4.4] 框选即统计 + Save）。
 *
 * 两种数据源（契约 §12 两端点）：
 * - run：测试记录 → 查看报告（GET /api/runs/{id}/statistics + GET /api/runs/{id}）；
 *   标记读回 §15.3 sidecar（GET /api/runs/{id}/markers，无标记 → [] 是常态）；
 * - session：停止后保存前预览（GET /api/sessions/{id}/statistics，对标 PerfDog
 *   「停止→看统计→决定保存」[I§2.6]）；标记 = 会话内存标记（store.state.markers）。
 */
const store = useStore()

const source = computed(() => store.state.report)

/** run 侧标注客户端（baseUrl 同 store.state.baseUrl；mock 模式无 run 目录，入口由模板门控） */
const runArtifacts = createRunArtifactsClient(store.state.baseUrl)

const loading = ref(false)
const errorMsg = ref('')
const stats = ref<Statistics | null>(null)
const summary = ref<SessionSummary | null>(null)
const caseName = ref('')
const deviceText = ref('')
const pkgText = ref('')
const curveNote = ref('')
const curveSeries = ref<Record<string, MetricPoint[]>>({})
const activeTab = ref<'overview' | 'fps' | string>('overview')
/** §15 标记（run 来源 = sidecar 读回；session 来源 = 会话内存标记，由 reactive 派生） */
const runMarkers = ref<ReplayMarker[]>([])
/** §21.4（M6，R18）：run 启动耗时（TTID/TTFD 不可等价 → null + note 口径声明原样渲染） */
const startup = ref<StartupTiming | null>(null)
const startupError = ref('')
let loadSeq = 0

/* ---------------- R30：报告页框选区间统计（§18 区间统计端点，MonitorView 同款交互移植） ---------------- */

const brushRange = ref<BrushRange | null>(null)
const rangeStats = ref<Statistics | null>(null)
const rangeLoading = ref(false)
const rangeError = ref('')
const rangeSaveBusy = ref(false)
const rangeSavedText = ref('')
let rangeSeq = 0

async function fetchRangeStats(range: BrushRange): Promise<void> {
  const src = source.value
  if (!src) return
  const seq = ++rangeSeq
  rangeLoading.value = true
  rangeError.value = ''
  try {
    // §18：区间闭区间含端点；fpsGe 随 R31 阈值输入联动（占比行/列同步变化）
    const q = { fromMs: range.fromMs, toMs: range.toMs, fpsGe: store.state.thresholds.fpsGe }
    const s =
      src.kind === 'run' ? await store.getRunStatistics(src.runId, q) : await store.getSessionStatistics(src.sessionId, q)
    if (seq !== rangeSeq) return
    rangeStats.value = s
  } catch (err) {
    if (seq !== rangeSeq) return
    rangeStats.value = null
    rangeError.value = errText(err)
  } finally {
    if (seq === rangeSeq) rangeLoading.value = false
  }
}

function onBrushRange(range: BrushRange): void {
  brushRange.value = range
  rangeSavedText.value = ''
  void fetchRangeStats(range)
}

/** 清除选区 → 回全量视图（各图选区同步清除，seq 作废在途请求） */
function clearBrush(): void {
  brushRange.value = null
  rangeStats.value = null
  rangeError.value = ''
  rangeSavedText.value = ''
  rangeSeq += 1
}

/** R31：阈值变更 → 区间统计占比联动重取（选区打开时） */
watch(
  () => store.state.thresholds.fpsGe,
  () => {
    if (brushRange.value) void fetchRangeStats(brushRange.value)
  },
)

/** 相对时间戳 mm:ss.mmm（原点 = 曲线最早点；区间卡头与 Save 备注文案用） */
function fmtRel(ts: number): string {
  const origin = curveStartTs.value ?? 0
  const rel = Math.max(0, ts - origin)
  const ms = Math.floor(rel % 1000)
  const total = Math.floor(rel / 1000)
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}.${String(ms).padStart(3, '0')}`
}

/** 区间统计 → 备注文案（MonitorView saveRangeAsNote 同款 [I§4.4] 形态） */
function rangeNoteText(range: BrushRange, s: Statistics): string {
  const num = (v: number | null, digits = 1): string => (v === null || !Number.isFinite(v) ? '—' : v.toFixed(digits))
  const ge = Object.keys(s.fps.ge)
    .sort((a, b) => Number(a) - Number(b))
    .map((t) => `FPS>=${t} ${s.fps.ge[t] === null || s.fps.ge[t] === undefined ? '—' : `${num(s.fps.ge[t]!)}%`}`)
    .join('，')
  return `区间统计 ${fmtRel(range.fromMs)}–${fmtRel(range.toMs)}：Avg(FPS) ${num(s.fps.avg)}，Std ${num(s.fps.std)}，Min ${num(s.fps.min)}，${ge}，Jank ${num(s.jank.jank, 0)}，BigJank ${num(s.jank.big, 0)}，Stutter ${num(s.jank.stutter)}%`
}

/** 区间卡 Save 可用性：run 来源 = live 模式（run 侧标注可写）；session 来源 = 会话批注
 *  （store.addNote 自带 §15.2 状态门，saved/discarded 时置错不写） */
const canSaveRange = computed(() => source.value?.kind === 'run')

/** 区间卡 Save（[I§4.4] 框选即统计 + Save）：run 来源写 run 侧备注（§15.2 run 侧扩展，
 *  tsMs = 区间起点）；session 来源写会话内存批注（与 MonitorView 同语义） */
async function saveRangeAsNote(): Promise<void> {
  const src = source.value
  const range = brushRange.value
  const s = rangeStats.value
  if (!src || !range || !s || rangeSaveBusy.value) return
  const text = rangeNoteText(range, s)
  rangeSaveBusy.value = true
  try {
    if (src.kind === 'run') {
      await runArtifacts.createRunNote(src.runId, { tsMs: range.fromMs, text })
      rangeSavedText.value = fmtRel(range.fromMs)
      await refreshRunMarkers()
    } else {
      const created = await store.addNote({ tsMs: range.fromMs, text })
      if (created) rangeSavedText.value = fmtRel(created.tsMs)
    }
  } catch (err) {
    // 失败走顶部错误条（与 store.addNote 失败路径同通道；卡片内 errorMsg 仅承载区间统计失败）
    store.setError(`区间统计 Save 失败：${errText(err)}`)
  } finally {
    rangeSaveBusy.value = false
  }
}

/* ---------------- run 侧标注（§15.2 run 侧扩展 POST /runs/{id}/labels|notes|marks） ---------------- */

/** 入口可用性：run 数据源 + live 模式（mock 无后端 run 目录，runs 是 live 后端事实源 §14）；
 *  页面局部状态，不走 store（runArtifacts.ts 独立客户端，store/http/index 归他流） */
const annotationsAvailable = computed(() => source.value?.kind === 'run')

const annKind = ref<'note' | 'mark'>('note')
const annTsInput = ref('')
const annText = ref('')
const annBusy = ref(false)
const annError = ref('')
const annDoneText = ref('')

/** 时刻输入解析：`mm:ss(.mmm)`（相对曲线原点）/ 纯数字（epoch ms 直传）；空 = 缺省
 *  框选起点，再退曲线原点；无法解析/无原点 → null（不猜） */
function resolveAnnTsMs(): number | null {
  const raw = annTsInput.value.trim()
  if (!raw) return brushRange.value?.fromMs ?? curveStartTs.value ?? null
  const rel = /^(\d+):(\d{1,2})(?:\.(\d{1,3}))?$/.exec(raw)
  if (rel) {
    if (curveStartTs.value === undefined) return null
    const m = Number(rel[1])
    const s = Number(rel[2])
    const ms = rel[3] ? Number(rel[3].padEnd(3, '0')) : 0
    return curveStartTs.value + m * 60_000 + s * 1000 + ms
  }
  if (/^\d+$/.test(raw)) return Number(raw)
  return null
}

/** 提交 run 侧标注：note（文本）/ mark（红竖线）；成功后重取 §15.3 读回（图内 overlay 联动） */
async function submitAnnotation(): Promise<void> {
  const src = source.value
  if (src?.kind !== 'run' || annBusy.value) return
  const tsMs = resolveAnnTsMs()
  if (tsMs === null) {
    annError.value = '无法定位时刻 —— 输入 mm:ss（相对曲线原点）或 epoch ms；无曲线数据时请先框选区间或用 epoch ms'
    return
  }
  if (annKind.value === 'note' && !annText.value.trim()) {
    annError.value = '备注文字不能为空（契约 §15.2：空 text → 400）'
    return
  }
  annBusy.value = true
  annError.value = ''
  annDoneText.value = ''
  try {
    if (annKind.value === 'note') {
      await runArtifacts.createRunNote(src.runId, { tsMs, text: annText.value.trim() })
      annText.value = ''
    } else {
      await runArtifacts.createRunMark(src.runId, { tsMs })
    }
    annDoneText.value = `${annKind.value === 'note' ? '备注' : '标记'}已写入 run 侧标注（tsMs=${tsMs}）`
    await refreshRunMarkers()
  } catch (err) {
    annError.value = errText(err)
  } finally {
    annBusy.value = false
  }
}

/** §15.3 读回刷新（标注写入成功后调用；失败保持上一态不阻塞） */
async function refreshRunMarkers(): Promise<void> {
  const src = source.value
  if (src?.kind !== 'run') return
  try {
    const payload = await store.getRunMarkers(src.runId)
    runMarkers.value = payload.markers
  } catch {
    /* 读回失败不阻塞写入结果（图内 overlay 保持上一态） */
  }
}

/** §15 标记 → 图内 overlay（mark 红竖线 / label 色虚线 / note 点线，MetricChart M4 能力） */
const chartMarkers = computed<ChartMarker[]>(() =>
  sourceMarkers.value.map((m) => ({
    tsMs: m.tsMs,
    kind: m.kind,
    ...(m.color ? { color: m.color } : {}),
    ...(m.text ? { text: m.text } : {}),
  })),
)

const sourceMarkers = computed<ReplayMarker[]>(() =>
  source.value?.kind === 'run' ? runMarkers.value : store.state.markers,
)

watch(
  source,
  () => {
    activeTab.value = 'overview'
    runMarkers.value = []
    clearBrush() // 选区与区间统计随数据源切换作废（含在途请求 seq）
    annTsInput.value = ''
    annText.value = ''
    annError.value = ''
    annDoneText.value = ''
    void load()
  },
  { immediate: true },
)

async function load(): Promise<void> {
  const src = source.value
  if (!src) return
  const seq = ++loadSeq
  loading.value = true
  errorMsg.value = ''
  stats.value = null
  summary.value = null
  startup.value = null
  startupError.value = ''
  caseName.value = src.kind === 'run' ? (src.caseName ?? src.runId) : '会话统计预览'
  deviceText.value = ''
  pkgText.value = ''
  curveSeries.value = {}
  curveNote.value = ''
  try {
    // R31：fpsGe 阈值随 store.thresholds（图右上输入联动；服务端缺省 18,25 与默认值同源）
    if (src.kind === 'run') {
      stats.value = await store.getRunStatistics(src.runId, { fpsGe: store.state.thresholds.fpsGe })
      // §15.3：run 标记 sidecar 读回（无 markers.ndjson → [] 是常态，不是错误）
      store
        .getRunMarkers(src.runId)
        .then((p) => {
          if (seq === loadSeq) runMarkers.value = p.markers
        })
        .catch(() => {
          /* sidecar 读回失败不阻塞统计卡（分段表如实显示 All 行 + 提示） */
        })
      // run 详情可能 501（live 后端 §9 端点 R39/R40 未落地）——统计卡不受影响，如实降级
      try {
        const detail = await store.getRunDetail(src.runId)
        summary.value = detail.summary ?? null
        deviceText.value = detail.device ?? ''
        pkgText.value = detail.packageName ?? ''
        if (detail.sessionId) await loadCurves(detail.sessionId, seq)
        else setRunCurveNote()
      } catch (err) {
        setRunCurveNote(`run 详情不可用：${errText(err)}`)
      }
      // §21.4（R18）：启动耗时启动卡（run 侧端点；失败不阻塞统计卡，卡内如实显示错误）
      store
        .getRunStartup(src.runId)
        .then((r) => {
          if (seq === loadSeq) startup.value = r
        })
        .catch((err) => {
          if (seq === loadSeq) startupError.value = errText(err)
        })
    } else {
      stats.value = await store.getSessionStatistics(src.sessionId, { fpsGe: store.state.thresholds.fpsGe })
      caseName.value = `会话统计预览 · ${src.sessionId}`
      try {
        const sess = await store.getSessionDetail(src.sessionId)
        pkgText.value = sess.packageName
        deviceText.value = store.state.devices.find((d) => d.id === sess.deviceId)?.model ?? sess.deviceId
      } catch {
        /* 详情缺失不影响统计卡 */
      }
      try {
        summary.value = await store.getSessionSummary(src.sessionId)
      } catch {
        /* 无汇总不阻塞统计卡 */
      }
      await loadCurves(src.sessionId, seq)
    }
  } catch (err) {
    errorMsg.value = errText(err)
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

/** R31：阈值输入变更 → 重取统计（fpsGe 联动占比卡/分段占比列）+ 着色由 computed 自动跟随 */
watch(
  () => store.state.thresholds.fpsGe,
  () => {
    if (source.value) void load()
  },
)

function setRunCurveNote(prefix?: string): void {
  curveNote.value =
    (prefix ? `${prefix} ` : '') +
    '契约 §9 未提供 run 级时序数据读取端点（run 目录仅 manifest/summary/artifacts 索引），曲线区不可用；统计卡与 Sum 行不受影响。回放曲线属 R37（M2+）。'
}

async function loadCurves(sessionId: string, seq: number): Promise<void> {
  try {
    const win = await store.getMetricsWindow(sessionId, { maxPoints: 600 })
    if (seq !== loadSeq) return
    if (win.series.length === 0) {
      curveNote.value = '窗口内无时序数据（会话未产生采样点）'
      return
    }
    const next: Record<string, MetricPoint[]> = {}
    for (const s of win.series) next[s.metricId] = s.points
    curveSeries.value = next
    curveNote.value = win.downsampled
      ? `曲线经 LTTB 降采样至 ${win.series[0]?.points.length ?? 0} 点内（sampling=${win.sampling ?? 'lttb'}），统计卡为全量现算不受影响`
      : ''
  } catch (err) {
    if (seq === loadSeq) curveNote.value = `曲线数据不可用：${errText(err)}`
  }
}

function errText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}


/* ---------------- Tab 结构（[I§2.9] 指标 Tab：概览 | FPS | 按 summary 分组） ---------------- */

const GROUP_PREFIXES: Array<{ prefix: string; name: string }> = [
  { prefix: 'cpu.', name: 'CPU' },
  { prefix: 'memory.', name: 'Memory' },
  { prefix: 'gpu.', name: 'GPU' },
  { prefix: 'net.', name: 'Network' },
  { prefix: 'power.', name: 'Power' },
  { prefix: 'thermal.', name: 'Temperature' },
]

/**
 * summary.metrics 两种落点形状归一（如实注明，兼容修复）：
 * - run 详情（§9）summary = perfeval.summary.v1 原样透传，metrics 为 **dict**
 *   （metricId → 统计块，字段 mean/n/sampleCount/min/max/unit）；
 * - session summary 端点为 **数组**（SessionSummary 声明形状）。
 * 消费侧统一归一为数组（perfeval summary_report._normalize_metrics 同款兼容思路），
 * dict 形态按字段映射（mean→avg、sampleCount/n→count），缺失字段如实缺省。
 */
type SummaryMetricRow = NonNullable<SessionSummary['metrics']>[number]

function normalizeSummaryMetrics(s: SessionSummary | null | undefined): SummaryMetricRow[] {
  const raw = s?.metrics
  if (Array.isArray(raw)) return raw
  if (raw && typeof raw === 'object') {
    return Object.entries(raw as Record<string, Record<string, unknown>>).map(([metricId, m]) => ({
      metricId,
      unit: typeof m.unit === 'string' ? m.unit : undefined,
      count: typeof m.sampleCount === 'number' ? m.sampleCount : typeof m.n === 'number' ? m.n : undefined,
      avg: typeof m.mean === 'number' ? m.mean : null,
      min: typeof m.min === 'number' ? m.min : null,
      max: typeof m.max === 'number' ? m.max : null,
      latest: typeof m.latest === 'number' ? m.latest : null,
    }))
  }
  return []
}

const tabs = computed<Array<{ id: string; name: string }>>(() => {
  const list = [{ id: 'overview', name: '概览' }, { id: 'fps', name: 'FPS' }]
  const metrics = normalizeSummaryMetrics(summary.value)
  for (const g of GROUP_PREFIXES) {
    if (metrics.some((m) => m.metricId.startsWith(g.prefix))) list.push({ id: g.prefix, name: g.name })
  }
  return list
})

function fmtNum(v: number): string {
  if (Math.abs(v) >= 1000) return v.toFixed(0)
  if (Number.isInteger(v)) return String(v)
  return v.toFixed(1)
}

function toVM(def: StatCardDef, s: Statistics): StatCardVM {
  const value = def.read(s)
  const note = value === null ? groupNote(s, def) : undefined
  return {
    key: def.key,
    label: def.label,
    unit: def.unit,
    value: value === null ? '—' : fmtNum(value),
    tooltip: note ? `${def.tooltip}\n当前不可计算：${note}` : def.tooltip,
    approx: def.approx,
    dim: value === null,
    // R31：阈值联动达标着色（fps/fTime 族口径可判定才着色；statCards.cardTone）
    tone: cardTone(def, value, store.state.thresholds),
  }
}

/** FPS Tab 口径声明行（契约 §12 规则 1/2：note 原文可见携带；R31 阈值联动口径可见） */
const fpsNoteLines = computed<string[]>(() => {
  const s = stats.value
  if (!s) return []
  const lines: string[] = []
  if (s.fps.note) lines.push(s.fps.note)
  if (s.smooth.note) lines.push(`Smooth：${s.smooth.note}`)
  const t = store.state.thresholds
  lines.push(`阈值口径：FPS>=${t.fpsGe.join('/')}[%]、FTime>=${t.ftimeMs}ms（图右上输入可调，「重置」还原统计口径建议值，刷新还原默认）`)
  return lines
})

const fpsCards = computed<StatCardVM[]>(() => (stats.value ? fpsTabCards(stats.value).map((d) => toVM(d, stats.value!)) : []))

/* ---------------- R32：Label 分段统计（行 = All + label 段；每段一次 §12/§18 区间查询） ---------------- */

/** label 段（段起点 = 打标时刻，段终点 = 下次打标；末段 toMs 省略 = 至数据结束，§18 两参数各自可选） */
interface LabelSeg {
  name: string
  color?: string
  fromMs: number
  toMs?: number
}

const LABEL_PALETTE = ['#e5534b', '#a371f7', '#2f81f7', '#3fb950', '#d29922']

const labelSegs = computed<LabelSeg[]>(() => {
  const labels = sourceMarkers.value.filter((m) => m.kind === 'label').sort((a, b) => a.tsMs - b.tsMs)
  const segs: LabelSeg[] = []
  for (let i = 0; i < labels.length; i++) {
    const m = labels[i]
    if (!m) continue
    const next = labels[i + 1]?.tsMs
    segs.push({
      name: m.name ?? `label${i + 1}`,
      color: m.color ?? LABEL_PALETTE[i % LABEL_PALETTE.length],
      fromMs: m.tsMs,
      ...(next !== undefined ? { toMs: next } : {}),
    })
  }
  return segs
})

const segmentRows = ref<SegmentRowVM[]>([])

/** 由 All 行 + label 段构建分段行并逐段拉取区间统计（seq 守卫防切页竞态） */
async function buildSegmentRows(seq: number): Promise<void> {
  const src = source.value
  const base = stats.value
  if (!src || !base) {
    segmentRows.value = []
    return
  }
  // All 行 = 全量统计（已取得）；无段时也保留 All 行（PerfDog 同形态 [I§2.9]）
  segmentRows.value = [
    { name: 'All', stats: base, loading: false, errorMsg: '' },
    ...labelSegs.value.map((seg) => ({ name: seg.name, color: seg.color, fromMs: seg.fromMs, toMs: seg.toMs, stats: null, loading: true, errorMsg: '' })),
  ]
  const fetchOne = async (seg: LabelSeg): Promise<Statistics | null> => {
    const q = { fromMs: seg.fromMs, toMs: seg.toMs, fpsGe: store.state.thresholds.fpsGe }
    return src.kind === 'run' ? store.getRunStatistics(src.runId, q) : store.getSessionStatistics(src.sessionId, q)
  }
  await Promise.all(
    labelSegs.value.map(async (seg, i) => {
      try {
        const s = await fetchOne(seg)
        if (seq !== loadSeq) return
        const row = segmentRows.value[i + 1]
        if (row) {
          row.stats = s
          row.loading = false
        }
      } catch (err) {
        if (seq !== loadSeq) return
        const row = segmentRows.value[i + 1]
        if (row) {
          row.loading = false
          row.errorMsg = errText(err)
        }
      }
    }),
  )
}

watch([stats, labelSegs], () => {
  void buildSegmentRows(loadSeq)
})

/** 概览 Tab：统计量要点卡（statistics 层） */
const overviewStatCards = computed<StatCardVM[]>(() => {
  const s = stats.value
  if (!s) return []
  const defs: StatCardDef[] = [
    { key: 'o.avg', label: 'Avg(FPS)', unit: 'fps', tooltip: 'fps 点列的均值 [F§3.1]', read: (x) => x.fps.avg },
    { key: 'o.smooth', label: 'Smooth(稳帧指数)', unit: '', tooltip: '近似口径：官方未公开算法公式 [F§3.5][F§10.1]（详见 FPS Tab 声明）', approx: true, read: (x) => x.smooth.value },
    { key: 'o.p1', label: '1%Low(FPS)', unit: 'fps', tooltip: '帧耗时最长 1% 的均值换算 fps [F§3.5]', read: (x) => x.lowFps.p1 },
    { key: 'o.per10min', label: 'Jank(/10min)', unit: '/10min', tooltip: 'Jank 次数 × 600000 / durationMs [F§3.3]', read: (x) => x.jank.per10min },
    { key: 'o.std', label: 'Std(FPS)', unit: 'fps', tooltip: 'fps 点列标准差 [F§3.1]', read: (x) => x.fps.std },
    { key: 'o.drop', label: 'Drop(FPS)', unit: '/h', tooltip: '相邻点下降 >8 帧次数 × 3600000 / durationMs [F§3.1]', read: (x) => x.fps.drop },
  ]
  return defs.map((d) => toVM(d, s))
})

/** 概览/分组 Tab 的 summary 口径卡（perfeval.summary.v1 原样透传层，非 §12 统计层） */
const SUMMARY_SCOPE_TIP = '来自 perfeval.summary.v1（§10 只读透传层）的采样点统计；perfdog.statistics.v1（§12）仅覆盖 FPS/帧族'

function summaryCards(prefix: '' | string): StatCardVM[] {
  const metrics = normalizeSummaryMetrics(summary.value)
  const picked = prefix ? metrics.filter((m) => m.metricId.startsWith(prefix)) : metrics
  return picked.map((m) => {
    const meta = metricMeta(m.metricId)
    const parts: string[] = []
    if (m.count !== undefined) parts.push(`样本数 ${m.count}`)
    if (m.min !== null && m.min !== undefined) parts.push(`min ${m.min}`)
    if (m.max !== null && m.max !== undefined) parts.push(`max ${m.max}`)
    return {
      key: `sum.${m.metricId}`,
      label: meta.label,
      unit: m.unit ?? '',
      value: m.avg === null || m.avg === undefined ? '—' : `均值 ${fmtNum(m.avg)}`,
      tooltip: `${SUMMARY_SCOPE_TIP}\n${meta.desc}${parts.length ? `\n${parts.join(' · ')}` : ''}`,
      dim: m.avg === null || m.avg === undefined,
    }
  })
}

const overviewSummaryCards = computed<StatCardVM[]>(() => summaryCards(''))

const activeSummaryCards = computed<StatCardVM[]>(() => {
  const id = activeTab.value
  if (id === 'overview') return overviewSummaryCards.value
  if (GROUP_PREFIXES.some((g) => g.prefix === id)) return summaryCards(id)
  return []
})

const activeNoteLines = computed<string[]>(() => {
  if (activeTab.value === 'fps') return fpsNoteLines.value
  if (activeTab.value === 'overview') {
    const s = stats.value
    return s?.smooth.note ? [`Smooth：${s.smooth.note}`] : []
  }
  return []
})

/* ---------------- 全量曲线（第三层 [I§4.4]） ---------------- */

const curveCharts = computed<ChartSeries[]>(() =>
  Object.entries(curveSeries.value).map(([metricId, points]) => {
    const meta = metricMeta(metricId)
    const def = store.state.metricDefs.find((d) => d.metricId === metricId)
    return { metricId, label: meta.label, unit: def?.unit ?? '', color: meta.color, points }
  }),
)

const curveStartTs = computed<number | undefined>(() => {
  let min = Infinity
  for (const pts of Object.values(curveSeries.value)) for (const p of pts) if (p.ts < min) min = p.ts
  return Number.isFinite(min) ? min : undefined
})

/* ---------------- 头部信息 ---------------- */

const durationText = computed(() => {
  const ms = stats.value?.durationMs ?? 0
  const sec = Math.round(ms / 1000)
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`
})

const basisText = computed(() => {
  const s = stats.value
  if (!s) return ''
  const basis: Record<string, string> = {
    frameEvents: '逐帧事件基线',
    fpsSeries: 'FPS 轮询点列基线',
    none: '无帧数据基线',
  }
  const interval = s.frameSeriesIntervalMs === null ? '' : ` · ${s.frameSeriesIntervalMs}ms 节拍`
  return `${basis[s.frameDataBasis] ?? s.frameDataBasis}${interval}`
})

const basisCls = computed(() => {
  switch (stats.value?.frameDataBasis) {
    case 'frameEvents':
      return 'green'
    case 'fpsSeries':
      return 'blue'
    default:
      return 'orange'
  }
})

function onExport(): void {
  const src = source.value
  if (src?.kind === 'run') void store.exportRun(src.runId)
}

/** 报告页 → 回放（M4 R37，[I§2.5] 第二入口：同布局切换数据源到该 run） */
function openReplay(): void {
  const src = source.value
  if (src?.kind === 'run') store.openReplay(src.runId, src.caseName)
}

/** 报告页 → 深度分析（M7 R50，[I§2.7] 入口一：帧时间线/单帧卡/CPU 调度 + R51 根因卡） */
function openDeep(): void {
  const src = source.value
  if (src?.kind === 'run') store.openDeep(src.runId, src.caseName)
}

/** R51 锚指标候选（诊断卡 anchor 下拉；缺省 FPS 均值 = 契约默认锚） */
const DIAGNOSTIC_ANCHORS = ['frame.fps.mean', 'cpu.total.pct', 'cpu.proc.pct', 'memory.pss.peak_mb']
</script>

<template>
  <div class="report-wrap">
    <div class="report-head">
      <button class="ghost" @click="store.closeReport()">← 返回记录</button>
      <strong class="report-title">{{ caseName }}</strong>
      <span v-if="deviceText" class="chip gray">{{ deviceText }}</span>
      <span v-if="pkgText" class="chip gray mono">{{ pkgText }}</span>
      <span v-if="stats" class="chip gray">时长 {{ durationText }}</span>
      <span v-if="basisText" class="chip" :class="basisCls" data-tip="契约 §12 frameDataBasis：统计量计算的数据基线与 fps 点列节拍">
        {{ basisText }}
      </span>
      <span style="flex: 1"></span>
      <button
        v-if="source?.kind === 'run'"
        class="mini"
        title="回放视图（M4 R37，[I§2.5]）：主窗口复用 + 缩略图时间条 + 悬停单点卡 + ←/→ 逐采样点"
        @click="openReplay"
      >
        回放
      </button>
      <button
        v-if="source?.kind === 'run'"
        class="mini"
        title="深度分析（M7 R50，[I§2.7]）：帧时间线 Overview / 单帧卡 / CPU 调度条带 / M 键定位 + 根因诊断卡"
        @click="openDeep"
      >
        深度分析
      </button>
      <button v-if="source?.kind === 'run'" @click="onExport">导出全部 (excel)</button>
    </div>

    <div v-if="loading" class="empty-hint">统计计算中…（/statistics 现算）</div>
    <div v-else-if="errorMsg" class="empty-hint">统计加载失败：{{ errorMsg }}</div>

    <template v-else-if="stats">
      <!-- 首屏：指标 Tab + 数值卡矩阵（[I§2.9] 指标 Tab / [I§4.4] 汇总层） -->
      <nav class="report-tabs">
        <span
          v-for="t in tabs"
          :key="t.id"
          class="rtab"
          :class="{ active: activeTab === t.id }"
          @click="activeTab = t.id"
        >
          {{ t.name }}
        </span>
      </nav>

      <StatCardMatrix
        v-if="activeTab === 'fps'"
        :cards="fpsCards"
        :note-lines="fpsNoteLines"
      />
      <template v-else-if="activeTab === 'overview'">
        <div class="report-sub">统计量要点（perfdog.statistics.v1 · /statistics）</div>
        <StatCardMatrix :cards="overviewStatCards" :note-lines="activeNoteLines" />
        <!-- R18：启动卡（§21.4，run 侧端点；TTID/TTFD null = 口径不可等价，note 原样渲染） -->
        <div class="report-sub" style="margin-top: 14px">启动耗时 StartupTiming（R18 · §21.4）</div>
        <div v-if="startupError" class="stat-note">启动耗时不可用：{{ startupError }}</div>
        <div v-else-if="startup" class="startup-card">
          <div class="sc-cell" title="am start -W 的 TotalTime（整数毫秒；会话期启动测量落盘于 run 产物）">
            <div class="sc-label">TotalTime（am start -W）</div>
            <div class="sc-value" :class="{ dim: startup.totalTimeMs === null }">
              {{ startup.totalTimeMs === null ? '—' : `${startup.totalTimeMs} ms` }}
            </div>
            <div v-if="startup.totalTimeMs === null" class="sc-null-note">无启动测量数据（200 常态，非错误）</div>
          </div>
          <div class="sc-cell" title="TTID：启动到第一帧显示（FrameMetrics/drawn 回调链未落地 → 不可等价实现为 null，非 0）">
            <div class="sc-label">TTID（首屏）</div>
            <div class="sc-value dim">—</div>
            <div class="sc-null-note">口径不可等价：null ≠ 0，数值与 PerfDog 不可直接对比</div>
          </div>
          <div class="sc-cell" title="TTFD：主屏可交互（需应用代码调用 reportFullyDrawn()，[F§3.11]；未落地 → null）">
            <div class="sc-label">TTFD（主屏）</div>
            <div class="sc-value dim">—</div>
            <div class="sc-null-note">口径不可等价：需 reportFullyDrawn() 回调链，未落地</div>
          </div>
        </div>
        <div v-if="startup?.note" class="stat-note" style="margin-top: 6px">※ {{ startup.note }}</div>
        <div v-else-if="!startup && !startupError" class="stat-note">启动耗时加载中…</div>
        <div class="report-sub" style="margin-top: 14px">采样点汇总（perfeval.summary.v1 · summary 口径）</div>
        <StatCardMatrix :cards="overviewSummaryCards" />
        <div v-if="overviewSummaryCards.length === 0" class="empty-hint">
          无 summary 数据（live 后端 §9 run 详情端点未落地或该记录无汇总）。
        </div>
      </template>
      <template v-else>
        <div class="report-sub">{{ activeTab }} 指标（summary 口径；该族的 PerfDog 统计量属 §12 FPS/帧族之外口径）</div>
        <StatCardMatrix :cards="activeSummaryCards" />
        <div v-if="activeSummaryCards.length === 0" class="empty-hint">该 Tab 无采样点汇总数据。</div>
      </template>

      <!-- 其二：Label 分段统计表（R32，[I§2.9]）：行 = All + label 段，列可勾选显隐（只影响显示） -->
      <div class="report-section">
        <div class="report-sub">
          Label 分段统计
          <span class="hint">（段 = 打标时刻 → 下次打标 [F§6]；每段一次 §12/§18 区间查询；列显隐只影响显示不影响导出）</span>
        </div>
        <LabelStatsTable :rows="segmentRows" />
        <div v-if="segmentRows.length > 0 && labelSegs.length === 0" class="stat-note" style="margin-top: 4px">
          ※ 该记录无 label 段标记 —— 仅显示 All 行（§15：无标记是常态；测试期打标见监控/回放页 R28）。
        </div>
      </div>

      <!-- 其三：全量曲线（R30 框选区间统计：brush → §18 区间端点 → RangeStatCard，
           选区跨图由共享 brushRange 同步渲染；清除选区回全量视图） -->
      <div class="report-section">
        <div class="report-sub">
          全量曲线
          <span class="hint">（横向拖出选区 = 区间统计 · 图内标记 = §15 sidecar/会话标记 · 清除选区回全量）</span>
        </div>
        <div v-if="curveNote" class="stat-note" style="margin-bottom: 8px">※ {{ curveNote }}</div>
        <div v-if="curveCharts.length > 0" class="chart-pages" style="padding: 0; position: relative">
          <MetricChart
            v-for="s in curveCharts"
            :key="s.metricId"
            :title="s.label"
            :series="[s]"
            :start-ts="curveStartTs"
            :window-ms="Number.MAX_SAFE_INTEGER"
            :height="200"
            brush-enabled
            :brush-range="brushRange"
            :markers="chartMarkers"
            @brush-range="onBrushRange"
          />
          <!-- R30：区间统计卡（选区后浮出；Save 写备注：run 来源=run 侧标注 / session 来源=会话批注） -->
          <div v-if="brushRange !== null" class="range-card-pos">
            <RangeStatCard
              :from-ms="brushRange.fromMs"
              :to-ms="brushRange.toMs"
              :origin-ts="curveStartTs"
              :stats="rangeStats"
              :loading="rangeLoading"
              :error-msg="rangeError"
              :can-save="canSaveRange"
              :save-busy="rangeSaveBusy"
              :saved-text="rangeSavedText"
              :fps-note="rangeStats?.fps.note"
              @save="saveRangeAsNote"
              @close="clearBrush"
            />
          </div>
        </div>
        <div v-else class="empty-hint">无曲线数据。</div>
      </div>

      <!-- 其四：run 侧标注（§15.2 run 侧扩展 POST /runs/{id}/labels|notes|marks；
           live 模式 run 数据源可用；页面局部状态，写入后 §15.3 读回 → 图内 overlay 联动） -->
      <div v-if="annotationsAvailable" class="report-section">
        <div class="report-sub">
          添加备注 / 标记
          <span class="hint">（§15.2 run 侧扩展：POST /runs/{id}/notes|marks → markers.ndjson 增量追加；重开报告仍可读）</span>
        </div>
        <div class="ann-form">
          <label class="ann-kind">
            <input v-model="annKind" type="radio" value="note" /> 备注
          </label>
          <label class="ann-kind">
            <input v-model="annKind" type="radio" value="mark" /> 标记
          </label>
          <input
            v-model="annTsInput"
            class="ann-ts"
            type="text"
            placeholder="时刻 mm:ss（相对曲线原点）/ epoch ms / 留空=选区起点"
            title="解析规则：mm:ss(.mmm) 相对曲线原点；纯数字按 epoch ms 直传；留空取框选起点，再退曲线原点"
            @keyup.enter="submitAnnotation"
          />
          <input
            v-if="annKind === 'note'"
            v-model="annText"
            class="ann-text"
            type="text"
            maxlength="200"
            placeholder="备注文字（§15.2 非空校验）"
            @keyup.enter="submitAnnotation"
          />
          <button class="mini primary" :disabled="annBusy" @click="submitAnnotation">
            {{ annBusy ? '写入中…' : '写入' }}
          </button>
        </div>
        <div v-if="annError" class="stat-note" style="color: #f85149; margin-top: 6px">※ {{ annError }}</div>
        <div v-else-if="annDoneText" class="stat-note" style="margin-top: 6px">※ {{ annDoneText }}</div>
        <div v-if="sourceMarkers.length === 0" class="stat-note" style="margin-top: 4px">
          ※ 该记录暂无标注（§15：无标记是常态；label 打标属会话期操作，run 侧仅备注/标记追加）。
        </div>
      </div>

      <!-- 其四：根因诊断卡（M7 R51，契约 §22.2；点击下降区间 → 回放定位复用 M4 时间定位） -->
      <div v-if="source?.kind === 'run'" class="report-section">
        <div class="report-sub">
          根因诊断（R51 · /diagnostics）
          <span class="hint">（下降区间 + 指标相关系数 + 候选原因 Top3；AI 叙述无 LLM key 时为占位不伪造）</span>
        </div>
        <RootCauseCard
          :run-id="source.runId"
          :case-name="source.caseName"
          :anchors="DIAGNOSTIC_ANCHORS"
          :origin-ts="curveStartTs ?? null"
        />
      </div>
    </template>
  </div>
</template>

<style scoped>
.report-wrap {
  padding: 12px 14px;
}

.report-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.report-title {
  font-size: 14px;
}

.report-tabs {
  display: flex;
  gap: 2px;
  margin-bottom: 10px;
  border-bottom: 1px solid var(--border);
}

.rtab {
  padding: 5px 14px;
  cursor: pointer;
  color: var(--text-dim);
  border: 1px solid transparent;
  border-bottom: none;
  border-radius: 6px 6px 0 0;
  user-select: none;
  font-size: 12.5px;
}

.rtab.active {
  color: var(--text);
  background: var(--bg-panel-2);
  border-color: var(--border);
}

.report-sub {
  font-size: 11px;
  color: var(--text-faint);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 8px;
}

.report-section {
  margin-top: 18px;
}

/* R18 启动卡（TTID/TTFD null 时显示「口径不可等价」说明） */
.startup-card {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

.sc-cell {
  flex: 1;
  min-width: 180px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px 12px;
}

.sc-label {
  font-size: 11px;
  color: var(--text-dim);
  margin-bottom: 4px;
}

.sc-value {
  font-size: 22px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.sc-value.dim {
  color: var(--text-faint);
}

.sc-null-note {
  font-size: 10.5px;
  color: var(--warn);
  margin-top: 4px;
}

/* R30：区间统计卡浮出定位（MonitorView .range-card-pos 同款） */
.range-card-pos {
  position: absolute;
  top: 8px;
  right: 8px;
  z-index: 7;
  width: 560px;
  max-width: min(92%, 640px);
}

/* §15.2 run 侧扩展：报告页添加备注/标记表单（页面局部状态，不走 store） */
.ann-form {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.ann-kind {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--text-dim);
  cursor: pointer;
}

.ann-kind input {
  accent-color: var(--accent);
}

.ann-ts {
  width: 250px;
  font-size: 12px;
}

.ann-text {
  flex: 1;
  min-width: 200px;
  font-size: 12px;
}
</style>
