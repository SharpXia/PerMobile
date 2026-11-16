<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useStore } from '../store'
import { baseMetricId, metricMeta } from '../metricMeta'
import { thresholdSpecOf } from '../thresholds'
import MetricChart from '../components/MetricChart.vue'
import type { BrushRange, ChartMarker, ChartSeries } from '../components/MetricChart.vue'
import ValueCards from '../components/ValueCards.vue'
import LabelBand from '../components/LabelBand.vue'
import RangeStatCard from '../components/RangeStatCard.vue'
import type { Statistics } from '../api-client'

/**
 * 实时监控视图（M5 交互 R28-R32，对标 [I§2.1]/[I§2.4]）：
 * - R28：工具栏打标按钮（输入框默认段名 labelN）+ 顶部 Label 色带常驻、按段着色、
 *   双击色区改名（改名走 store.renameLabel = DELETE+同 tsMs 同色重建，契约 §15.2 自由度）；
 * - R29：双击曲线 = 批注（输入文字；双击已有批注 = 删除）、单击（非拖动）= 标定红竖线
 *   —— 单击/框选/双击的命中判定在 MetricChart 内实现（见其 onCanvasDblclick/
 *   onCanvasPointerUp 注释），本视图只接事件并落 §15.2 端点；
 * - R30：ECharts brush lineX 框选（每图启用），选区跨图同步 = 共享 brushRange 下发到
 *   全部图（markArea 渲染，等价 echarts.connect 语义但对 setOption 重渲染稳健），
 *   浮出区间统计卡（§12/§18 statistics?fromMs&toMs），卡上 Save = 写一条 note（§15.2）；
 * - R31：每图右上阈值输入 + 一键重置（默认 FPS>=18/25、FTime>=100ms），变更写回
 *   store.thresholds，联动数值卡达标着色与区间统计占比（fpsGe 查询参数）。
 */
const store = useStore()

const activeIds = computed<string[]>(() => {
  const s = store.state.session
  if (s && s.state !== 'saved' && s.state !== 'discarded') return s.metrics.map((m) => m.metricId)
  return Object.keys(store.state.selected)
})

/**
 * M6（§21.2）：带标签指标（cpu.freq.mhz per-core / cpu.proc.thread.pct per-thread）
 * 在 store 中以 `metricId@tag` 分序列 —— 显示时展开为多图（每核/每线程一条曲线）；
 * 无标签指标保持单序列。
 */
const displayKeys = computed<string[]>(() => {
  const out: string[] = []
  for (const id of activeIds.value) {
    const tagged = Object.keys(store.state.series)
      .filter((k) => k.startsWith(`${id}@`))
      .sort()
    if (tagged.length > 0) out.push(...tagged)
    else out.push(id)
  }
  // 拖拽顺序覆盖（UI 反馈 #4）：chartOrder 里的键按用户拖出的次序排前，
  // 未收录的键（新勾选/标签）保持缺省序跟在后面——不丢指标只调顺序
  const order = store.state.chartOrder
  if (order.length > 0) {
    const idx = new Map(order.map((k, i) => [k, i]))
    out.sort((a, b) => (idx.get(a) ?? Number.POSITIVE_INFINITY) - (idx.get(b) ?? Number.POSITIVE_INFINITY))
  }
  return out
})

/** 拖卡换位（ValueCards emit）：按「拖动卡插入目标卡位置」重排 chartOrder */
function onCardsReorder(draggedId: string, targetId: string): void {
  const keys = displayKeys.value
  const from = keys.indexOf(draggedId)
  const to = keys.indexOf(targetId)
  if (from < 0 || to < 0 || from === to) return
  const next = [...keys]
  next.splice(from, 1)
  next.splice(to, 0, draggedId)
  store.setChartOrder(next)
}

const chartSeries = computed<ChartSeries[]>(() =>
  displayKeys.value.map((key) => {
    const meta = metricMeta(key) // `metricId@tag` 由 metricMeta 剥离标签回查元数据
    const def = store.state.metricDefs.find((d) => d.metricId === baseMetricId(key))
    const tag = key.includes('@') ? key.slice(key.indexOf('@') + 1) : ''
    return {
      metricId: key,
      label: tag ? `${meta.label} · ${tag}` : meta.label,
      unit: def?.unit ?? '',
      color: meta.color,
      points: store.state.series[key] ?? [],
    }
  }),
)

const startTs = computed<number | undefined>(() => store.state.session?.createdAt)

/** §15.2 可编辑窗口（monitoring/recording/stopped；saved/discarded 409） */
const editable = computed(() => store.markersEditable())
/** 框选可用：有会话即可（stopped 后仍可框选补统计）；无会话无统计端点 */
const brushAvailable = computed(() => store.state.session !== null)

const connectionText = computed(() => {
  switch (store.state.connection) {
    case 'open':
      return '已连接'
    case 'connecting':
      return '连接中'
    case 'error':
      return '已断开（重连中）'
    default:
      return '未开始'
  }
})

const connectionCls = computed(() => {
  switch (store.state.connection) {
    case 'open':
      return 'green'
    case 'connecting':
      return 'orange'
    case 'error':
      return 'red'
    default:
      return 'gray'
  }
})

function onOverlayChange(ev: Event): void {
  store.setOverlay((ev.target as HTMLInputElement).checked)
}

/* ---------------- R28：Label 色带与打标 ---------------- */

/** 色带/标记时间域 = 当前显示窗口（series 首尾点；无数据时回退「会话起点→现在」或最近 2 分钟） */
const bandFrom = computed<number>(() => {
  let min = Infinity
  for (const s of chartSeries.value) for (const p of s.points) if (p.ts < min) min = p.ts
  if (Number.isFinite(min)) return min
  return store.state.session?.createdAt ?? store.state.now - 120_000
})

const bandTo = computed<number>(() => {
  let max = -Infinity
  for (const s of chartSeries.value) for (const p of s.points) if (p.ts > max) max = p.ts
  return Number.isFinite(max) ? max : store.state.now
})

/** R28 默认段名：label1/label2/…（[I§2.8] PerfDog label1/label2 形态；已有同名的继续递增） */
const labelInput = ref('')

function defaultLabelName(): string {
  const used = new Set(store.state.markers.filter((m) => m.kind === 'label').map((m) => m.name ?? ''))
  let n = 1
  while (used.has(`label${n}`)) n += 1
  return `label${n}`
}

// 无输入时输入框显示下一个默认段名（可改）
watch(
  () => [store.state.markers.length, store.state.session?.id],
  () => {
    if (!labelInput.value.trim()) labelInput.value = defaultLabelName()
  },
  { immediate: true },
)

async function addLabelFromToolbar(): Promise<void> {
  const name = labelInput.value.trim() || defaultLabelName()
  const created = await store.addLabel({ name })
  if (created) labelInput.value = defaultLabelName()
}

/** 色带/图内标记叠加（§15.2 + 时域过滤；label 色按已有标记自带色，缺省由色带按序分配） */
const chartMarkers = computed<ChartMarker[]>(() => {
  const out: ChartMarker[] = []
  for (const m of store.state.markers) {
    if (m.tsMs < bandFrom.value || m.tsMs > bandTo.value) continue
    if (m.kind === 'label') {
      out.push({ tsMs: m.tsMs, kind: 'label', color: m.color, text: m.name })
    } else if (m.kind === 'note') {
      out.push({ tsMs: m.tsMs, kind: 'note', text: m.text })
    } else {
      out.push({ tsMs: m.tsMs, kind: 'mark' })
    }
  }
  return out
})

/** 色带单击对齐的临时游标（2.5s 自动消失；仅观察用途） */
const seekTs = ref<number | null>(null)

function onBandSeek(ts: number): void {
  // 色带单击 = 对齐游标（与回放页同一语义；监控页无固定游标渲染——
  // 单击仅用于快速定位观察，不打标不写库）
  seekTs.value = ts
  setTimeout(() => {
    seekTs.value = null
  }, 2500)
}

/* ---------------- R29：批注（双击）与标定（单击） ---------------- */

const pendingNoteTs = ref<number | null>(null)
const noteText = ref('')
const noteError = ref('')

function onNoteCreate(ts: number): void {
  if (!editable.value) {
    store.setError('当前会话状态不可打批注（saved/discarded 已收口，§15.2 409）')
    return
  }
  pendingNoteTs.value = ts
  noteText.value = ''
  noteError.value = ''
}

async function commitNote(): Promise<void> {
  const ts = pendingNoteTs.value
  const text = noteText.value.trim()
  if (ts === null) return
  if (!text) {
    noteError.value = '批注文字不能为空（空文本 = 取消，与双击空白处新建语义一致）'
    return
  }
  const created = await store.addNote({ tsMs: ts, text })
  if (created) {
    pendingNoteTs.value = null
    noteText.value = ''
  } else {
    noteError.value = '批注创建失败（见顶部错误条）'
  }
}

function cancelNote(): void {
  pendingNoteTs.value = null
  noteText.value = ''
  noteError.value = ''
}

/** 双击已有批注 = 删除（MetricChart 命中判定命中 note 后 emit 其 tsMs） */
function onNoteDelete(noteTs: number): void {
  const target = store.state.markers.find((m) => m.kind === 'note' && m.tsMs === noteTs)
  if (!target) return
  void store.removeMarker(target.id)
}

/** 单击（非拖动）= 标定红竖线（MetricChart 已做拖拽/双击消歧） */
function onMarkCreate(ts: number): void {
  if (!editable.value) {
    store.setError('当前会话状态不可打标定（saved/discarded 已收口，§15.2 409）')
    return
  }
  void store.addMark({ tsMs: ts })
}

/* ---------------- R30：brush 框选区间统计 ---------------- */

const brushRange = ref<BrushRange | null>(null)
const rangeStats = ref<Statistics | null>(null)
const rangeLoading = ref(false)
const rangeError = ref('')
const rangeSaveBusy = ref(false)
const rangeSavedText = ref('')
let rangeSeq = 0

async function fetchRangeStats(range: BrushRange): Promise<void> {
  const s = store.state.session
  if (!s) return
  const seq = ++rangeSeq
  rangeLoading.value = true
  rangeError.value = ''
  try {
    // §18：fpsGe 随 R31 阈值输入联动（占比行/列同步变化）
    const stats = await store.getSessionStatistics(s.id, {
      fromMs: range.fromMs,
      toMs: range.toMs,
      fpsGe: store.state.thresholds.fpsGe,
    })
    if (seq !== rangeSeq) return
    rangeStats.value = stats
  } catch (err) {
    if (seq !== rangeSeq) return
    rangeStats.value = null
    rangeError.value = err instanceof Error ? err.message : String(err)
  } finally {
    if (seq === rangeSeq) rangeLoading.value = false
  }
}

function onBrushRange(range: BrushRange): void {
  brushRange.value = range
  rangeSavedText.value = ''
  void fetchRangeStats(range)
}

function clearBrush(): void {
  brushRange.value = null
  rangeStats.value = null
  rangeError.value = ''
  rangeSavedText.value = ''
  rangeSeq += 1 // 作废在途请求
}

/** R31：阈值变更 → 区间统计占比联动重取（选区打开时） */
watch(
  () => store.state.thresholds.fpsGe,
  () => {
    if (brushRange.value) void fetchRangeStats(brushRange.value)
  },
)

/** 区间卡 Save = 把区间统计写为一条批注（§15.2 POST /notes，tsMs = 区间起点 [I§4.4]） */
async function saveRangeAsNote(): Promise<void> {
  const range = brushRange.value
  const s = rangeStats.value
  if (!range || !s) return
  const num = (v: number | null, digits = 1): string => (v === null || !Number.isFinite(v) ? '—' : v.toFixed(digits))
  const ge = Object.keys(s.fps.ge)
    .sort((a, b) => Number(a) - Number(b))
    .map((t) => `FPS>=${t} ${s.fps.ge[t] === null || s.fps.ge[t] === undefined ? '—' : `${num(s.fps.ge[t]!)}%`}`)
    .join('，')
  const text = `区间统计 ${fmtRel(range.fromMs)}–${fmtRel(range.toMs)}：Avg(FPS) ${num(s.fps.avg)}，Std ${num(s.fps.std)}，Min ${num(s.fps.min)}，${ge}，Jank ${num(s.jank.jank, 0)}，BigJank ${num(s.jank.big, 0)}，Stutter ${num(s.jank.stutter)}%`
  rangeSaveBusy.value = true
  try {
    const created = await store.addNote({ tsMs: range.fromMs, text })
    if (created) rangeSavedText.value = fmtRel(created.tsMs)
  } finally {
    rangeSaveBusy.value = false
  }
}

/* ---------------- R31：阈值输入联动 ---------------- */

function onThresholdCommit(patch: { spec: 'fps' | 'ftime'; fpsGe?: number[]; ftimeMs?: number }): void {
  if (patch.spec === 'fps' && patch.fpsGe) store.setFpsThresholds(patch.fpsGe)
  else if (patch.spec === 'ftime' && patch.ftimeMs !== undefined) store.setFtimeThreshold(patch.ftimeMs)
}

/* ---------------- 相对时间（mm:ss.mmm，原点 = 会话起点，[I§2.5] 数据卡形态） ---------------- */

function fmtRel(ts: number): string {
  const origin = startTs.value ?? bandFrom.value
  const rel = Math.max(0, ts - origin)
  const ms = Math.floor(rel % 1000)
  const total = Math.floor(rel / 1000)
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}.${String(ms).padStart(3, '0')}`
}
</script>

<template>
  <div>
    <div class="monitor-toolbar">
      <!-- R28：打标按钮 + 默认段名输入框（[I§2.1] 顶部工具 📍 位置语义） -->
      <span v-if="editable" class="label-input-wrap" title="R28 场景打标：段名（默认 labelN 递增）→ 打标即在当前时刻切段，色带按段着色 [F§6]">
        <input
          v-model="labelInput"
          class="label-input"
          type="text"
          maxlength="40"
          placeholder="段名（label1…）"
          @keydown.enter="addLabelFromToolbar"
        />
        <button class="mini label-btn" @click="addLabelFromToolbar">📍 打标</button>
      </span>
      <span v-else class="chip gray" title="saved/discarded 会话已收口（sidecar 冻结，§15.2），不可再打标">标记只读（已收口）</span>

      <label style="display: flex; align-items: center; gap: 6px; cursor: pointer">
        <input type="checkbox" :checked="store.state.overlay" @change="onOverlayChange" />
        <span>叠加模式（多指标同图）</span>
      </label>
      <span class="chip" :class="connectionCls">实时通道：{{ connectionText }}</span>
      <span class="hint">滚动窗口约 5 分钟 · 拖动数值卡=调面板顺序 · 滚轮滚页面（Shift+滚轮缩放曲线）· 双击曲线=批注 · 单击=标定 · 拖出选区=区间统计 · 图右上可调阈值</span>
      <span style="flex: 1"></span>
      <button v-if="store.state.session?.state === 'stopped'" class="mini" @click="store.openSessionReport()">
        统计报告（/statistics 预览）
      </button>
      <span v-if="store.state.session" class="chip blue">
        {{ store.state.session.id }} · {{ store.state.session.packageName }}
      </span>
    </div>

    <!-- R28：顶部 Label 色带（常驻渲染；无段时显示占位说明 [I§2.1] 色带为顶部常驻元素） -->
    <div class="monitor-band-row">
      <LabelBand
        :markers="store.state.markers"
        :from-ts="bandFrom"
        :to-ts="bandTo"
        :editable="editable"
        @seek="onBandSeek"
        @rename="(id, name) => store.renameLabel(id, name)"
      />
      <span v-if="seekTs !== null" class="band-seek-chip mono">游标 {{ fmtRel(seekTs) }}</span>
    </div>

      <ValueCards v-if="chartSeries.length > 0" :metric-ids="displayKeys" @reorder="onCardsReorder" />

    <div class="monitor-charts-wrap">
      <div v-if="chartSeries.length > 0" class="chart-pages">
        <MetricChart
          v-if="store.state.overlay && chartSeries.length > 1"
          title="叠加视图（按单位分轴）"
          :series="chartSeries"
          :start-ts="startTs"
          :height="320"
          :markers="chartMarkers"
          :brush-enabled="brushAvailable"
          :brush-range="brushRange"
          :interactive="editable"
          :thresholds="store.state.thresholds"
          @brush-range="onBrushRange"
          @note-create="onNoteCreate"
          @note-delete="onNoteDelete"
          @mark-create="onMarkCreate"
        />
        <template v-else>
          <MetricChart
            v-for="s in chartSeries"
            :key="s.metricId"
            :title="s.label"
            :series="[s]"
            :start-ts="startTs"
            :markers="chartMarkers"
            :brush-enabled="brushAvailable"
            :brush-range="brushRange"
            :interactive="editable"
            :threshold-spec="thresholdSpecOf(baseMetricId(s.metricId))"
            :thresholds="store.state.thresholds"
            @brush-range="onBrushRange"
            @note-create="onNoteCreate"
            @note-delete="onNoteDelete"
            @mark-create="onMarkCreate"
            @threshold-commit="onThresholdCommit"
          />
        </template>
      </div>
      <div v-else class="records-wrap">
        <div class="empty-hint">
          尚未选择指标 —— 点击左栏「＋ 指标面板」勾选要采集的指标（勾选同时控制采集与显示），
          然后点「开始监控」建立会话；无真机时可使用默认的模拟数据模式查看实时曲线。
        </div>
      </div>

      <!-- R29：批注输入（双击曲线唤起；Enter 提交 / Esc 取消；空白文字 = 取消） -->
      <div v-if="pendingNoteTs !== null" class="note-pop">
        <div class="note-pop-head">
          <span>新增批注（双击位置 {{ fmtRel(pendingNoteTs) }}）</span>
          <button class="close" title="取消（Esc）" @click="cancelNote">✕</button>
        </div>
        <input
          v-model="noteText"
          class="note-pop-input"
          type="text"
          maxlength="200"
          placeholder="批注文字（Enter 提交）"
          autofocus
          @keydown.enter.prevent="commitNote"
          @keydown.esc.prevent="cancelNote"
        />
        <div v-if="noteError" class="note-pop-err">{{ noteError }}</div>
        <div class="note-pop-actions">
          <button class="mini" @click="commitNote">提交</button>
          <button class="mini" @click="cancelNote">取消</button>
          <span class="hint">提示：双击已有批注 = 删除该批注（命中半径 ±14px）</span>
        </div>
      </div>

      <!-- R30：区间统计卡（选区后浮出；Save 写 note；跨图选区由共享 brushRange 同步渲染） -->
      <div v-if="brushRange !== null" class="range-card-pos">
        <RangeStatCard
          :from-ms="brushRange.fromMs"
          :to-ms="brushRange.toMs"
          :origin-ts="startTs ?? bandFrom"
          :stats="rangeStats"
          :loading="rangeLoading"
          :error-msg="rangeError"
          :can-save="editable"
          :save-busy="rangeSaveBusy"
          :saved-text="rangeSavedText"
          :fps-note="rangeStats?.fps.note"
          @save="saveRangeAsNote"
          @close="clearBrush"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
/* R28：工具栏打标控件 */
.label-input-wrap {
  display: flex;
  align-items: center;
  gap: 4px;
}

.label-input {
  width: 110px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--text);
  font-size: 11.5px;
  padding: 3px 7px;
}

.label-btn {
  white-space: nowrap;
}

.monitor-band-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 6px 0 10px;
}

.monitor-band-row > :first-child {
  flex: 1;
  min-width: 0;
}

.band-seek-chip {
  flex: none;
  font-size: 10.5px;
  color: #79b8ff;
  border: 1px solid rgba(47, 129, 247, 0.55);
  border-radius: 5px;
  padding: 1px 6px;
  font-variant-numeric: tabular-nums;
}

.monitor-charts-wrap {
  position: relative;
}

/* R29 批注输入浮层（曲线区顶部居中，不随游标；操作完成即收起） */
.note-pop {
  position: absolute;
  top: 40px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 8;
  width: 380px;
  max-width: 90%;
  background: #1a222dee;
  border: 1px solid rgba(210, 168, 255, 0.5);
  border-radius: 8px;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.45);
  padding: 8px 10px;
}

.note-pop-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  color: #d2a8ff;
  margin-bottom: 6px;
}

.note-pop-head .close {
  background: none;
  border: none;
  color: var(--text-faint);
  cursor: pointer;
}

.note-pop-input {
  width: 100%;
  box-sizing: border-box;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 5px;
  color: var(--text);
  font-size: 12px;
  padding: 4px 8px;
}

.note-pop-err {
  font-size: 10.5px;
  color: #f85149;
  margin-top: 4px;
}

.note-pop-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
}

.note-pop-actions .hint {
  margin-left: auto;
}

/* R30 区间统计卡：浮于曲线区右上（不遮挡首图标题；随选区出现/消失） */
.range-card-pos {
  position: absolute;
  top: 52px;
  right: 8px;
  z-index: 7;
  width: 560px;
  max-width: min(92%, 640px);
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
</style>
