<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useStore, COMPARE_POOL_MAX } from '../store'
import MetricChart from '../components/MetricChart.vue'
import type { ChartSeries } from '../components/MetricChart.vue'
import type {
  CaseRecord,
  CompareBudget,
  CompareBudgetMetric,
  CompareResponse,
  MetricCompare,
  MetricPoint,
  Statistics,
} from '../api-client'

/**
 * 对比报告页（R43，[I§2.9] 对比报告页 web_cmp_page + [F§8]）：
 * - 顶部对比池卡槽：≤5 条（上限由前端执行，契约 §9 M3 扩展），空槽提示「添加用例」；
 * - 页头标注可比前提：同设备 / 同场景 / 同时长 / 同初始状态（[F§8]），并按池内实际
 *   数据给出设备/应用/时长一致性提示（场景与初始状态无元数据，提示人工确认）；
 * - 概览对比表（行=用例，列=统计指标）；
 * - 预算门禁判定区（§9 S3 扩展，compare-two-groups-only / compare-budget）：
 *   对比池全量送判定引擎（首条=全局基线组，其余各成一组 N 路 2–10 组），
 *   perfeval.compare.v1 顶层结论 + 引擎全指标交集逐指标矩阵（含 memory/cpu/
 *   power/net/thermal 等全部指标）+ 逐对 pairs/perGroup；预算编辑入口逐指标
 *   warn/fail（百分比小数随请求透传，defaults 单 run/组为池语义）；
 * - 每指标区：指标切换 FPS | Jank；FPS = 曲线（frame.fps.mean 序列），Jank = Jank 族
 *   统计小表（逐帧判定需逐帧数据，fpsSeries 基线不可计算 —— 如实声明，不伪造曲线）；
 * - 两种时间对齐（[I§2.9]）：
 *   · 自由拖动：按住某条用例曲线左键水平拖动 → 该用例 offsetMs 变化，作用于全部图表
 *     （所有图共用同一 offsets 状态渲染）；
 *   · 相同时间对齐 ☐：各用例时间轴归一到自身起点（x = ts − 首点），且全部图表 X 轴
 *     统一固定域 [0, max(最长用例时长, 曲线实际跨度)]——以时间最长的 case 为基准拉长
 *     （[I§2.9]；经 MetricChart axisMinTs/axisMaxTs 显式轴域实现），短用例曲线左置、
 *     轴不截断数据；拖动对齐此时禁用（pan-enabled 关闭）。
 *
 * 曲线数据边界（2026-11-06 体检 compare-curve-unwired 更新）：
 * 主路径 = §19 run 级回放端点 /runs/{id}/replay（后端 routers/run_artifacts.py 与 mock
 * 均已实装）——读 run 目录 samples.ndjson 降采样序列，后端重启或会话销毁后仍可用；
 * 回退路径 = RunDetail.sessionId → 会话窗口（契约 §7），覆盖旧 run 无样本但来源会话
 * 缓冲仍可用的情形；两路均不可得时曲线区如实说明，统计表不受影响。
 */
const store = useStore()

const FPS_METRIC = 'frame.fps.mean'
const PALETTE = ['#4fc3f7', '#ffb74d', '#81c784', '#ba68c8', '#f06292']

const metricFamily = ref<'fps' | 'jank'>('fps')
const sameAlign = ref(false)
const stats = ref<Record<string, Statistics>>({})
const statsLoading = ref(false)
const statsError = ref('')
const curves = ref<Record<string, MetricPoint[]>>({})
const curveNote = ref('')
const offsets = ref<Record<string, number>>({})

const pool = computed<CaseRecord[]>(() => store.state.comparePool)

/* ---- 预算门禁判定（§9 S3：N 路 + budget）---- */

const verdict = ref<CompareResponse | null>(null)
const verdictLoading = ref(false)
const verdictError = ref('')
const budgetOpen = ref(false)
/** 逐指标预算草稿（% 表示，随「运行判定」换算为小数透传；空串=未设） */
const budgetDraft = ref<Record<string, { warnPct: string; failPct: string }>>({})

onMounted(() => {
  void reload()
})

watch(
  () => pool.value.map((c) => c.runId).join(','),
  () => void reload(),
)

async function reload(): Promise<void> {
  statsLoading.value = true
  statsError.value = ''
  stats.value = {}
  curves.value = {}
  curveNote.value = ''
  const runIds = pool.value.map((c) => c.runId)
  if (runIds.length === 0) {
    statsLoading.value = false
    return
  }
  // 统计（§12 /runs/{id}/statistics）：对比表与对齐跨度都依赖它
  const results = await Promise.allSettled(runIds.map((id) => store.getRunStatistics(id)))
  const next: Record<string, Statistics> = {}
  const errs: string[] = []
  results.forEach((r, i) => {
    const id = runIds[i]!
    if (r.status === 'fulfilled') next[id] = r.value
    else errs.push(`${pool.value[i]!.caseName}: ${errText(r.reason)}`)
  })
  stats.value = next
  if (errs.length > 0) statsError.value = errs.join('；')
  statsLoading.value = false
  await loadCurves(runIds)
}

/** 单用例 FPS 点列：主路径 §19 run 级回放（后端重启/会话销毁后仍可用），回退会话窗口（§7） */
async function curvePointsFor(id: string): Promise<MetricPoint[]> {
  let replayNote = ''
  try {
    const replay = await store.getRunReplay(id, 600)
    const pts = replay.series.find((s) => s.metricId === FPS_METRIC)?.points ?? []
    if (pts.length > 0) return pts
    replayNote = '回放无 FPS 样本'
  } catch (err) {
    replayNote = `回放读取失败（${errText(err)}）`
  }
  // 回退：来源会话窗口（旧 run 无样本但会话缓冲仍可用的情形）
  const detail = await store.getRunDetail(id)
  if (!detail.sessionId) throw new Error(`${replayNote}，且无来源会话可回退`)
  const win = await store.getMetricsWindow(detail.sessionId, { metricIds: [FPS_METRIC], maxPoints: 600 })
  const pts = win.series[0]?.points ?? []
  if (pts.length === 0) throw new Error(`${replayNote}，会话窗口内亦无 FPS 点列`)
  return pts
}

/** 曲线：§19 /runs/{id}/replay 主路径 + §7 会话窗口回退；两路均不可得即如实声明 */
async function loadCurves(runIds: string[]): Promise<void> {
  const next: Record<string, MetricPoint[]> = {}
  const notes: string[] = []
  for (let i = 0; i < runIds.length; i++) {
    const id = runIds[i]!
    try {
      next[id] = await curvePointsFor(id)
    } catch (err) {
      notes.push(`${pool.value[i]?.caseName ?? id}：${errText(err)}`)
    }
  }
  curves.value = next
  const unavailable = runIds.filter((id) => !next[id])
  if (unavailable.length === runIds.length && runIds.length > 0) {
    curveNote.value = `曲线不可用 —— ${notes.join('；')}。曲线主路径为 run 级回放端点（§19 /runs/{id}/replay），回退来源会话窗口（§7）；统计对比不受影响。`
  } else if (unavailable.length > 0) {
    curveNote.value = `部分用例曲线不可用：${notes.join('；')}`
  } else if (notes.length === 0) {
    curveNote.value = ''
  }
}

/* ---- 可比前提（[F§8]）---- */

const premiseDevices = computed(() => new Set(pool.value.map((c) => c.device)).size)
const premisePackages = computed(() => new Set(pool.value.map((c) => c.packageName ?? '')).size)
const premiseDurations = computed<number[]>(() =>
  pool.value.map((c) => stats.value[c.runId]?.durationMs ?? 0).filter((d) => d > 0),
)
const durationSpread = computed<number | null>(() => {
  const ds = premiseDurations.value
  if (ds.length < 2) return null
  const max = Math.max(...ds)
  const min = Math.min(...ds)
  return max > 0 ? (max - min) / max : null
})

/** 池内可自动核对的前提（同设备/同应用/时长差 ≤10%）；场景/初始状态无元数据 → 人工确认提示 */
const premiseIssues = computed<string[]>(() => {
  const issues: string[] = []
  if (pool.value.length >= 2) {
    if (premiseDevices.value > 1) issues.push('设备不一致')
    if (premisePackages.value > 1) issues.push('应用（包名）不一致')
    if (durationSpread.value !== null && durationSpread.value > 0.1) issues.push('时长差异超过 10%')
  }
  return issues
})

/* ---- 时间对齐（两种，作用于全部图表） ---- */

function firstTs(id: string): number | null {
  const pts = curves.value[id]
  return pts && pts.length > 0 ? pts[0]!.ts : null
}

/** 生效偏移：自由拖动=手动 offset；相同时间对齐=各用例归一到自身起点（拖动禁用） */
function effectiveOffset(id: string): number {
  if (sameAlign.value) return -(firstTs(id) ?? 0)
  return offsets.value[id] ?? 0
}

function shifted(id: string): MetricPoint[] {
  const off = effectiveOffset(id)
  return (curves.value[id] ?? []).map((p) => ({ ts: p.ts + off, value: p.value }))
}

/** 公共时间原点：自由模式下取各用例平移后最早首点（所有图同一 mm:ss 轴） */
const commonOrigin = computed<number | null>(() => {
  let min = Infinity
  for (const c of pool.value) {
    const pts = shifted(c.runId)
    if (pts.length > 0 && pts[0]!.ts < min) min = pts[0]!.ts
  }
  return Number.isFinite(min) ? min : null
})

/** 相同时间对齐下 X 轴跨度=最长用例时长（「以时间最长的case为基准拉长」[I§2.9]） */
const baselineCaseId = computed<string | null>(() => {
  let best: string | null = null
  let bestDur = -1
  for (const c of pool.value) {
    const d = stats.value[c.runId]?.durationMs ?? 0
    if (d > bestDur) {
      bestDur = d
      best = c.runId
    }
  }
  return best
})

/**
 * 相同时间对齐下统一 X 轴域上界：max(最长用例时长(§12 统计), 各曲线平移后实际末点)。
 * 归一后各曲线首点均为 0（commonOrigin=0），故取 max 保证「拉长到最长」且不截断数据；
 * 非 sameAlign 恒 null（图表按数据自适应）。
 */
const alignAxisMax = computed<number | null>(() => {
  if (!sameAlign.value) return null
  let max = maxDurationMs.value
  for (const c of pool.value) {
    const pts = shifted(c.runId)
    if (pts.length > 0) max = Math.max(max, pts[pts.length - 1]!.ts)
  }
  return max > 0 ? max : null
})

function onPan(caseId: string, deltaMs: number): void {
  if (sameAlign.value) return
  offsets.value[caseId] = (offsets.value[caseId] ?? 0) + deltaMs
}

function resetOffsets(): void {
  offsets.value = {}
}

const offsetsActive = computed(() => Object.values(offsets.value).some((v) => v !== 0))

/* ---- 图表序列 ---- */

function colorOf(index: number): string {
  return PALETTE[index % PALETTE.length]!
}

/** 单用例单图序列（按 runId 定位，避免同名用例串图） */
function seriesFor(c: CaseRecord, i: number): ChartSeries[] {
  return [
    {
      metricId: FPS_METRIC,
      label: c.caseName,
      unit: 'fps',
      color: colorOf(i),
      points: shifted(c.runId),
    },
  ]
}

const maxDurationMs = computed(() => {
  let max = 0
  for (const c of pool.value) max = Math.max(max, stats.value[c.runId]?.durationMs ?? 0)
  return max
})

/* ---- 概览对比表 + 逐用例统计小表 ---- */

const OVERVIEW_COLS: Array<{ label: string; read: (s: Statistics) => number | null; unit: string }> = [
  { label: 'Avg(FPS)', read: (s) => s.fps.avg, unit: 'fps' },
  { label: 'Std(FPS)', read: (s) => s.fps.std, unit: 'fps' },
  { label: 'Drop(FPS)', read: (s) => s.fps.drop, unit: '/h' },
  { label: 'Smooth', read: (s) => s.smooth.value, unit: '' },
  { label: '1%Low', read: (s) => s.lowFps.p1, unit: 'fps' },
  { label: 'Jank(/10min)', read: (s) => s.jank.per10min, unit: '/10min' },
  { label: 'Stutter', read: (s) => s.jank.stutter, unit: '%' },
  { label: 'Avg(FTime)', read: (s) => s.frameTime.avg, unit: 'ms' },
]

const FPS_TABLE_COLS: Array<{ label: string; read: (s: Statistics) => number | null }> = [
  { label: 'Avg(FPS)', read: (s) => s.fps.avg },
  { label: 'Std(FPS)', read: (s) => s.fps.std },
  { label: 'Var(FPS)', read: (s) => s.fps.var },
  { label: 'Drop(FPS)[/h]', read: (s) => s.fps.drop },
  { label: 'Min', read: (s) => s.fps.min },
  { label: 'Median', read: (s) => s.fps.median },
  { label: 'MedRange[%]', read: (s) => s.fps.medRange },
]

const JANK_TABLE_COLS: Array<{ label: string; read: (s: Statistics) => number | null }> = [
  { label: 'SmallJank', read: (s) => s.jank.small },
  { label: 'BigJank', read: (s) => s.jank.big },
  { label: 'Jank', read: (s) => s.jank.jank },
  { label: 'Stutter[%]', read: (s) => s.jank.stutter },
  { label: 'Jank(/10min)', read: (s) => s.jank.per10min },
  { label: 'Avg(FTime)[ms]', read: (s) => s.frameTime.avg },
]

function fmtNum(v: number | null): string {
  return v === null || v === undefined ? '—' : String(Math.round(v * 10) / 10)
}

function fmtDur(ms: number): string {
  const sec = Math.round(ms / 1000)
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`
}

function errText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

const basisNote = computed<string[]>(() => {
  const lines: string[] = []
  for (const c of pool.value) {
    const s = stats.value[c.runId]
    if (!s) continue
    if (s.frameDataBasis !== 'frameEvents') {
      lines.push(`${c.caseName}：${s.frameDataBasis} 基线 —— Jank 族/1%Low/FTime 无逐帧耗时，不可计算（[R§8.6] 不填假零值）`)
      break
    }
  }
  const smoothNote = stats.value[pool.value[0]?.runId ?? '']?.smooth.note
  if (smoothNote) lines.push(`Smooth：${smoothNote}`)
  return lines
})

/* ---- 预算门禁判定（§9 S3：对比池全量送判定引擎，N 路 2–10 组 + 逐指标 budget）---- */

async function runVerdict(): Promise<void> {
  if (pool.value.length < 2) return
  verdictLoading.value = true
  verdictError.value = ''
  try {
    verdict.value = await store.runComparePool({ budget: buildBudget() })
    ensureBudgetDrafts()
  } catch (err) {
    verdictError.value = errText(err)
    verdict.value = null
  } finally {
    verdictLoading.value = false
  }
}

/** 预算组装（§9 S3 budget）：defaults 单 run/组（池语义——每组恰 1 条 run，
 * 引擎缺省 minRuns=5 会让全部指标 unknown）；metrics 逐指标 warn/fail（% → 小数）。 */
function buildBudget(): CompareBudget {
  const metrics: Record<string, CompareBudgetMetric> = {}
  for (const [metricId, draft] of Object.entries(budgetDraft.value)) {
    const warnPct = parsePct(draft.warnPct)
    const failPct = parsePct(draft.failPct)
    if (warnPct === null && failPct === null) continue
    const item: CompareBudgetMetric = {}
    if (warnPct !== null) item.warnRegressionPct = warnPct / 100
    if (failPct !== null) item.failRegressionPct = failPct / 100
    metrics[metricId] = item
  }
  return { defaults: { minRuns: 1, recommendedRuns: 1 }, metrics }
}

function parsePct(raw: string | undefined): number | null {
  if (!raw || !raw.trim()) return null
  const value = Number(raw)
  return Number.isFinite(value) && value >= 0 ? value : null
}

/** 判定结果出现后，为引擎全指标交集逐指标铺预算草稿（warn/fail 空串起步） */
function ensureBudgetDrafts(): void {
  if (!verdict.value) return
  for (const metricId of Object.keys(verdict.value.metrics)) {
    if (!budgetDraft.value[metricId]) budgetDraft.value[metricId] = { warnPct: '', failPct: '' }
  }
}

function draftFor(metricId: string): { warnPct: string; failPct: string } {
  if (!budgetDraft.value[metricId]) budgetDraft.value[metricId] = { warnPct: '', failPct: '' }
  return budgetDraft.value[metricId]!
}

/** 判定矩阵行序 = 引擎全指标交集（响应 metrics 插入序，后端已按 metricId 排序） */
const verdictMetrics = computed<Array<[string, MetricCompare]>>(() =>
  Object.entries(verdict.value?.metrics ?? {}),
)

/** N 路分组列：响应 groups（S3 live 形态）；mock/旧两路形态回退 baseline+candidate 两组 */
const verdictGroups = computed<Array<{ index: number; label: string; color: string; runId: string }>>(
  () => {
    const v = verdict.value
    if (!v) return []
    if (v.groups && v.groups.length > 0) {
      return v.groups.map((g) => {
        const runId = g.runIds[0] ?? ''
        const label = pool.value.find((c) => c.runId === runId)?.caseName ?? runId ?? `组${g.index}`
        return { index: g.index, label, color: colorOf(g.index), runId }
      })
    }
    const baseId = v.meta?.baseline?.[0]?.runId ?? ''
    const candId = v.meta?.candidate?.[0]?.runId ?? ''
    const out: Array<{ index: number; label: string; color: string; runId: string }> = []
    if (baseId) out.push({ index: 0, label: pool.value.find((c) => c.runId === baseId)?.caseName ?? baseId, color: colorOf(0), runId: baseId })
    if (candId) out.push({ index: 1, label: pool.value.find((c) => c.runId === candId)?.caseName ?? candId, color: colorOf(1), runId: candId })
    return out
  },
)

/** 逐组中位数（S3 perGroup；mock 两路回退 baseline/candidate.median） */
function groupValueOf(metric: MetricCompare, groupIndex: number): number | null {
  if (metric.perGroup && metric.perGroup.length > 0) {
    return metric.perGroup.find((g) => g.groupIndex === groupIndex)?.median ?? null
  }
  if (groupIndex === 0) return metric.baseline?.median ?? null
  if (groupIndex === 1) return metric.candidate?.median ?? null
  return null
}

/** Δ 列：headline（N 路 = 最差对，两路 = 唯一对）的 delta */
function fmtDelta(metric: MetricCompare): string {
  const abs = metric.delta?.abs
  const pct = metric.delta?.pct
  if (abs === null || abs === undefined) return '—'
  const absText = `${abs >= 0 ? '+' : ''}${Math.round(abs * 10) / 10}`
  const pctText = pct === null || pct === undefined ? '' : ` (${(pct * 100).toFixed(1)}%)`
  return `${absText}${pctText}`
}

function judgeClass(judgement: string): string {
  if (judgement === 'pass') return 'green'
  if (judgement === 'fail') return 'red'
  if (judgement === 'warn') return 'orange'
  return 'gray'
}

function statusClass(status: string): string {
  return judgeClass(status)
}
</script>

<template>
  <div class="compare-wrap">
    <div class="records-head">
      <strong>对比报告</strong>
      <span class="hint">最多支持 {{ COMPARE_POOL_MAX }} 条用例同时对比（[I§2.9]；上限由前端执行）</span>
      <span style="flex: 1"></span>
      <button @click="store.state.activeTab = 'cases'">＋ 添加用例（去我的数据）</button>
      <button class="ghost" @click="store.clearComparePool()">清空对比池</button>
    </div>

    <!-- 对比用例卡槽（[I§2.9] web_cmp_page 顶部横排卡槽） -->
    <div class="pool-slots">
      <div v-for="(c, i) in pool" :key="c.runId" class="pool-card" :style="{ borderColor: colorOf(i) }">
        <div class="pool-card-head">
          <span class="swatch" :style="{ background: colorOf(i) }"></span>
          <span class="pool-name" :title="c.caseName">{{ c.caseName }}</span>
          <button class="mini ghost" title="移出对比池" @click="store.removeFromComparePool(c.runId)">×</button>
        </div>
        <div class="pool-meta">{{ c.device }}</div>
        <div class="pool-meta mono">{{ c.packageName ?? '—' }}</div>
        <div class="pool-meta">时长 {{ stats[c.runId] ? fmtDur(stats[c.runId]!.durationMs) : '…' }}</div>
        <div class="pool-offset" v-if="!sameAlign">
          对齐偏移 {{ (offsets[c.runId] ?? 0) >= 0 ? '+' : '' }}{{ Math.round(offsets[c.runId] ?? 0) }}ms
        </div>
      </div>
      <div v-for="n in Math.max(0, 5 - pool.length)" :key="`slot-${n}`" class="pool-slot">
        <div>添加用例</div>
        <div class="pool-meta">最多支持5条用例同时对比</div>
      </div>
    </div>

    <!-- 可比前提页头（[F§8]：相同设备/场景/时长/初始状态） -->
    <div class="premise-bar" :class="{ warn: premiseIssues.length > 0 }">
      <b>可比前提：</b>相同设备 · 相同场景 · 相同时长 · 相同初始状态（[F§8]）
      <template v-if="pool.length >= 2">
        <span class="chip" :class="premiseDevices === 1 ? 'green' : 'red'">设备 {{ premiseDevices === 1 ? '一致' : '不一致' }}</span>
        <span class="chip" :class="premisePackages === 1 ? 'green' : 'red'">应用 {{ premisePackages === 1 ? '一致' : '不一致' }}</span>
        <span
          v-if="durationSpread !== null"
          class="chip"
          :class="durationSpread <= 0.1 ? 'green' : 'red'"
        >
          时长 {{ durationSpread <= 0.1 ? '接近' : `差异 ${(durationSpread * 100).toFixed(0)}%` }}
        </span>
        <span class="chip gray">场景/初始状态无元数据，请人工确认</span>
        <span v-if="premiseIssues.length > 0" class="premise-warn">⚠ {{ premiseIssues.join('；') }} —— 结论可比性受限</span>
      </template>
    </div>

    <div v-if="pool.length === 0" class="empty-hint">
      对比池为空 —— 在「我的数据」行菜单选择「加入对比池」（≤5 条），再回到本页对比。
    </div>

    <template v-else>
      <!-- 概览对比表（[I§2.9] 概览对比汇总表：行=用例，列=统计指标） -->
      <div class="report-sub">概览对比（perfdog.statistics.v1 · /runs/{id}/statistics）</div>
      <div v-if="statsError" class="empty-hint">{{ statsError }}</div>
      <div v-else-if="statsLoading" class="empty-hint">统计加载中…</div>
      <table v-else class="records-table compare-table">
        <thead>
          <tr>
            <th>用例</th>
            <th v-for="col in OVERVIEW_COLS" :key="col.label" :title="col.unit">
              {{ col.label }}<span v-if="col.unit" class="unit">[{{ col.unit }}]</span>
            </th>
            <th>时长</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(c, i) in pool" :key="c.runId">
            <td><span class="swatch" :style="{ background: colorOf(i) }"></span> {{ c.caseName }}</td>
            <td v-for="col in OVERVIEW_COLS" :key="col.label" class="num">{{ fmtNum(stats[c.runId] ? col.read(stats[c.runId]!) : null) }}</td>
            <td class="num">{{ stats[c.runId] ? fmtDur(stats[c.runId]!.durationMs) : '—' }}</td>
          </tr>
        </tbody>
      </table>
      <div v-for="(line, idx) in basisNote" :key="idx" class="stat-note">※ {{ line }}</div>

      <!-- 预算门禁判定（§9 S3：对比池全量送判定引擎，N 路 2–10 组 + 逐指标 budget） -->
      <div class="report-sub">预算门禁判定（perfeval.compare.v1 · POST /api/reports/compare）</div>
      <div class="verdict-bar">
        <button :disabled="verdictLoading || pool.length < 2" @click="runVerdict">
          {{ verdictLoading ? '判定中…' : '运行判定' }}
        </button>
        <button class="ghost" @click="budgetOpen = !budgetOpen">预算编辑</button>
        <span v-if="verdict" class="chip" :class="statusClass(verdict.status)">总体 {{ verdict.status }}</span>
        <span v-if="verdict" class="hint">
          可靠性 {{ verdict.reliability ? Math.round(verdict.reliability.score * 100) / 100 : '—' }}（{{ verdict.reliability?.level ?? '—' }}）
        </span>
        <span class="hint">
          首条用例=全局基线，其余各成一组与基线两两判定（N 路 2–10 组，budget 随请求透传）
        </span>
      </div>
      <div v-if="budgetOpen" class="budget-editor">
        <b>逐指标预算（回归 warn/fail，百分比）：</b>
        <span class="hint">
          空 = 引擎缺省（无阈值 → 回归不越界即 pass）；defaults 固定 minRuns=1（池语义：每组 1 条 run）；
          warn/fail 以基线中位数回归百分比填写（如 warn 5 表示 +5% 提示、fail 10 表示 +10% 判退）。
        </span>
      </div>
      <div v-if="verdictError" class="empty-hint">判定请求失败：{{ verdictError }}</div>
      <div v-else-if="verdictLoading" class="empty-hint">判定引擎运行中…</div>
      <template v-else-if="verdict">
        <table class="records-table compare-table verdict-table">
          <thead>
            <tr>
              <th>指标（引擎全指标交集）</th>
              <th v-for="g in verdictGroups" :key="g.index">
                <span class="swatch" :style="{ background: g.color }"></span>
                {{ g.label }}<span class="unit">[中位数]</span>
              </th>
              <th>Δ vs 基线</th>
              <th>判定</th>
              <th>原因</th>
              <th>预算 warn% / fail%</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="[metricId, m] in verdictMetrics" :key="metricId">
              <td class="mono">{{ metricId }}<span v-if="m.unit" class="unit"> [{{ m.unit }}]</span></td>
              <td v-for="g in verdictGroups" :key="g.index" class="num">{{ fmtNum(groupValueOf(m, g.index)) }}</td>
              <td class="num">{{ fmtDelta(m) }}</td>
              <td><span class="chip" :class="judgeClass(m.judgement)">{{ m.judgement }}</span></td>
              <td class="reason-cell">{{ m.reason ?? '—' }}</td>
              <td class="budget-cells">
                <input v-model="draftFor(metricId).warnPct" class="mini-input" placeholder="warn%" />
                <input v-model="draftFor(metricId).failPct" class="mini-input" placeholder="fail%" />
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="verdict.meta?.nWayNote" class="stat-note">※ {{ verdict.meta.nWayNote }}</div>
        <div v-else class="stat-note">※ 两路判定：baseline/candidate 中位数 + 回归阈值（budget 缺省 = 引擎内建预算）。</div>
      </template>
      <div v-else class="empty-hint">点击「运行判定」将对比池（≤{{ COMPARE_POOL_MAX }} 条）全量送判定引擎。</div>

      <!-- 指标区：指标切换 + 两种时间对齐控件（[I§2.9]） -->
      <div class="report-section">
        <div class="metric-zone-head">
          <div class="family-switch">
            <span class="fbtn" :class="{ active: metricFamily === 'fps' }" @click="metricFamily = 'fps'">FPS</span>
            <span class="fbtn" :class="{ active: metricFamily === 'jank' }" @click="metricFamily = 'jank'">Jank</span>
          </div>
          <span class="hint">自由拖动：按住某条用例曲线左键水平拖动做时间对齐（对所有图表生效）</span>
          <label class="align-check" title="对比case将以时间最长的case为基准拉长（[I§2.9]）：各用例时间轴归一到自身起点">
            <input v-model="sameAlign" type="checkbox" />
            相同时间对齐
          </label>
          <span v-if="sameAlign && baselineCaseId" class="chip blue">
            基准（最长）：{{ pool.find((c) => c.runId === baselineCaseId)?.caseName }} · {{ fmtDur(maxDurationMs) }}
          </span>
          <button v-if="offsetsActive" class="mini" @click="resetOffsets">重置对齐</button>
        </div>

        <template v-if="metricFamily === 'fps'">
          <div v-if="curveNote" class="stat-note" style="margin-bottom: 8px">※ {{ curveNote }}</div>
          <div v-if="pool.some((c) => curves[c.runId]?.length)" class="chart-pages" style="padding: 0">
            <template v-for="(c, i) in pool" :key="c.runId">
              <MetricChart
                v-if="curves[c.runId]?.length"
                :title="c.caseName"
                :series="seriesFor(c, i)"
                :start-ts="commonOrigin ?? undefined"
                :window-ms="Number.MAX_SAFE_INTEGER"
                :axis-min-ts="sameAlign ? (commonOrigin ?? undefined) : undefined"
                :axis-max-ts="sameAlign ? (alignAxisMax ?? undefined) : undefined"
                :height="180"
                :pan-enabled="!sameAlign"
                @pan-delta="(d: number) => onPan(c.runId, d)"
              />
            </template>
          </div>
          <div class="report-sub" style="margin-top: 12px">逐用例统计小表（FPS 族）</div>
          <table class="records-table compare-table">
            <thead>
              <tr>
                <th>用例</th>
                <th v-for="col in FPS_TABLE_COLS" :key="col.label">{{ col.label }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="c in pool" :key="c.runId">
                <td>{{ c.caseName }}</td>
                <td v-for="col in FPS_TABLE_COLS" :key="col.label" class="num">{{ fmtNum(stats[c.runId] ? col.read(stats[c.runId]!) : null) }}</td>
              </tr>
            </tbody>
          </table>
        </template>

        <template v-else>
          <!-- Jank 族：双条件判定需逐帧耗时（[F§3.3]）。fpsSeries/none 基线不可计算 → 统计表如实置灰 + 说明。
               Jank 曲线（逐帧尖刺）依赖逐帧事件数据面（R37/M4），此处不伪造曲线。 -->
          <div class="stat-note" style="margin-bottom: 8px">
            ※ Jank 族曲线需逐帧耗时事件（timeline.ndjson / frame.duration.detail.ms）；当前数据基线下不绘制曲线，
            仅给出统计小表（fpsSeries 基线各值为 null 属预期，[R§8.6]）。逐帧数据面属 R37（M4）。
          </div>
          <table class="records-table compare-table">
            <thead>
              <tr>
                <th>用例</th>
                <th v-for="col in JANK_TABLE_COLS" :key="col.label">{{ col.label }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="c in pool" :key="c.runId">
                <td>{{ c.caseName }}</td>
                <td v-for="col in JANK_TABLE_COLS" :key="col.label" class="num">{{ fmtNum(stats[c.runId] ? col.read(stats[c.runId]!) : null) }}</td>
              </tr>
            </tbody>
          </table>
        </template>
      </div>
    </template>
  </div>
</template>

<style scoped>
.compare-wrap {
  padding: 14px;
}

.pool-slots {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.pool-card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-left-width: 3px;
  border-radius: var(--radius);
  padding: 8px 10px;
  width: 218px;
}

.pool-card-head {
  display: flex;
  align-items: center;
  gap: 6px;
}

.pool-card-head .swatch {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  flex: none;
}

.pool-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12.5px;
  font-weight: 600;
}

.pool-meta {
  font-size: 11px;
  color: var(--text-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pool-offset {
  font-size: 11px;
  color: #79b8ff;
  font-variant-numeric: tabular-nums;
}

.pool-slot {
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  padding: 8px 10px;
  width: 218px;
  color: var(--text-faint);
  font-size: 12px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 2px;
}

.premise-bar {
  background: var(--bg-panel-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 8px 12px;
  font-size: 12px;
  color: var(--text-dim);
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.premise-bar.warn {
  border-color: rgba(227, 179, 65, 0.5);
}

.premise-warn {
  color: #e3b341;
  font-size: 11.5px;
}

.compare-table th .unit {
  color: var(--text-faint);
  font-size: 10px;
}

.metric-zone-head {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.family-switch {
  display: inline-flex;
  border: 1px solid var(--border);
  border-radius: 6px;
  overflow: hidden;
}

.fbtn {
  padding: 4px 16px;
  font-size: 12px;
  cursor: pointer;
  color: var(--text-dim);
  user-select: none;
}

.fbtn.active {
  background: var(--accent);
  color: #fff;
}

.align-check {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  cursor: pointer;
  user-select: none;
}

.align-check input {
  accent-color: var(--accent);
}

.num {
  font-variant-numeric: tabular-nums;
  text-align: right;
}

.verdict-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.budget-editor {
  background: var(--bg-panel-2);
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  padding: 8px 12px;
  font-size: 12px;
  color: var(--text-dim);
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.verdict-table .mono {
  font-family: var(--font-mono, monospace);
  font-size: 11.5px;
}

.verdict-table .unit {
  color: var(--text-faint);
  font-size: 10px;
}

.reason-cell {
  font-size: 11px;
  color: var(--text-dim);
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.budget-cells {
  white-space: nowrap;
}

.mini-input {
  width: 64px;
  font-size: 11.5px;
  padding: 2px 5px;
  margin-right: 4px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text);
}
</style>
