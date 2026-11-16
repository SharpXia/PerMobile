<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { Ref } from 'vue'
import { useStore } from '../store'
import FrameOverviewChart from '../components/FrameOverviewChart.vue'
import RootCauseCard from '../components/RootCauseCard.vue'
import { isDeepAnalysisFrameDetail } from '../api-client'
import type {
  CpuSchedulingSlice,
  DeepAnalysisOverview,
  FlamegraphNode,
  FrameRecord,
  SimpleperfFace,
} from '../api-client'

/**
 * 深度分析视图（M7 R50，[I§2.7] 深度分析页交互层；数据面 = 契约 §22.1）：
 * - 入口：报告页 / 回放页「深度分析」按钮（run 侧端点，会话期无 trace 工件不提供入口）；
 * - 帧时间线 Overview 图（X=帧序号，Y=FTime，30/60FPS 参考线；jank 红点）；
 * - 帧列表（可按 FTime 排序）→ 点击单帧 = Overview 卡（该帧 FrameTimeline 字段）+
 *   CPU 调度条带（按核泳道、线程着色）+ Thread 状态行（Running/Runnable/Sleeping 色带）；
 * - M 键定位帧（[I§2.7] S4「帧时间线点击某帧后按 M 键选定对应帧，上下联动」）+
 *   帧序号输入框直接定位（对标 M 键语义的显式输入路径）；右键菜单 Max FrameTime / Detail；
 * - CPU 热点火焰图卡（M8+ 缺口 flamegraph-compare-missing，§22 M8+ 扩展 simpleperf
 *   数据面）：icicle 行（缩进=栈深、条宽=overhead%），Set left / Set right 左右对比
 *   （[F§9] 调用栈对比语义：右−左差值着色，红=CPU 占用上升 / 绿=下降）；快照存模块级
 *   内存（跨 run 浏览保持，页面刷新即失，不做持久化）；
 * - 数据缺失：traceAvailable=false → 空结构 + note 原样显示（无 perfetto trace 是常态，
 *   不回退伪造帧数据 [R§8.6]）；simpleperf 无工件 → available=false + note 原样显示；
 *   live 后端未实现 §22 端点时如实显示错误。
 * - 根因诊断卡（R51）挂载页尾（RootCauseCard，§22.2）。
 */
const store = useStore()

const src = computed(() => store.state.deep)

const loading = ref(false)
const errorMsg = ref('')
const overview = ref<DeepAnalysisOverview | null>(null)
let loadSeq = 0

async function loadOverview(): Promise<void> {
  const runId = src.value?.runId
  if (!runId) return
  const seq = ++loadSeq
  loading.value = true
  errorMsg.value = ''
  try {
    const res = await store.getRunDeepAnalysis(runId)
    if (seq !== loadSeq) return
    if (isDeepAnalysisFrameDetail(res)) {
      // 服务端对缺省请求返回了单帧形态（不合契约 §22.1 两形态）——如实显示，不猜字段
      errorMsg.value = '响应形态异常（缺省请求应返回帧列表概览，收到单帧形态）'
      return
    }
    overview.value = res
  } catch (err) {
    if (seq === loadSeq) errorMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

/* ---------------- 时间轴原点（帧列表最早 ts；相对时间 [I§2.7] `17008.82ms` 形态改 mm:ss.mmm） ---------------- */

const originTs = computed<number | null>(() => {
  let min = Infinity
  for (const f of overview.value?.frames ?? []) if (f.tsEpochMs < min) min = f.tsEpochMs
  return Number.isFinite(min) ? min : null
})

function fmtRel(ts: number): string {
  if (originTs.value === null) return `${Math.round(ts)}`
  const rel = Math.max(0, ts - originTs.value)
  const ms = Math.floor(rel % 1000)
  const total = Math.floor(rel / 1000)
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}.${String(ms).padStart(3, '0')}`
}

/* ---------------- 帧列表（可按 FTime 排序，[I§2.7] stat 表排序语义） ---------------- */

type SortKey = 'frameIndex' | 'durationMs'
const sortKey = ref<SortKey>('frameIndex')
const sortAsc = ref(true)

function toggleSort(key: SortKey): void {
  if (sortKey.value === key) sortAsc.value = !sortAsc.value
  else {
    sortKey.value = key
    sortAsc.value = key === 'frameIndex' // FTime 默认降序（最慢帧在前更常用）
  }
}

const sortedFrames = computed<FrameRecord[]>(() => {
  const frames = [...(overview.value?.frames ?? [])]
  const key = sortKey.value
  frames.sort((a, b) => (sortAsc.value ? a[key] - b[key] : b[key] - a[key]))
  return frames
})

/* ---------------- 单帧选择：Overview 卡 + CPU 调度（上下联动 [I§2.7]） ---------------- */

const selectedFrameIndex = ref<number | null>(null)
const selectedFrame = ref<FrameRecord | null>(null)
const frameDetailSlices = ref<CpuSchedulingSlice[]>([])
const detailLoading = ref(false)
const detailError = ref('')
/** Overview 图悬停帧（M 键「选定对应帧」的锚；灰游标显示） */
const hoverFrameIndex = ref<number | null>(null)
let detailSeq = 0

/** 深度分析数据源切换（含首次进入）：清帧选择与详情后重取概览（watch 置于全部 ref 声明后，避免 immediate TDZ） */
watch(
  src,
  () => {
    overview.value = null
    errorMsg.value = ''
    selectedFrame.value = null
    frameDetailSlices.value = []
    hoverFrameIndex.value = null
    if (src.value) void loadOverview()
  },
  { immediate: true },
)

async function selectFrame(frameIndex: number): Promise<void> {
  const runId = src.value?.runId
  if (!runId) return
  const seq = ++detailSeq
  selectedFrameIndex.value = frameIndex
  detailLoading.value = true
  detailError.value = ''
  try {
    const res = await store.getRunDeepAnalysis(runId, frameIndex)
    if (seq !== detailSeq) return
    if (!isDeepAnalysisFrameDetail(res)) {
      detailError.value = '响应形态异常（frameIndex 请求应返回单帧形态）'
      return
    }
    selectedFrame.value = res.frame
    frameDetailSlices.value = res.cpuScheduling
    void scrollRowIntoView(frameIndex)
  } catch (err) {
    if (seq !== detailSeq) return
    selectedFrame.value = null
    frameDetailSlices.value = []
    detailError.value = err instanceof Error ? err.message : String(err)
  } finally {
    if (seq === detailSeq) detailLoading.value = false
  }
}

const listEl = ref<HTMLDivElement | null>(null)

function scrollRowIntoView(frameIndex: number): void {
  void nextTick(() => {
    const el = listEl.value?.querySelector(`[data-frame-index="${frameIndex}"]`)
    el?.scrollIntoView({ block: 'nearest' })
  })
}

/** 帧序号输入框（M 键语义的显式输入路径；Enter 定位） */
const frameInput = ref<number | null>(null)

function locateByInput(): void {
  if (frameInput.value === null || !Number.isFinite(frameInput.value)) return
  void selectFrame(Math.trunc(frameInput.value))
}

/** M 键定位帧（[I§2.7] S4 原文；输入框聚焦时不劫持按键） */
function onKey(e: KeyboardEvent): void {
  if (e.key !== 'm' && e.key !== 'M') return
  const target = e.target as HTMLElement | null
  if (target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return
  if (e.altKey || e.ctrlKey || e.metaKey) return
  if (hoverFrameIndex.value === null) return
  e.preventDefault()
  void selectFrame(hoverFrameIndex.value)
}

onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))

/* ---------------- 右键菜单（[I§2.7] S4：Max FrameTime / Detail） ---------------- */

const ctxMenu = ref<{ x: number; y: number; frameIndex: number | null } | null>(null)

function onChartContext(payload: { frameIndex: number | null; x: number; y: number }): void {
  ctxMenu.value = payload
}

function ctxMaxFrameTime(): void {
  ctxMenu.value = null
  const frames = overview.value?.frames ?? []
  if (frames.length === 0) return
  let max = frames[0]!
  for (const f of frames) if (f.durationMs > max.durationMs) max = f
  void selectFrame(max.frameIndex)
}

function ctxDetail(): void {
  const idx = ctxMenu.value?.frameIndex
  ctxMenu.value = null
  if (idx !== null && idx !== undefined) void selectFrame(idx)
}

function closeCtx(): void {
  ctxMenu.value = null
}

onMounted(() => window.addEventListener('click', closeCtx))
onBeforeUnmount(() => window.removeEventListener('click', closeCtx))

/* ---------------- CPU 调度条带（按核泳道着色）+ Thread 状态行 ---------------- */

/** 线程名 → 泳道色（调色板循环；与线程状态色独立——条带按线程着色 [I§2.7]） */
const THREAD_PALETTE = ['#58a6ff', '#3fb950', '#d29922', '#a371f7', '#e5534b', '#39c5cf', '#db61a2', '#8b98a5']

function threadColor(name: string): string {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0
  return THREAD_PALETTE[h % THREAD_PALETTE.length] ?? '#8b98a5'
}

/** 线程状态色（[I§2.7] Thread 状态行：Running/Runnable/Sleep 色带） */
function stateColor(state: string): string {
  const s = state.toLowerCase()
  if (s.startsWith('run') && s.includes('ing')) return '#3fb950' // Running
  if (s.startsWith('run')) return '#d29922' // Runnable
  if (s.includes('sleep')) return '#4c5b6b' // Sleeping/S
  if (s.includes('block') || s.includes('wait')) return '#8957e5'
  return '#8b98a5'
}

interface SchedWindow {
  startMs: number
  spanMs: number
}

const schedWindow = computed<SchedWindow | null>(() => {
  const frame = selectedFrame.value
  const slices = frameDetailSlices.value
  if (!frame || slices.length === 0) return null
  let start = frame.tsEpochMs
  let end = frame.tsEpochMs + Math.max(frame.durationMs, 1)
  for (const s of slices) {
    if (s.startEpochMs < start) start = s.startEpochMs
    const e = s.startEpochMs + s.durationMs
    if (e > end) end = e
  }
  return { startMs: start, spanMs: Math.max(1, end - start) }
})

interface SchedBlockVM {
  slice: CpuSchedulingSlice
  leftPct: string
  widthPct: string
  color: string
  title: string
}

function pctOfWindow(ts: number, win: SchedWindow): number {
  return ((ts - win.startMs) / win.spanMs) * 100
}

function schedBlocks(groupBy: 'core' | 'thread'): Array<{ key: string; label: string; blocks: SchedBlockVM[] }> {
  const win = schedWindow.value
  const slices = frameDetailSlices.value
  if (!win || slices.length === 0) return []
  const groups = new Map<string, CpuSchedulingSlice[]>()
  for (const s of slices) {
    const key = groupBy === 'core' ? `CPU${s.core}` : s.threadName
    const list = groups.get(key) ?? []
    list.push(s)
    groups.set(key, list)
  }
  const keys = [...groups.keys()].sort((a, b) => {
    if (groupBy === 'core') {
      const ca = Number(a.replace('CPU', ''))
      const cb = Number(b.replace('CPU', ''))
      return ca - cb
    }
    return a.localeCompare(b)
  })
  return keys.map((key) => ({
    key,
    label: key,
    blocks: (groups.get(key) ?? []).map((s) => {
      const left = Math.max(0, pctOfWindow(s.startEpochMs, win))
      const width = Math.max(0.4, Math.min(100 - left, ((s.durationMs) / win.spanMs) * 100))
      return {
        slice: s,
        leftPct: `${left}%`,
        widthPct: `${width}%`,
        color: groupBy === 'core' ? threadColor(s.threadName) : stateColor(s.state),
        title: `${groupBy === 'core' ? s.threadName : s.state} · ${s.threadName}(${s.tid}) · core ${s.core} · ${s.state} · ${s.durationMs.toFixed(2)}ms @ ${fmtRel(Math.round(s.startEpochMs))}`,
      }
    }),
  }))
}

const coreLanes = computed(() => schedBlocks('core'))
const threadLanes = computed(() => schedBlocks('thread'))

/** 泳道图例（出现的线程名 + 颜色） */
const legendThreads = computed<Array<{ name: string; color: string }>>(() => {
  const names = [...new Set(frameDetailSlices.value.map((s) => s.threadName))].sort()
  return names.map((name) => ({ name, color: threadColor(name) }))
})

/* ---------------- 头部与诊断卡入参 ---------------- */

const caseTitle = computed(() => src.value?.caseName ?? src.value?.runId ?? '')

const jankRateText = computed(() => {
  const r = overview.value?.jankRate
  return r === null || r === undefined ? '—' : `${(r * 100).toFixed(2)}%`
})

const ROOT_CAUSE_ANCHORS = ['frame.fps.mean', 'cpu.total.pct', 'cpu.proc.pct', 'memory.pss.peak_mb']

/* ---------------- CPU 热点火焰图卡（§22 M8+ 扩展 simpleperf 数据面） ---------------- */

const simpleperfFace = computed<SimpleperfFace | null>(
  () => overview.value?.simpleperf ?? null,
)

/** stat 卡入参（模板不依赖 computed 窄化，直接用该可空 computed + 可选链） */
const simpleperfStat = computed(() => simpleperfFace.value?.stat ?? null)

interface FlameRow {
  key: string
  name: string
  depth: number
  value: number
}

/** 树 → icicle 行（DFS；key = 全路径，左右对比按路径对齐） */
function flattenFlameTree(node: FlamegraphNode): FlameRow[] {
  const rows: FlameRow[] = []
  const walk = (item: FlamegraphNode, depth: number, prefix: string): void => {
    const key = `${prefix}/${item.name}`
    rows.push({ key, name: item.name, depth, value: Number(item.value ?? 0) })
    for (const child of item.children ?? []) walk(child, depth + 1, key)
  }
  walk(node, 0, '')
  return rows
}

const flameRows = computed<FlameRow[] | null>(() => {
  const tree = simpleperfFace.value?.flamegraph?.tree
  return tree ? flattenFlameTree(tree) : null
})

const flameRootValue = computed(() => Math.max(flameRows.value?.[0]?.value ?? 1, 1))

/** 左/右快照：模块级 reactive 内存（跨 run 浏览保持；页面刷新即失——不做持久化，
 * 如实声明）。容器必须是 Vue ref：普通对象的变更不触发计算属性重算（冒烟实测踩坑）；
 * ref 挂 globalThis 实现模块单例（SFC 单 setup 块无模块级作用域）。 */
interface FlameSnapshot {
  runId: string
  caseName: string
  rows: FlameRow[]
}
const flameScope = globalThis as {
  __perfevalFlameSnapshots?: Ref<{ left: FlameSnapshot | null; right: FlameSnapshot | null }>
}
if (!flameScope.__perfevalFlameSnapshots) {
  flameScope.__perfevalFlameSnapshots = ref({ left: null, right: null })
}
const flameSnapshots = flameScope.__perfevalFlameSnapshots

function snapshotFlame(side: 'left' | 'right'): void {
  const rows = flameRows.value
  if (!rows) return
  flameSnapshots.value[side] = {
    runId: src.value?.runId ?? '',
    caseName: src.value?.caseName ?? '',
    rows: rows.map((row) => ({ ...row })),
  }
}

function clearFlameSnapshots(): void {
  flameSnapshots.value.left = null
  flameSnapshots.value.right = null
}

/** 左右对比就绪（两侧都设置过）→ 差值视图 */
const flameDiffReady = computed(
  () => flameSnapshots.value.left !== null && flameSnapshots.value.right !== null,
)

interface FlameDiffRow {
  name: string
  depth: number
  left: number
  right: number
  diff: number
}

/** 左右差值行：按全路径对齐（缺侧按 0 计），红=右−左>0（占用上升）、绿=下降，按 |diff| 降序 */
const flameDiffRows = computed<FlameDiffRow[]>(() => {
  if (!flameDiffReady.value) return []
  const left = new Map(flameSnapshots.value.left!.rows.map((row) => [row.key, row]))
  const right = new Map(flameSnapshots.value.right!.rows.map((row) => [row.key, row]))
  const rows: FlameDiffRow[] = []
  const keys = new Set<string>([...left.keys(), ...right.keys()])
  for (const key of keys) {
    const lRow = left.get(key)
    const rRow = right.get(key)
    if ((lRow?.value ?? 0) === 0 && (rRow?.value ?? 0) === 0) continue // 两侧 0 值链上帧不入差值表
    rows.push({
      name: (rRow ?? lRow)!.name,
      depth: Math.max(lRow?.depth ?? 0, rRow?.depth ?? 0),
      left: lRow?.value ?? 0,
      right: rRow?.value ?? 0,
      diff: (rRow?.value ?? 0) - (lRow?.value ?? 0),
    })
  }
  rows.sort((a, b) => Math.abs(b.diff) - Math.abs(a.diff))
  return rows
})

function flameDiffTone(diff: number): string {
  if (diff > 0.05) return 'flame-worse' // 红：右比左占用更高
  if (diff < -0.05) return 'flame-better' // 绿：右比左占用更低
  return 'flame-flat'
}

const flameLeftLabel = computed(() =>
  flameSnapshots.value.left ? `${flameSnapshots.value.left.caseName || flameSnapshots.value.left.runId}` : '',
)
const flameRightLabel = computed(() =>
  flameSnapshots.value.right ? `${flameSnapshots.value.right.caseName || flameSnapshots.value.right.runId}` : '',
)
</script>

<template>
  <div class="deep-wrap">
    <div class="report-head">
      <button class="ghost" @click="store.closeDeep()">← 返回</button>
      <strong class="report-title">🔬 深度分析 · {{ caseTitle }}</strong>
      <template v-if="overview && overview.traceAvailable">
        <span class="chip blue" title="契约 §22.1 source：数据源（FrameTimeline）">{{ overview.source ?? 'trace' }}</span>
        <span class="chip gray" title="帧总数（trace 覆盖窗口内）">帧 ×{{ overview.frameCount }}</span>
        <span class="chip red" title="Jank 帧数 / Jank 率">Jank ×{{ overview.jankCount }}（{{ jankRateText }}）</span>
        <span v-if="overview.vsyncPeriodMs" class="chip gray" title="vsync 周期">vsync {{ overview.vsyncPeriodMs }}ms</span>
      </template>
      <span style="flex: 1"></span>
      <button
        v-if="src"
        class="mini"
        title="同一 run 的报告页（数值卡矩阵 + 分段统计）"
        @click="store.openRunReport(src.runId, src.caseName)"
      >
        查看报告
      </button>
    </div>

    <div v-if="loading && !overview" class="empty-hint">深度分析数据加载中…（§22.1 /deep-analysis）</div>
    <div v-else-if="errorMsg" class="empty-hint">
      深度分析数据不可用：{{ errorMsg }}
      <template>（live 后端可能未实现契约 §22 /deep-analysis 端点）</template>
    </div>

    <template v-else-if="overview">
      <!-- 数据缺失：无 perfetto trace → note 原样说明（不回退伪造帧数据 [R§8.6]） -->
      <div v-if="!overview.traceAvailable" class="empty-note">
        <div class="en-icon">▦</div>
        <div class="en-title">深度分析数据面为空（traceAvailable=false）</div>
        <div class="en-text">{{ overview.note ?? 'run 无 perfetto trace 工件（未采集 android.surfaceflinger.frametimeline 或 trace_processor 不可用）。' }}</div>
        <div class="en-hint">采集 perfetto trace 属真机路径（real-only）；synthetic/mock 模式不回退伪造帧数据。</div>
      </div>

      <template v-else>
        <div v-if="overview.note" class="stat-note" style="margin-bottom: 8px">※ {{ overview.note }}</div>

        <!-- M 键定位 + 输入框定位（对标「M 键定位帧」语义） -->
        <div class="deep-toolbar">
          <span class="hint">
            点击图/列表选帧 · 悬停后按
            <kbd>M</kbd> 选定对应帧 · 输入帧号直接定位 · 右键菜单 Max FrameTime / Detail
          </span>
          <span style="flex: 1"></span>
          <label class="locate-box">
            帧号
            <input
              v-model.number="frameInput"
              type="number"
              min="0"
              placeholder="frameIndex"
              @keydown.enter="locateByInput"
            />
            <button class="mini" @click="locateByInput">定位</button>
          </label>
        </div>

        <!-- 帧时间线 Overview 图（X=帧序号，Y=FTime ms，30/60FPS 参考线） -->
        <FrameOverviewChart
          :frames="overview.frames"
          :selected-index="selectedFrameIndex"
          :hover-index="hoverFrameIndex"
          :height="220"
          @hover-frame="hoverFrameIndex = $event"
          @select-frame="selectFrame($event)"
          @context-frame="onChartContext"
        />

        <!-- 右键菜单（Max FrameTime / Detail，[I§2.7] S4 原文两项） -->
        <div
          v-if="ctxMenu"
          class="ctx-menu"
          :style="{ left: ctxMenu.x + 'px', top: ctxMenu.y + 'px' }"
          @click.stop
        >
          <button class="ctx-item" @click="ctxMaxFrameTime">Max FrameTime（跳转最大帧）</button>
          <button class="ctx-item" :disabled="ctxMenu.frameIndex === null" @click="ctxDetail">
            Detail（查看单帧{{ ctxMenu.frameIndex !== null ? ` #${ctxMenu.frameIndex}` : '' }}）
          </button>
        </div>

        <!-- 单帧 Overview 卡 + CPU 调度条带 + Thread 状态行（上下联动） -->
        <div class="deep-detail">
          <div class="report-sub">单帧 Overview</div>
          <div v-if="detailError" class="stat-note">单帧数据不可用：{{ detailError }}（frameIndex 越界 → 400 validation_error，契约 §22.1）</div>
          <div v-else-if="detailLoading && !selectedFrame" class="empty-hint">单帧数据加载中…（?frameIndex=）</div>
          <div v-else-if="!selectedFrame" class="empty-hint">
            未选中帧 —— 在时间线/列表点击某帧，或悬停后按 <kbd>M</kbd>、输入帧号定位。
          </div>
          <template v-else>
            <div class="frame-card">
              <div class="fc-cell fc-main" title="帧序号 + FTime（durationMs）[I§2.7] 单帧数据卡形态">
                <div class="fc-label">Frame {{ selectedFrame.frameIndex }}</div>
                <div class="fc-value">FrameTime {{ selectedFrame.durationMs.toFixed(2) }} ms</div>
              </div>
              <div class="fc-cell" title="Jank 判定与类型（FrameTimeline jankType 原文）">
                <div class="fc-label">Jank</div>
                <div class="fc-value" :class="selectedFrame.jank ? 'bad' : 'good'">
                  {{ selectedFrame.jank ? selectedFrame.jankType ?? 'Jank' : '无' }}
                </div>
              </div>
              <div class="fc-cell" title="Present 类型 / 是否按时完成（onTimeFinish）">
                <div class="fc-label">Present</div>
                <div class="fc-value" :class="{ bad: !selectedFrame.onTimeFinish }">{{ selectedFrame.presentType }}</div>
                <div class="fc-sub">onTimeFinish {{ selectedFrame.onTimeFinish ? '✓' : '✗' }}</div>
              </div>
              <div class="fc-cell" title="预期帧时长（expectedDurationMs，vsync 口径）">
                <div class="fc-label">Expected</div>
                <div class="fc-value">{{ selectedFrame.expectedDurationMs }} ms</div>
                <div class="fc-sub">vsync 周期口径</div>
              </div>
              <div class="fc-cell" title="发生时刻（相对数据起点）与进程">
                <div class="fc-label">时刻 / 进程</div>
                <div class="fc-value mono">{{ fmtRel(selectedFrame.tsEpochMs) }}</div>
                <div class="fc-sub">{{ selectedFrame.processName ?? '—' }}<template v-if="selectedFrame.pid !== null && selectedFrame.pid !== undefined"> · pid {{ selectedFrame.pid }}</template></div>
              </div>
            </div>

            <!-- CPU 调度条带：按核泳道、块按线程着色（[I§2.7] CPU Scheduling） -->
            <div class="sched">
              <div class="sched-title">
                CPU Scheduling（该帧附近窗口 · 按核泳道 · 块按线程着色）
                <span class="hint">（数据面 = §22.1 trace sched 片段）</span>
              </div>
              <div v-if="coreLanes.length === 0" class="stat-note">※ 该帧无 CPU 调度片段（trace 无 sched 数据）。</div>
              <template v-else>
                <div class="lane" v-for="lane in coreLanes" :key="lane.key">
                  <span class="lane-name">{{ lane.label }}</span>
                  <div class="lane-track">
                    <span
                      v-for="(b, i) in lane.blocks"
                      :key="i"
                      class="lane-block"
                      :style="{ left: b.leftPct, width: b.widthPct, background: b.color }"
                      :title="b.title"
                    >
                      <span v-if="b.slice.durationMs >= schedWindow!.spanMs * 0.12" class="lane-block-label">
                        {{ b.slice.threadName }}({{ b.slice.tid }})
                      </span>
                    </span>
                  </div>
                </div>
                <div class="lane-ruler">
                  <span>{{ fmtRel(Math.round(schedWindow!.startMs)) }}</span>
                  <span>{{ fmtRel(Math.round(schedWindow!.startMs + schedWindow!.spanMs)) }}</span>
                </div>
                <div class="sched-legend">
                  <span v-for="t in legendThreads" :key="t.name" class="lg-item">
                    <span class="swatch" :style="{ background: t.color }"></span>{{ t.name }}
                  </span>
                </div>
              </template>
            </div>

            <!-- Thread 状态行（Running/Runnable/Sleeping 色带，[I§2.7]） -->
            <div v-if="threadLanes.length > 0" class="sched">
              <div class="sched-title">Thread 状态行（Running<span class="st green">■</span>/Runnable<span class="st yellow">■</span>/Sleeping<span class="st gray">■</span>）</div>
              <div class="lane" v-for="lane in threadLanes" :key="lane.key">
                <span class="lane-name">{{ lane.label }}</span>
                <div class="lane-track">
                  <span
                    v-for="(b, i) in lane.blocks"
                    :key="i"
                    class="lane-block"
                    :style="{ left: b.leftPct, width: b.widthPct, background: b.color }"
                    :title="b.title"
                  ></span>
                </div>
              </div>
            </div>
          </template>
        </div>

        <!-- 帧列表（可按 FTime / 帧号排序；点击 = 选定该帧并联动上方卡与条带） -->
        <div class="deep-detail">
          <div class="report-sub">
            帧列表 ×{{ sortedFrames.length }}
            <span class="hint">（列头点击排序：FTime 默认降序 = 最慢帧在前）</span>
          </div>
          <div ref="listEl" class="frame-list">
            <table class="fl-table">
              <thead>
                <tr>
                  <th class="sortable" @click="toggleSort('frameIndex')">
                    帧号 {{ sortKey === 'frameIndex' ? (sortAsc ? '▲' : '▼') : '' }}
                  </th>
                  <th class="sortable" @click="toggleSort('durationMs')">
                    FTime(ms) {{ sortKey === 'durationMs' ? (sortAsc ? '▲' : '▼') : '' }}
                  </th>
                  <th>Jank</th>
                  <th>Present</th>
                  <th>时刻</th>
                  <th>进程</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="f in sortedFrames"
                  :key="f.frameIndex"
                  class="fl-row"
                  :class="{ selected: f.frameIndex === selectedFrameIndex }"
                  :data-frame-index="f.frameIndex"
                  @click="selectFrame(f.frameIndex)"
                >
                  <td class="mono">{{ f.frameIndex }}</td>
                  <td class="mono" :class="{ slow: f.jank }">{{ f.durationMs.toFixed(2) }}</td>
                  <td>
                    <span v-if="f.jank" class="chip red" :title="f.jankType ?? ''">Jank</span>
                    <span v-else class="chip green">OK</span>
                  </td>
                  <td :title="`${f.presentType} · onTimeFinish=${f.onTimeFinish}`">{{ f.presentType }}</td>
                  <td class="mono">{{ fmtRel(f.tsEpochMs) }}</td>
                  <td class="mono dim" :title="`${f.processName ?? ''} · pid ${f.pid ?? '—'}`">{{ f.processName ?? '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- 根因诊断卡（R51，§22.2）：下降区间 + 相关系数 + Top3 候选；AI 叙述占位 -->
        <div class="deep-detail">
          <div class="report-sub">根因诊断（R51 · §22.2 /diagnostics）</div>
          <RootCauseCard
            :run-id="src?.runId ?? ''"
            :case-name="src?.caseName"
            :anchors="ROOT_CAUSE_ANCHORS"
            :origin-ts="originTs"
          />
        </div>
      </template>

      <!-- CPU 热点火焰图卡（§22 M8+ 扩展 simpleperf 数据面；无工件 → note 原样，不伪造；
           simpleperf 与 trace 独立采集，空 trace ≠ 空 simpleperf，故卡片挂 overview 层） -->
      <div class="deep-detail">
        <div class="report-sub">CPU 热点火焰图（simpleperf · §22 M8+ 扩展）</div>
        <div v-if="!simpleperfFace || !simpleperfFace.available" class="stat-note">
          ※ {{ simpleperfFace?.note ?? '后端未提供 simpleperf 数据面（旧后端无 §22 M8+ 扩展字段）。' }}
        </div>
        <template v-else>
            <div class="flame-stat">
              <span
                v-for="ev in simpleperfStat?.events ?? []"
                :key="ev.event"
                class="chip blue"
                :title="`事件 ${ev.event} · event count ${ev.eventCount ?? '—'}`"
              >{{ ev.event }} ×{{ ev.samples ?? '—' }} 样本</span>
              <span class="chip gray" :title="simpleperfStat?.basis">{{ simpleperfStat?.mode }} · {{ simpleperfStat?.entryCount }} 条目</span>
              <span v-if="(simpleperfStat?.unparsedLines ?? 0) > 0" class="chip gray" title="未能解析的 report 行数（如实计数，不回退伪造）">未解析 {{ simpleperfStat?.unparsedLines }} 行</span>
              <span style="flex: 1"></span>
              <span class="hint">Set left / Set right 后左右对比（红 = 右侧占用更高，绿 = 更低）</span>
              <button class="mini" @click="snapshotFlame('left')">Set left</button>
              <button class="mini" @click="snapshotFlame('right')">Set right</button>
              <button class="mini" @click="clearFlameSnapshots">清除</button>
            </div>
            <div class="flame-snap-hint">
              <span v-if="flameLeftLabel" class="chip gray">左：{{ flameLeftLabel }}</span>
              <span v-if="flameRightLabel" class="chip gray">右：{{ flameRightLabel }}</span>
              <span class="hint">快照存内存（跨 run 浏览保持，页面刷新即失）</span>
            </div>

            <!-- 左右差值视图（两侧都设置过 → 红绿差值着色，对标 [F§9] 调用栈对比） -->
            <div v-if="flameDiffReady" class="flame-view">
              <div class="flame-row flame-diff-head">
                <span class="flame-name">调用栈（按 |右−左| 降序）</span>
                <span class="flame-pct">左 %</span>
                <span class="flame-pct">右 %</span>
                <span class="flame-pct">Δ</span>
              </div>
              <div
                v-for="row in flameDiffRows"
                :key="`${row.name}@${row.depth}`"
                class="flame-row"
                :class="flameDiffTone(row.diff)"
                :style="{ marginLeft: row.depth * 14 + 'px' }"
                :title="`${row.name} · 左 ${row.left.toFixed(2)}% / 右 ${row.right.toFixed(2)}% / Δ ${row.diff.toFixed(2)}pt`"
              >
                <span class="flame-name">{{ row.name }}</span>
                <span class="flame-pct mono">{{ row.left.toFixed(2) }}</span>
                <span class="flame-pct mono">{{ row.right.toFixed(2) }}</span>
                <span class="flame-pct mono">{{ (row.diff >= 0 ? '+' : '') + row.diff.toFixed(2) }}</span>
              </div>
              <div v-if="flameDiffRows.length === 0" class="hint" style="padding: 6px">两侧快照无差值行（调用树为空）。</div>
            </div>

            <!-- 单树 icicle 视图（未设置两侧快照时）：缩进=栈深、条宽=overhead% -->
            <div v-else class="flame-view">
              <div
                v-for="row in flameRows"
                :key="row.key"
                class="flame-row"
                :style="{ marginLeft: row.depth * 14 + 'px' }"
                :title="`${row.name} · ${row.value.toFixed(2)}%（栈深 ${row.depth}）`"
              >
                <span class="flame-bar" :style="{ width: Math.max(2, (row.value / flameRootValue) * 100) + '%' }"></span>
                <span class="flame-name">{{ row.name }}</span>
                <span class="flame-pct mono">{{ row.value.toFixed(2) }}%</span>
              </div>
            </div>
        </template>
      </div>
    </template>
  </div>
</template>

<style scoped>
.deep-wrap {
  padding: 12px 14px 20px;
}

.deep-wrap .report-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.deep-wrap .report-title {
  font-size: 14px;
}

.chip.red {
  background: #e5534b22;
  color: #ff7b72;
}

.chip.green {
  background: #3fb95022;
  color: #7ee787;
}

/* 数据缺失空态（traceAvailable=false → note 原样显示） */
.empty-note {
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  padding: 26px 18px;
  text-align: center;
  color: var(--text-faint);
}

.en-icon {
  font-size: 24px;
  margin-bottom: 6px;
}

.en-title {
  font-size: 13px;
  color: var(--text-dim);
  margin-bottom: 6px;
}

.en-text {
  font-size: 12px;
  line-height: 1.7;
  max-width: 720px;
  margin: 0 auto 6px;
}

.en-hint {
  font-size: 11px;
  color: var(--text-faint);
}

/* 工具行：M 键提示 + 帧号输入 */
.deep-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.deep-toolbar kbd {
  background: var(--bg-panel-2);
  border: 1px solid var(--border);
  border-bottom-width: 2px;
  border-radius: 4px;
  padding: 0 5px;
  font-size: 10.5px;
  font-family: inherit;
}

.locate-box {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--text-dim);
}

.locate-box input {
  width: 92px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text);
  font-size: 11px;
  padding: 2px 5px;
  -moz-appearance: textfield;
}

.locate-box input::-webkit-outer-spin-button,
.locate-box input::-webkit-inner-spin-button {
  -webkit-appearance: none;
}

/* 右键菜单 */
.ctx-menu {
  position: fixed;
  z-index: 30;
  min-width: 220px;
  background: #1a222d;
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.45);
  padding: 4px;
  display: flex;
  flex-direction: column;
}

.ctx-item {
  text-align: left;
  background: none;
  border: none;
  color: var(--text-dim);
  font-size: 12px;
  padding: 6px 10px;
  border-radius: 5px;
  cursor: pointer;
}

.ctx-item:hover:not(:disabled) {
  background: #24303d;
  color: var(--text);
}

.ctx-item:disabled {
  opacity: 0.45;
  cursor: default;
}

/* 单帧卡 */
.deep-detail {
  margin-top: 16px;
}

.deep-detail .report-sub {
  font-size: 11px;
  color: var(--text-faint);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 8px;
}

.deep-detail .report-sub .hint {
  text-transform: none;
  letter-spacing: 0;
}

.frame-card {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

.fc-cell {
  flex: 1;
  min-width: 150px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 8px 10px;
}

.fc-main {
  min-width: 220px;
}

.fc-label {
  font-size: 10.5px;
  color: var(--text-faint);
  margin-bottom: 3px;
}

.fc-value {
  font-size: 15px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.fc-value.bad {
  color: #ff7b72;
}

.fc-value.good {
  color: #7ee787;
}

.fc-sub {
  font-size: 10.5px;
  color: var(--text-faint);
  margin-top: 2px;
  word-break: break-all;
}

/* 调度条带 */
.sched {
  margin-top: 12px;
}

.sched-title {
  font-size: 11px;
  color: var(--text-faint);
  margin-bottom: 6px;
}

.sched-title .hint {
  text-transform: none;
}

.sched-title .st {
  font-size: 9px;
  margin: 0 6px 0 2px;
}

.sched-title .st.green {
  color: #3fb950;
}

.sched-title .st.yellow {
  color: #d29922;
}

.sched-title .st.gray {
  color: #4c5b6b;
}

.lane {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 3px;
}

.lane-name {
  width: 130px;
  flex: none;
  font-size: 10.5px;
  color: var(--text-dim);
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.lane-track {
  position: relative;
  flex: 1;
  height: 16px;
  background: #10161d;
  border-radius: 3px;
  overflow: hidden;
}

.lane-block {
  position: absolute;
  top: 1px;
  bottom: 1px;
  border-radius: 2px;
  overflow: hidden;
  opacity: 0.92;
}

.lane-block:hover {
  opacity: 1;
  outline: 1px solid #d7e0ea;
}

.lane-block-label {
  font-size: 8.5px;
  color: #0d1117;
  padding: 0 2px;
  line-height: 14px;
  white-space: nowrap;
  font-weight: 600;
}

.lane-ruler {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
  margin-left: 138px;
}

.sched-legend {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 6px;
  font-size: 10.5px;
  color: var(--text-dim);
}

.lg-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

/* 帧列表 */
.frame-list {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  max-height: 320px;
  overflow: auto;
}

.fl-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11.5px;
}

.fl-table th {
  position: sticky;
  top: 0;
  background: var(--bg-panel-2);
  text-align: left;
  color: var(--text-faint);
  font-weight: 400;
  padding: 5px 8px;
  border-bottom: 1px solid var(--border);
  z-index: 2;
}

.fl-table th.sortable {
  cursor: pointer;
  user-select: none;
}

.fl-table th.sortable:hover {
  color: var(--text);
}

.fl-table td {
  padding: 3px 8px;
  border-bottom: 1px solid #1c2531;
}

.fl-row {
  cursor: pointer;
}

.fl-row:hover {
  background: #1a222d;
}

.fl-row.selected {
  background: #16283d;
}

.fl-row td.slow {
  color: #ff7b72;
  font-weight: 600;
}

.mono {
  font-variant-numeric: tabular-nums;
}

.dim {
  color: var(--text-faint);
}

/* 火焰图卡（icicle 行 + 左右对比差值着色） */
.flame-stat {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}

.flame-snap-hint {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
  font-size: 10.5px;
  color: var(--text-faint);
}

.flame-view {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  padding: 6px;
  max-height: 320px;
  overflow: auto;
}

.flame-row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  line-height: 18px;
  white-space: nowrap;
  overflow: hidden;
  border-radius: 2px;
}

.flame-row:hover {
  outline: 1px solid #d7e0ea;
}

.flame-bar {
  flex: none;
  height: 10px;
  background: #e5534b66;
  border-radius: 2px;
  min-width: 2px;
}

.flame-name {
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--text-dim);
}

.flame-pct {
  flex: none;
  width: 64px;
  text-align: right;
  color: var(--text-dim);
}

.flame-diff-head {
  color: var(--text-faint);
  font-size: 10px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 2px;
}

.flame-row.flame-worse {
  background: #e5534b22;
}

.flame-row.flame-worse .flame-name {
  color: #ff7b72;
}

.flame-row.flame-better {
  background: #3fb95022;
}

.flame-row.flame-better .flame-name {
  color: #7ee787;
}
</style>
