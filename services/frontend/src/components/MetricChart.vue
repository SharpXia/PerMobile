<script lang="ts">
export interface ChartSeries {
  metricId: string
  label: string
  unit: string
  color: string
  points: Array<{ ts: number; value: number | null }>
}

/** M4 回放标记叠加（§15 三类 → 图内 markLine 样式映射） */
export interface ChartMarker {
  tsMs: number
  kind: 'label' | 'note' | 'mark'
  /** label/name；note/text；mark 无 —— 图内只按 kind 取样式，文本在时间轴色带展示 */
  color?: string
  text?: string
}

/** R30 共享框选区间（epoch ms，闭区间）；null = 无选区。由父组件持有并下发到全部图表 */
export interface BrushRange {
  fromMs: number
  toMs: number
}
</script>

<script setup lang="ts">
/**
 * 指标图表页（对标 PerfDog「一个指标一个 Page」[I§2.1]）：
 * - 居中标题 + Y 轴单位 + mm:ss 时间轴；
 * - 每图底部独立缩放条（ECharts dataZoom slider/inside），拖动后暂停跟随，可「回到实时」；
 * - 右缘图例：曲线名 + 当前值（曲线色一致）[I§4.1]；
 * - 高频序列 sampling:'lttb' 降采样（[R§R56] 口径）；
 * - 叠加模式：多个指标同图，按单位分 Y 轴（最多 5 轴，超出单位共用末轴）。
 * - M3 扩展：panEnabled 时按住左键水平拖动曲线做时间对齐（emit pan-delta 毫秒增量，
 *   语义见 [I§2.9] 对比页「自由拖动」；是否作用于全部图表由父组件决定）；
 *   markPoints 以红色菱形散点叠加（R44 退化点图内高亮）。
 * - M4 扩展（R37 回放联动）：cursorTs 蓝色游标竖线（←/→/悬停定位，[I§4.6]）；
 *   markers 三类标记叠加（mark 红竖线 / label 段边界虚线 / note 批注点线，§15）；
 *   hoverEnabled 时 emit axis-hover（轴时刻或 null），单点数据卡由父组件渲染（[I§2.5]）。
 * - M5 扩展（R28-R31）：
 *   · brushEnabled：ECharts brush lineX 框选 → emit brush-range；选区跨图同步由父组件
 *     以共享 brushRange prop（markArea 渲染到每张图）实现，本地 brush 区域框选后即清，
 *     避免与共享 markArea 双重渲染；
 *   · interactive：双击=批注（双击已有批注 ±NOTE_HIT_PX 内=删除）、单击（非拖动）=标定
 *     红竖线 —— 命中判定见 onCanvasDblclick / onCanvasPointerUp（[F§6][I§2.9]）；
 *   · threshold：图右上阈值输入 + 一键重置（R31），变更 emit threshold-commit 由父组件
 *     写回 store 并联动统计 fpsGe 与数值卡达标着色。
 */
import * as echarts from 'echarts'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { THRESHOLD_DEFAULTS, type ThresholdSpec, type ThresholdState } from '../thresholds'

const props = withDefaults(
  defineProps<{
    title: string
    series: ChartSeries[]
    height?: number
    /** 跟随窗口宽度（ms）；跟随模式下始终显示最近 windowMs */
    windowMs?: number
    /** 会话起始时刻（相对时间轴 mm:ss 的原点；不传则用绝对时间） */
    startTs?: number
    /** M3：启用左键拖动时间对齐（emit pan-delta，单位 ms） */
    panEnabled?: boolean
    /** M3：显式 X 轴时间域（毫秒时间戳）。传入后轴域固定为 [axisMinTs, axisMaxTs]，
     *  曲线按此域拉伸显示（对比页「相同时间对齐」以最长 case 为基准拉长 [I§2.9]）；
     *  不传保持按数据自适应。固定域下不进入跟随模式（窗口=全域）。 */
    axisMinTs?: number
    axisMaxTs?: number
    /** M3：退化点高亮散点（R44）；markClickEnabled=true 时单击命中退化点 emit mark-point-click
     *  （评审页回放联动：store.openReplay 携带退化点时刻 seek，R37 回放页） */
    markPoints?: Array<{ ts: number; value: number }>
    markName?: string
    /** 退化点可点击（评审页回放联动；interactive 关闭时生效——单击画布命中退化点 ±MARK_HIT_PX → emit） */
    markClickEnabled?: boolean
    /** M4：回放游标时刻（蓝色竖线；未传无游标） */
    cursorTs?: number
    /** M4：标记叠加（§15 mark=红竖线 / label=色虚线 / note=点线；M5 note 参与双击命中判定） */
    markers?: ChartMarker[]
    /** M4：启用轴悬停上报（emit axis-hover），父组件渲染单点数据卡 [I§2.5] */
    hoverEnabled?: boolean
    /** M5 R30：启用 ECharts brush lineX 框选（emit brush-range） */
    brushEnabled?: boolean
    /** M5 R30：共享框选区间（父组件下发到全部图表 = 跨图同步选区渲染） */
    brushRange?: BrushRange | null
    /** M5 R29：启用单击=标定 / 双击=批注编辑（仅会话可编辑窗口传 true） */
    interactive?: boolean
    /** M5 R31：图右上阈值输入（fps 两档 / ftime 一档；null 不渲染） */
    thresholdSpec?: ThresholdSpec | null
    thresholds?: ThresholdState
  }>(),
  {
    height: 240,
    windowMs: 120_000,
    startTs: undefined,
    panEnabled: false,
    axisMinTs: undefined,
    axisMaxTs: undefined,
    markPoints: undefined,
    markName: '退化点',
    markClickEnabled: false,
    cursorTs: undefined,
    markers: undefined,
    hoverEnabled: false,
    brushEnabled: false,
    brushRange: null,
    interactive: false,
    thresholdSpec: null,
    thresholds: () => THRESHOLD_DEFAULTS,
  },
)

const emit = defineEmits<{
  'pan-delta': [deltaMs: number]
  'axis-hover': [ts: number | null]
  'brush-range': [range: BrushRange]
  'mark-point-click': [point: { ts: number; value: number }]
  'note-create': [ts: number]
  'note-delete': [noteTs: number]
  'mark-create': [ts: number]
  'threshold-commit': [patch: { spec: ThresholdSpec; fpsGe?: number[]; ftimeMs?: number }]
}>()

const rootEl = ref<HTMLDivElement>()
const canvasEl = ref<HTMLDivElement>()
const following = ref(true)

let chart: echarts.ECharts | null = null
let raf = 0
let suppressZoom = false
let resizeObserver: ResizeObserver | null = null

const hasData = computed(() => props.series.some((s) => s.points.length > 0))

interface LegendItem {
  label: string
  color: string
  unit: string
  value: string
}

const legendItems = computed<LegendItem[]>(() =>
  props.series.map((s) => {
    let last: number | null = null
    for (let i = s.points.length - 1; i >= 0; i--) {
      const p = s.points[i]
      if (p && p.value !== null) {
        last = p.value
        break
      }
    }
    return { label: s.label, color: s.color, unit: s.unit, value: last === null ? '—' : fmtNum(last) }
  }),
)

function fmtNum(v: number): string {
  if (!Number.isFinite(v)) return '—'
  if (Math.abs(v) >= 1000) return v.toFixed(0)
  if (Number.isInteger(v)) return String(v)
  return v.toFixed(1)
}

function axisTime(ts: number): string {
  const rel = props.startTs !== undefined ? ts - props.startTs : ts
  const totalSec = Math.max(0, Math.floor(rel / 1000))
  const m = Math.floor(totalSec / 60)
  const s = totalSec % 60
  const p = (n: number): string => String(n).padStart(2, '0')
  return props.startTs !== undefined ? `${m}:${p(s)}` : `${p(m)}:${p(s)}`
}

const MAX_AXES = 5

function buildOption(): echarts.EChartsOption {
  const units: string[] = []
  for (const s of props.series) if (!units.includes(s.unit)) units.push(s.unit)
  const axisOf = new Map<string, number>()
  units.forEach((u, i) => axisOf.set(u, Math.min(i, MAX_AXES - 1)))
  const nAxes = Math.max(1, Math.min(units.length, MAX_AXES))

  const yAxis = Array.from({ length: nAxes }, (_, i) => ({
    type: 'value' as const,
    name: units[i] ?? '',
    nameTextStyle: { color: '#8b98a5', fontSize: 10 },
    scale: true,
    axisLine: { show: true, lineStyle: { color: '#2b3644' } },
    axisLabel: { color: '#8b98a5', fontSize: 10 },
    splitLine: { show: i === 0, lineStyle: { color: '#1c2531' } },
    position: (i === 0 ? 'left' : 'right') as 'left' | 'right',
    offset: i === 0 ? 0 : (i - 1) * 44,
  }))

  const series: echarts.SeriesOption[] = props.series.map((s) => ({
    name: s.label,
    type: 'line' as const,
    yAxisIndex: axisOf.get(s.unit) ?? 0,
    showSymbol: false,
    connectNulls: false, // null 点断开显示（空值 ≠ 0，[R§8.6]）
    sampling: 'lttb' as const,
    lineStyle: { width: 1.5, color: s.color },
    itemStyle: { color: s.color },
    data: s.points.map((p) => [p.ts, p.value]),
  }))

  // M3 R44：退化点高亮（红色菱形散点；不计入 props.series 图例，由父组件自行展示说明）
  if (props.markPoints && props.markPoints.length > 0) {
    series.push({
      name: props.markName ?? '退化点',
      type: 'scatter' as const,
      yAxisIndex: 0,
      symbol: 'diamond',
      symbolSize: 9,
      itemStyle: { color: '#e5534b' },
      z: 6,
      data: props.markPoints.map((p) => [p.ts, p.value]),
    })
  }

  // M4 R37：回放游标（蓝）+ §15 三类标记叠加（mark 红竖线 / label 色虚线 / note 点线）。
  // 挂在首条 series 上（markLine 按 xAxis 定位，与该 series 数值无关）
  const overlay = buildOverlayLines()
  // M5 R30：共享框选区间（父组件持有、下发全部图 = 跨图同步渲染），蓝色半透明 markArea
  const markArea = buildBrushMarkArea()
  const firstSeries = series[0] as Record<string, unknown> | undefined
  if (firstSeries && series.length > 0) {
    if (overlay.length > 0) {
      firstSeries.markLine = {
        silent: true,
        symbol: 'none',
        animation: false,
        label: { show: false },
        data: overlay,
      }
    }
    if (markArea) firstSeries.markArea = markArea
  }

  return {
    animation: false,
    grid: {
      left: nAxes > 1 ? 52 : 48,
      right: nAxes <= 1 ? 18 : 34 + (nAxes - 2) * 44,
      top: 20,
      bottom: 46,
    },
    xAxis: {
      type: 'time',
      // M3：显式轴域（对比页「相同时间对齐」以最长 case 为基准拉长）；不传按数据自适应
      ...(props.axisMinTs !== undefined && props.axisMaxTs !== undefined
        ? { min: props.axisMinTs, max: props.axisMaxTs }
        : {}),
      axisLine: { lineStyle: { color: '#2b3644' } },
      axisTick: { show: false },
      axisLabel: { color: '#8b98a5', fontSize: 10, formatter: (v: number) => axisTime(v) },
      splitLine: { show: false },
    },
    yAxis,
    series,
    // M5 R30：brush 组件（brushPreprocessor 要求 option 声明 brush 才创建组件模型，
    // node_modules/echarts/lib/component/brush/preprocessor.js）——框选输入设备，
    // 选区渲染交给共享 markArea（跨图同步由父组件状态保证），本地区域框选后即清
    ...(props.brushEnabled ? { brush: { xAxisIndex: 0, transformable: false, outOfBrush: {} } } : {}),
    dataZoom: [
      { type: 'inside', zoomOnMouseWheel: 'shift' },
      {
        type: 'slider',
        height: 16,
        bottom: 8,
        borderColor: '#2b3644',
        backgroundColor: '#131a22',
        fillerColor: 'rgba(47,129,247,0.18)',
        handleStyle: { color: '#2f81f7' },
        moveHandleStyle: { color: '#2f81f7' },
        textStyle: { color: '#5c6873', fontSize: 10 },
        dataBackground: { lineStyle: { color: '#2b3644' }, areaStyle: { color: '#1a222d' } },
      },
    ],
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#1a222d',
      borderColor: '#2b3644',
      textStyle: { color: '#d7e0ea', fontSize: 11 },
      formatter: (params: unknown) => tooltipFormatter(params),
    },
  }
}

/** M4：游标 + 标记的 markLine 数据（§15 三类样式映射；文本展示在时间轴色带，图内不重复标注） */
function buildOverlayLines(): Array<Record<string, unknown>> {
  const out: Array<Record<string, unknown>> = []
  if (props.cursorTs !== undefined && Number.isFinite(props.cursorTs)) {
    out.push({
      xAxis: props.cursorTs,
      lineStyle: { color: '#2f81f7', width: 1.4, type: 'solid' },
    })
  }
  for (const m of props.markers ?? []) {
    if (!Number.isFinite(m.tsMs)) continue
    if (m.kind === 'mark') {
      out.push({ xAxis: m.tsMs, lineStyle: { color: m.color ?? '#e5534b', width: 1.2, type: 'solid' } })
    } else if (m.kind === 'label') {
      out.push({ xAxis: m.tsMs, lineStyle: { color: m.color ?? '#a371f7', width: 1, type: 'dashed' } })
    } else {
      out.push({ xAxis: m.tsMs, lineStyle: { color: '#d2a8ff', width: 1, type: 'dotted' } })
    }
  }
  return out
}

/** M5 R30：共享选区 → 首条 series 的 markArea（蓝色半透明；无选区返回 null 不渲染） */
function buildBrushMarkArea(): Record<string, unknown> | null {
  const r = props.brushRange
  if (!r || !Number.isFinite(r.fromMs) || !Number.isFinite(r.toMs) || r.toMs <= r.fromMs) return null
  return {
    silent: true,
    animation: false,
    label: { show: false },
    itemStyle: { color: 'rgba(47, 129, 247, 0.16)', borderColor: 'rgba(47, 129, 247, 0.55)', borderWidth: 1 },
    data: [[{ xAxis: r.fromMs }, { xAxis: r.toMs }]],
  }
}

/* ---------------- M5 R30/R29：brush 框选 + 单击/双击命中判定 ---------------- */

/** 框选最小宽度：小于该值视为「点了没拖」的误触，不发区间统计（单击语义由 mark 走） */
const MIN_BRUSH_MS = 250
/** 双击命中已有批注的像素半径（PerfDog 批注图标点击容差量级） */
const NOTE_HIT_PX = 14
/** 单击命中退化点的像素半径（R44 回放联动：同批注容差量级） */
const MARK_HIT_PX = 14
/** 单击（标定）与双击（批注）消歧窗口：双击必先触发 click，窗口内的挂起 mark 作废 */
const CLICK_DBLCLICK_MS = 280

let brushArmed = false

/** brush 模式装载：option 含 brush 组件后 takeGlobalCursor 进入 lineX 框选态 */
function armBrush(): void {
  if (!chart || !props.brushEnabled || brushArmed) return
  chart.dispatchAction({
    type: 'takeGlobalCursor',
    key: 'brush',
    brushOption: { brushType: 'lineX', brushMode: 'single', transformable: false },
  })
  brushArmed = true
}

function onBrushEnd(ev: unknown): void {
  if (!props.brushEnabled) return
  const e = ev as { areas?: Array<{ coordRange?: [number, number] }> }
  const range = e.areas?.[0]?.coordRange
  // 框选即清本地渲染：选区统一由共享 brushRange → markArea 呈现（跨图同步单一路径）
  try {
    chart?.dispatchAction({ type: 'brush', areas: [] })
  } catch {
    /* 清除失败不影响回调 */
  }
  if (!Array.isArray(range) || range.length !== 2) return
  const from = range[0]
  const to = range[1]
  if (typeof from !== 'number' || typeof to !== 'number') return
  if (Math.abs(to - from) < MIN_BRUSH_MS) return // 误触（点击而非拖拽）不进区间统计
  const fromMs = Math.round(Math.min(from, to))
  const toMs = Math.round(Math.max(from, to))
  emit('brush-range', { fromMs, toMs })
}

/**
 * R29 命中判定（区分单击与框选、单击与双击）：
 * - 单击 vs 框选：pointerup 与 pointerdown 位移 ≤ 4px 且时长 ≤ 400ms 才算「单击」；
 *   超出即拖拽（brush 框选/缩放条），不发标定 —— 位移阈值先于 brushEnd 的 MIN_BRUSH_MS
 *   拦住拖拽分支；
 * - 单击 vs 双击：挂起标定延迟 CLICK_DBLCLICK_MS 提交，窗口内到达 dblclick 则作废
 *   （双击必先触发两次 click，此为唯一可靠的消歧路径）；
 * - 双击命中批注：点击 x 与批注 ts 的像素位置差 ≤ NOTE_HIT_PX 即视为命中该批注 →
 *   删除；否则视为空白处双击 → 新建批注。
 */
interface ClickProbe {
  x: number
  y: number
  t: number
}

let downProbe: ClickProbe | null = null
let pendingMarkTimer: ReturnType<typeof setTimeout> | null = null

function canvasPoint(e: MouseEvent): { px: number; py: number } | null {
  if (!canvasEl.value) return null
  const rect = canvasEl.value.getBoundingClientRect()
  return { px: e.clientX - rect.left, py: e.clientY - rect.top }
}

/** 事件 x → 轴时刻（ms）；越界/转换失败返回 null */
function tsAt(px: number): number | null {
  if (!chart) return null
  try {
    const v = chart.convertFromPixel({ xAxisIndex: 0 }, [px, 0])
    return Array.isArray(v) && typeof v[0] === 'number' ? v[0]! : null
  } catch {
    return null
  }
}

/** 批注 ts → 画布像素 x（找不到/未初始化返回 null） */
function pxOf(ts: number): number | null {
  if (!chart) return null
  try {
    const v = chart.convertToPixel({ xAxisIndex: 0 }, ts)
    return typeof v === 'number' ? v : null
  } catch {
    return null
  }
}

function onCanvasPointerDown(e: PointerEvent): void {
  if ((!props.interactive && !props.markClickEnabled) || e.button !== 0) return
  downProbe = { x: e.clientX, y: e.clientY, t: performance.now() }
}

function onCanvasPointerUp(e: PointerEvent): void {
  if ((!props.interactive && !props.markClickEnabled) || !downProbe) return
  const probe = downProbe
  downProbe = null
  const dist = Math.hypot(e.clientX - probe.x, e.clientY - probe.y)
  const dt = performance.now() - probe.t
  if (dist > 4 || dt > 400) return // 拖拽 = 框选/缩放，不是单击
  const p = canvasPoint(e)
  if (!p) return
  const ts = tsAt(p.px)
  if (ts === null) return
  // interactive：挂起提交，窗口内的 dblclick 会作废它
  if (props.interactive) {
    if (pendingMarkTimer) clearTimeout(pendingMarkTimer)
    pendingMarkTimer = setTimeout(() => {
      pendingMarkTimer = null
      emit('mark-create', Math.round(ts))
    }, CLICK_DBLCLICK_MS)
    return
  }
  // markClickEnabled（评审页退化点回放联动）：单击命中图内退化点（x 像素 ±MARK_HIT_PX）→ emit
  let best: { ts: number; value: number } | null = null
  let bestDx = Infinity
  for (const m of props.markPoints ?? []) {
    const mx = pxOf(m.ts)
    if (mx === null) continue
    const dx = Math.abs(mx - p.px)
    if (dx < bestDx) {
      bestDx = dx
      best = m
    }
  }
  if (best && bestDx <= MARK_HIT_PX) emit('mark-point-click', { ts: best.ts, value: best.value })
}

function onCanvasDblclick(e: MouseEvent): void {
  if (!props.interactive) return
  if (pendingMarkTimer) {
    clearTimeout(pendingMarkTimer) // 双击作废挂起的单击标定
    pendingMarkTimer = null
  }
  const p = canvasPoint(e)
  if (!p) return
  const ts = tsAt(p.px)
  if (ts === null) return
  // 命中判定：已有批注 ±NOTE_HIT_PX 内 → 删除该批注；否则新建
  const hit = (props.markers ?? []).find((m) => {
    if (m.kind !== 'note') return false
    const nx = pxOf(m.tsMs)
    return nx !== null && Math.abs(nx - p.px) <= NOTE_HIT_PX
  })
  if (hit) emit('note-delete', hit.tsMs)
  else emit('note-create', Math.round(ts))
}

/* ---------------- M5 R31：图右上阈值输入 ---------------- */

const fpsInputs = ref<[number, number]>([...props.thresholds.fpsGe] as [number, number])
const ftimeInput = ref<number>(props.thresholds.ftimeMs)

watch(
  () => props.thresholds,
  (t) => {
    fpsInputs.value = [t.fpsGe[0] ?? THRESHOLD_DEFAULTS.fpsGe[0]!, t.fpsGe[1] ?? THRESHOLD_DEFAULTS.fpsGe[1]!]
    ftimeInput.value = t.ftimeMs
  },
  { deep: true },
)

function commitFps(): void {
  if (!props.thresholdSpec || props.thresholdSpec !== 'fps') return
  emit('threshold-commit', { spec: 'fps', fpsGe: [fpsInputs.value[0]!, fpsInputs.value[1] ?? fpsInputs.value[0]!] })
}

function commitFtime(): void {
  if (props.thresholdSpec !== 'ftime') return
  emit('threshold-commit', { spec: 'ftime', ftimeMs: ftimeInput.value })
}

function resetFromBar(): void {
  if (props.thresholdSpec === 'fps') {
    fpsInputs.value = [...THRESHOLD_DEFAULTS.fpsGe] as [number, number]
    emit('threshold-commit', { spec: 'fps', fpsGe: [...THRESHOLD_DEFAULTS.fpsGe] })
  } else if (props.thresholdSpec === 'ftime') {
    ftimeInput.value = THRESHOLD_DEFAULTS.ftimeMs
    emit('threshold-commit', { spec: 'ftime', ftimeMs: THRESHOLD_DEFAULTS.ftimeMs })
  }
}

function tooltipFormatter(params: unknown): string {
  const arr = Array.isArray(params) ? params : [params]
  const lines: string[] = []
  let head = ''
  for (const raw of arr) {
    const item = raw as { seriesName?: string; value?: unknown; marker?: string }
    if (!item || !Array.isArray(item.value)) continue
    const ts = item.value[0]
    const v = item.value[1]
    if (typeof ts === 'number' && head === '') head = `<div style="color:#8b98a5">${axisTime(ts)}</div>`
    const s = props.series.find((x) => x.label === item.seriesName)
    const shown = v === null || v === undefined ? '—' : fmtNum(v)
    lines.push(
      `${item.marker ?? ''}${item.seriesName ?? ''}&nbsp;<b>${shown}</b>${s?.unit ? ` ${s.unit}` : ''}`,
    )
  }
  return head + lines.join('<br/>')
}

/** 跟随模式：始终显示最近 windowMs（通过 dataZoom 百分比实现，保留缩放条） */
function applyFollow(opt: echarts.EChartsOption): void {
  const all = props.series.flatMap((s) => s.points)
  if (all.length < 2 || !opt.dataZoom || !Array.isArray(opt.dataZoom)) return
  let min = Infinity
  let max = -Infinity
  for (const p of all) {
    if (p.ts < min) min = p.ts
    if (p.ts > max) max = p.ts
  }
  const extent = Math.max(max - min, 1)
  const win = Math.min(props.windowMs, extent)
  const start = Math.max(0, 100 - (win / extent) * 100)
  for (const dz of opt.dataZoom) {
    const z = dz as { start?: number; end?: number }
    z.start = start
    z.end = 100
  }
}

function update(): void {
  if (!chart) return
  const opt = buildOption()
  if (props.axisMinTs !== undefined && props.axisMaxTs !== undefined) {
    // 显式轴域：固定展示全域（不进入跟随模式，dataZoom 全开）
    if (opt.dataZoom && Array.isArray(opt.dataZoom)) {
      for (const dz of opt.dataZoom) {
        const z = dz as { start?: number; end?: number }
        z.start = 0
        z.end = 100
      }
    }
  } else if (following.value) {
    applyFollow(opt)
  }
  suppressZoom = true
  // xAxis 也 replaceMerge：显式轴域（axisMin/MaxTs）关闭后，旧 min/max 不能残留
  chart.setOption(opt, { replaceMerge: ['series', 'yAxis', 'xAxis'] })
  setTimeout(() => {
    suppressZoom = false
  }, 0)
  armBrush() // setOption 后重新装载 brush 框选态（takeGlobalCursor 非 option 持久项）
}

function scheduleUpdate(): void {
  if (raf) return
  raf = requestAnimationFrame(() => {
    raf = 0
    update()
  })
}

watch(
  () => [props.series, props.markPoints, props.axisMinTs, props.axisMaxTs, props.cursorTs, props.markers, props.brushRange],
  scheduleUpdate,
  { deep: true },
)

onMounted(() => {
  if (!canvasEl.value) return
  chart = echarts.init(canvasEl.value)
  // 滚轮不劫持主页面（UI 反馈：鼠标悬停指标图时滚轮/触摸板滚不动页面）——zrender
  // 对 wheel 一律 preventDefault。捕获阶段拦下普通滚轮（无 Shift）→ zrender 收不到
  // → 浏览器原生滚动主内容；Shift+滚轮放行给 ECharts（inside dataZoom 'shift' 缩放）。
  canvasEl.value.addEventListener(
    'wheel',
    (ev: WheelEvent) => {
      if (!ev.shiftKey) ev.stopPropagation()
    },
    { capture: true, passive: true },
  )
  chart.on('datazoom', () => {
    if (!suppressZoom) following.value = false
  })
  // M4 R37：轴悬停上报（hoverEnabled 时）—— updateAxisPointer 携带轴时刻，单点数据卡由父组件渲染；
  // globalout 上报 null 供父组件清除悬停态
  chart.on('updateAxisPointer', (event) => {
    if (!props.hoverEnabled) return
    const info = (event as { axesInfo?: Array<{ value?: unknown }> }).axesInfo?.[0]
    emit('axis-hover', typeof info?.value === 'number' ? info.value : null)
  })
  chart.getZr().on('globalout', () => {
    if (props.hoverEnabled) emit('axis-hover', null)
  })
  // M5 R30：brush 框选结束 → emit brush-range（宽度 < MIN_BRUSH_MS 的误触不上报）
  chart.on('brushEnd', onBrushEnd)
  // M5 R29：单击=标定 / 双击=批注（DOM 事件层做拖拽与双击消歧，命中判定见函数注释）
  canvasEl.value.addEventListener('pointerdown', onCanvasPointerDown)
  canvasEl.value.addEventListener('pointerup', onCanvasPointerUp)
  canvasEl.value.addEventListener('dblclick', onCanvasDblclick)
  resizeObserver = new ResizeObserver(() => chart?.resize())
  if (rootEl.value) resizeObserver.observe(rootEl.value)
  update()
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
  if (raf) cancelAnimationFrame(raf)
  if (pendingMarkTimer) {
    clearTimeout(pendingMarkTimer)
    pendingMarkTimer = null
  }
  brushArmed = false
  chart?.dispose()
  chart = null
})

function backToLive(): void {
  following.value = true
  update()
}

/* ---------------- M3：左键拖动时间对齐（panEnabled，[I§2.9] 自由拖动） ---------------- */

let panState: { startX: number; width: number; extentMs: number } | null = null

/** 当前可见时间跨度（ms）：跟随模式=windowMs，缩放模式=全量 × dataZoom 百分比 */
function visibleExtentMs(): number {
  const all = props.series.flatMap((s) => s.points).filter((p) => p.value !== null)
  if (all.length < 2) return 0
  let min = Infinity
  let max = -Infinity
  for (const p of all) {
    if (p.ts < min) min = p.ts
    if (p.ts > max) max = p.ts
  }
  const full = Math.max(1, max - min)
  if (following.value) return Math.min(props.windowMs, full)
  try {
    const opt = chart?.getOption() as { dataZoom?: Array<{ start?: number; end?: number }> } | undefined
    const dz = Array.isArray(opt?.dataZoom) ? opt!.dataZoom![0] : undefined
    if (dz && typeof dz.start === 'number' && typeof dz.end === 'number') {
      return Math.max(1, (full * (dz.end - dz.start)) / 100)
    }
  } catch {
    /* getOption 不可用时回退全量跨度 */
  }
  return full
}

function onPointerDown(e: PointerEvent): void {
  if (!props.panEnabled || !canvasEl.value) return
  const rect = canvasEl.value.getBoundingClientRect()
  panState = { startX: e.clientX, width: Math.max(1, rect.width), extentMs: visibleExtentMs() }
  canvasEl.value.setPointerCapture?.(e.pointerId)
}

function onPointerMove(e: PointerEvent): void {
  if (!panState) return
  const dx = e.clientX - panState.startX
  // 曲线右拖（dx>0）= 该曲线时间轴后移：emit 增量，方向语义由父组件的 offset 应用保持
  const deltaMs = (dx / panState.width) * panState.extentMs
  if (Math.abs(deltaMs) >= 1) {
    panState.startX = e.clientX
    emit('pan-delta', Math.round(deltaMs))
  }
}

function onPointerUp(e: PointerEvent): void {
  if (panState) canvasEl.value?.releasePointerCapture?.(e.pointerId)
  panState = null
}
</script>

<template>
  <div ref="rootEl" class="chart-page">
    <div class="chart-page-title">{{ title }}</div>
    <!-- M5 R31：图右上阈值输入 + 一键重置（默认值 = 统计口径建议 FPS>=18/25、FTime>=100ms） -->
    <div v-if="thresholdSpec" class="chart-thresholds">
      <template v-if="thresholdSpec === 'fps'">
        <span class="th-name">FPS/(>=)</span>
        <input
          v-model.number="fpsInputs[0]"
          class="th-input"
          type="number"
          min="1"
          title="FPS>=第1档阈值（占比统计与达标着色联动）"
          @change="commitFps"
          @keydown.enter="commitFps"
        />
        <input
          v-model.number="fpsInputs[1]"
          class="th-input"
          type="number"
          min="1"
          title="FPS>=第2档阈值"
          @change="commitFps"
          @keydown.enter="commitFps"
        />
      </template>
      <template v-else>
        <span class="th-name">FrameTime(>=)</span>
        <input
          v-model.number="ftimeInput"
          class="th-input"
          type="number"
          min="1"
          title="FTime>=阈值（ms；数值卡达标着色联动）"
          @change="commitFtime"
          @keydown.enter="commitFtime"
        />
        <span class="th-unit">ms</span>
      </template>
      <button class="th-reset" title="一键重置为统计口径建议默认值" @click="resetFromBar">重置</button>
    </div>
    <div class="chart-body" :style="{ height: height + 'px' }">
      <div
        ref="canvasEl"
        class="chart-canvas"
        :class="{ pannable: panEnabled, brushable: brushEnabled }"
        @pointerdown="onPointerDown"
        @pointermove="onPointerMove"
        @pointerup="onPointerUp"
        @pointercancel="onPointerUp"
      ></div>
      <div class="chart-legend">
        <div v-for="it in legendItems" :key="it.label" class="legend-item">
          <span class="swatch" :style="{ background: it.color }"></span>
          <span class="lname" :style="{ color: it.color }">{{ it.label }}</span>
          <span class="lvalue">{{ it.value }}<span v-if="it.unit" class="lunit">{{ it.unit }}</span></span>
        </div>
        <div
          v-if="markPoints && markPoints.length > 0"
          class="legend-item"
          :title="markClickEnabled ? `退化点（单击进入回放并定位该时刻）` : '退化点（图内高亮）'"
        >
          <span class="swatch" style="background: #e5534b"></span>
          <span class="lname" style="color: #e5534b">{{ markName }} ×{{ markPoints.length }}</span>
        </div>
      </div>
    </div>
    <button v-if="!following && hasData" class="back-to-live" @click="backToLive">▶ 回到实时</button>
    <div v-if="!hasData" class="chart-empty">等待数据…</div>
  </div>
</template>

<style scoped>
.chart-canvas.pannable {
  cursor: grab;
}
.chart-canvas.pannable:active {
  cursor: grabbing;
}
</style>

<style scoped>
.chart-page {
  position: relative;
}
.lunit {
  font-size: 10px;
  font-weight: 400;
  color: var(--text-dim);
  margin-left: 2px;
}

/* M5 R31：图右上阈值输入（PerfDog [I§2.9] `FPS/(>=) [18][25] 重置` 形态） */
.chart-thresholds {
  position: absolute;
  top: 0;
  right: 0;
  z-index: 4;
  display: flex;
  align-items: center;
  gap: 4px;
  background: #131a22cc;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 2px 6px;
}

.chart-thresholds .th-name {
  font-size: 10px;
  color: var(--text-dim);
}

.chart-thresholds .th-input {
  width: 44px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text);
  font-size: 11px;
  padding: 1px 4px;
  -moz-appearance: textfield;
}

.chart-thresholds .th-input::-webkit-outer-spin-button,
.chart-thresholds .th-input::-webkit-inner-spin-button {
  -webkit-appearance: none;
}

.chart-thresholds .th-unit {
  font-size: 10px;
  color: var(--text-faint);
}

.chart-thresholds .th-reset {
  font-size: 10px;
  padding: 1px 6px;
  background: none;
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text-dim);
  cursor: pointer;
}

.chart-thresholds .th-reset:hover {
  color: var(--text);
  border-color: var(--text-dim);
}

.chart-canvas.brushable {
  cursor: crosshair;
}
</style>
