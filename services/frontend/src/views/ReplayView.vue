<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useStore } from '../store'
import { metricMeta } from '../metricMeta'
import MetricChart from '../components/MetricChart.vue'
import type { ChartMarker, ChartSeries } from '../components/MetricChart.vue'
import VideoThumbStrip from '../components/VideoThumbStrip.vue'
import type { ArtifactEntry, MetricPoint, ReplayResponse, Statistics } from '../api-client'

/**
 * 回放视图（R37，M4；[I§2.5] S2-img9 + [I§4.4][I§4.6]）：
 * - 主窗口复用（同布局切换数据源 [I§2.5]）：左栏设备选择器在回放中清空（App.vue 处理），
 *   主区沿用「一个指标一个 Page」堆叠曲线（MetricChart 复用）；
 * - 录屏回放区：有 video 工件时 <video> + 缩略图时间条（VideoThumbStrip：客户端
 *   <video>+canvas 逐段截图，零后端依赖）；无 video 时显示「合成模式或未录屏」占位；
 * - 标记叠加（§15 三类 + §15.5 clockAnchors.markers 代码打点）：label 色带（段着色）/
 *   note 图标 / mark 红竖线，画在共享时间轴色带上并同步叠加进每张图（markLine）；
 * - 悬停单点数据卡（[I§2.5]）：曲线悬停显示该时刻全指标（FPS/统计族/资源指标同卡；
 *   逐点值由 §19 series 供给、汇总值由 §12 统计端点供给）；←/→ 逐采样点移动并同步
 *   全部图表游标与视频 seek（[I§4.6] 逐帧查看）。
 *
 * 视频↔曲线时间映射（[R§2.9]）：video.currentTime = (eventEpochMs -
 * videoFirstFrameEpochMs) / 1000；映射锚点取 §19 clockAnchors.video.firstFrameEpochMs
 * （缺省回退 startEpochMs）。clockAnchors 为 null 时映射不可离线复算 —— 缩略图仅按
 * 视频相对时间浏览，点击/游标不对齐曲线，并如实声明（不猜锚点）。
 */
const store = useStore()

const loading = ref(false)
const errorMsg = ref('')
const replayData = ref<ReplayResponse | null>(null)
const stats = ref<Statistics | null>(null)
let loadSeq = 0

/* ---------------- 加载（§19 一次拉取） ---------------- */

watch(
  () => store.state.replay,
  (src) => {
    replayData.value = null
    stats.value = null
    errorMsg.value = ''
    if (src) void load(src.runId)
  },
  { immediate: true },
)

async function load(runId: string): Promise<void> {
  const seq = ++loadSeq
  loading.value = true
  errorMsg.value = ''
  try {
    const data = await store.getRunReplay(runId, 600)
    if (seq !== loadSeq) return
    replayData.value = data
    // §19 summary 引用不内联：汇总值经 §12 统计端点供给（statisticsUrl 与
    // getRunStatistics 为同一路径 /api/runs/{id}/statistics，阈值走服务端缺省 18,25）
    store
      .getRunStatistics(runId)
      .then((s) => {
        if (seq === loadSeq) stats.value = s
      })
      .catch(() => {
        /* 统计不可得不影响悬停卡逐点值（如实缺省为 '—'） */
      })
    // M7：诊断卡「点击下降区间」→ 回放打开后游标定位该时刻（复用 M4 时间定位，取走即清）
    const seek = store.consumeReplaySeek()
    if (seek !== null && seek >= startTs.value && seek <= axisMax.value) setCursor(seek, true)
  } catch (err) {
    if (seq === loadSeq) errorMsg.value = errText(err)
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

function errText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

/* ---------------- 时间轴域（series 全指标首尾点） ---------------- */

const startTs = computed<number>(() => {
  let min = Infinity
  for (const s of replayData.value?.series ?? []) for (const p of s.points) if (p.ts < min) min = p.ts
  return Number.isFinite(min) ? min : 0
})

const endTs = computed<number>(() => {
  let max = -Infinity
  for (const s of replayData.value?.series ?? []) for (const p of s.points) if (p.ts > max) max = p.ts
  return Number.isFinite(max) ? max : startTs.value
})

/** 图表轴域（axisMin==axisMax 会破坏 ECharts，单点时右端 +1s 兜底） */
const axisMax = computed(() => (endTs.value > startTs.value ? endTs.value : startTs.value + 1000))

const durationText = computed(() => fmtDuration(replayData.value?.durationMs ?? 0))

function fmtDuration(ms: number): string {
  const sec = Math.round(ms / 1000)
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`
}

/** 相对时间轴（mm:ss.mmm，原点 = 首样本；[I§2.5] 数据卡 `00:28:226` 形态） */
function fmtRel(ts: number | null): string {
  if (ts === null) return '—'
  const rel = Math.max(0, ts - startTs.value)
  const ms = Math.floor(rel % 1000)
  const total = Math.floor(rel / 1000)
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}.${String(ms).padStart(3, '0')}`
}

/* ---------------- 图表序列 / 标记 ---------------- */

const chartSeries = computed<ChartSeries[]>(() =>
  (replayData.value?.series ?? []).map((s) => {
    const meta = metricMeta(s.metricId)
    const def = store.state.metricDefs.find((d) => d.metricId === s.metricId)
    return { metricId: s.metricId, label: meta.label, unit: s.unit || def?.unit || '', color: meta.color, points: s.points }
  }),
)

const LABEL_PALETTE = ['#e5534b', '#a371f7', '#2f81f7', '#3fb950', '#d29922']

/** 色板取值（noUncheckedIndexedAccess 下索引访问为 string|undefined，兜底紫色） */
function paletteAt(i: number): string {
  return LABEL_PALETTE[((i % LABEL_PALETTE.length) + LABEL_PALETTE.length) % LABEL_PALETTE.length] ?? '#a371f7'
}

/** §15 sidecar 三类标记 + §15.5 clockAnchors.markers 代码打点（灰色 mark 竖线） */
const chartMarkers = computed<ChartMarker[]>(() => {
  const out: ChartMarker[] = []
  const list = replayData.value?.markers.markers ?? []
  let labelIdx = 0
  for (const m of list) {
    if (m.tsMs < startTs.value || m.tsMs > axisMax.value) continue
    if (m.kind === 'label') {
      out.push({ tsMs: m.tsMs, kind: 'label', color: m.color ?? paletteAt(labelIdx) })
      labelIdx += 1
    } else if (m.kind === 'note') {
      out.push({ tsMs: m.tsMs, kind: 'note' })
    } else {
      out.push({ tsMs: m.tsMs, kind: 'mark' })
    }
  }
  for (const am of replayData.value?.clockAnchors?.markers ?? []) {
    if (am.epochMs >= startTs.value && am.epochMs <= axisMax.value) {
      out.push({ tsMs: am.epochMs, kind: 'mark', color: '#8b98a5' })
    }
  }
  return out
})

const sidecarMarkers = computed(() => replayData.value?.markers.markers ?? [])
const codeMarkers = computed(() => replayData.value?.clockAnchors?.markers ?? [])

/** label 段（从该 label 起、至下一个 label 或数据结束，[F§6]「持续到下次 setLabel」） */
interface LabelSegment {
  name: string
  color: string
  fromTs: number
  toTs: number
}

const labelSegments = computed<LabelSegment[]>(() => {
  const labels = sidecarMarkers.value.filter((m) => m.kind === 'label').sort((a, b) => a.tsMs - b.tsMs)
  const segs: LabelSegment[] = []
  for (let i = 0; i < labels.length; i++) {
    const m = labels[i]
    if (!m || m.tsMs > axisMax.value) continue
    const next = labels[i + 1]?.tsMs
    segs.push({
      name: m.name ?? `label${i + 1}`,
      color: m.color ?? paletteAt(i),
      fromTs: Math.max(m.tsMs, startTs.value),
      toTs: Math.min(next ?? axisMax.value, axisMax.value),
    })
  }
  return segs
})

const notes = computed(() =>
  sidecarMarkers.value.filter((m) => m.kind === 'note' && m.tsMs >= startTs.value && m.tsMs <= axisMax.value),
)
const marks = computed(() =>
  sidecarMarkers.value.filter((m) => m.kind === 'mark' && m.tsMs >= startTs.value && m.tsMs <= axisMax.value),
)

const spanMs = computed(() => Math.max(1, axisMax.value - startTs.value))

function pctOf(ts: number): string {
  return `${((Math.min(Math.max(ts, startTs.value), axisMax.value) - startTs.value) / spanMs.value) * 100}%`
}

/* ---------------- 工件与视频（§17） ---------------- */

const videoArtifacts = computed<ArtifactEntry[]>(() =>
  (replayData.value?.artifacts ?? []).filter((a) => a.kind === 'video'),
)
const screenshotCount = computed(() => (replayData.value?.artifacts ?? []).filter((a) => a.kind === 'screenshot').length)
const dataArtifactCount = computed(() => (replayData.value?.artifacts ?? []).filter((a) => a.kind === 'data').length)

const selectedVideoName = ref<string | undefined>(undefined)
watch(videoArtifacts, (list) => {
  selectedVideoName.value = list.length > 0 ? list[0]!.name : undefined
})

const videoUrl = computed(() => {
  const runId = store.state.replay?.runId
  return runId ? store.getRunVideoUrl(runId, selectedVideoName.value) : ''
})

function thumbUrlFor(name?: string): string {
  const runId = store.state.replay?.runId
  return runId ? store.getRunVideoUrl(runId, name) : ''
}

const videoEl = ref<HTMLVideoElement | null>(null)
const videoDuration = ref(0)
const videoTime = ref(0)
const videoError = ref('')

const mappingAnchorMs = computed<number | null>(() => {
  const v = replayData.value?.clockAnchors?.video
  if (!v) return null
  if (typeof v.firstFrameEpochMs === 'number') return v.firstFrameEpochMs
  if (typeof v.startEpochMs === 'number') return v.startEpochMs
  return null
})

/** 视频时刻（s）→ 曲线时刻（epoch ms）；无锚点为 null（映射不可复算） */
function epochForVideoTime(t: number): number | null {
  const anchor = mappingAnchorMs.value
  return anchor === null ? null : anchor + t * 1000
}

/** 曲线时刻（epoch ms）→ 视频时刻（s）；无锚点/超界为 null */
function videoTimeForEpoch(ts: number): number | null {
  const anchor = mappingAnchorMs.value
  if (anchor === null || videoDuration.value <= 0) return null
  const t = (ts - anchor) / 1000
  return t >= -0.5 && t <= videoDuration.value + 0.5 ? Math.min(Math.max(0, t), videoDuration.value) : null
}

function onThumbSeek(t: number): void {
  const v = videoEl.value
  if (v && Number.isFinite(t)) v.currentTime = t
  const ts = epochForVideoTime(t)
  if (ts !== null && ts >= startTs.value && ts <= axisMax.value) setCursor(ts, true)
}

function onVideoClick(): void {
  const ts = epochForVideoTime(videoTime.value)
  if (ts !== null && ts >= startTs.value && ts <= axisMax.value) setCursor(ts, true)
}

function seekVideoTo(ts: number): void {
  const t = videoTimeForEpoch(ts)
  const v = videoEl.value
  if (t !== null && v) v.currentTime = t
}

/* ---------------- 游标与悬停单点数据卡（[I§2.5][I§4.6]） ---------------- */

const cursorTs = ref<number | null>(null)
const cursorPinned = ref(false)

function setCursor(ts: number, pinned: boolean): void {
  cursorTs.value = ts
  cursorPinned.value = pinned
  seekVideoTo(ts)
}

function onAxisHover(ts: number | null): void {
  if (ts === null) {
    if (!cursorPinned.value) cursorTs.value = null
    return
  }
  cursorPinned.value = false
  cursorTs.value = ts
}

/** 关闭悬停卡（手动 ✕；←/→ 或再次悬停会重新唤起） */
function onCloseCard(): void {
  cursorTs.value = null
  cursorPinned.value = false
}

/** ←/→ 逐采样点锚序列：fps 点列优先，否则点数最多的序列（[I§4.6] 逐帧查看） */
const anchorTsList = computed<number[]>(() => {
  const series = replayData.value?.series ?? []
  if (series.length === 0) return []
  const fps = series.find((s) => s.metricId === 'frame.fps.mean')
  const pick = fps ?? [...series].sort((a, b) => b.points.length - a.points.length)[0]!
  return pick.points.map((p) => p.ts)
})

function nearestIndexOf(ts: number): number {
  const list = anchorTsList.value
  let lo = 0
  let hi = list.length - 1
  let best = -1
  let bestD = Infinity
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    const v = list[mid] ?? 0
    const d = Math.abs(v - ts)
    if (d < bestD) {
      bestD = d
      best = mid
    }
    if (v < ts) lo = mid + 1
    else hi = mid - 1
  }
  return best
}

function onKey(e: KeyboardEvent): void {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
  const target = e.target as HTMLElement | null
  if (target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return
  if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return
  const list = anchorTsList.value
  if (list.length === 0) return
  e.preventDefault()
  const dir = e.key === 'ArrowRight' ? 1 : -1
  let idx = cursorTs.value === null ? -1 : nearestIndexOf(cursorTs.value)
  if (idx === -1) idx = dir === 1 ? 0 : list.length - 1
  else idx = Math.min(list.length - 1, Math.max(0, idx + dir))
  setCursor(list[idx]!, true)
}

onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))

/** band 点击 → 游标对齐（与缩略图同一「点击对齐曲线」语义 [I§2.5]） */
function onBandClick(e: MouseEvent): void {
  const el = e.currentTarget as HTMLElement | null
  if (!el) return
  const rect = el.getBoundingClientRect()
  const ratio = rect.width > 0 ? (e.clientX - rect.left) / rect.width : 0
  const ts = startTs.value + Math.min(Math.max(ratio, 0), 1) * spanMs.value
  setCursor(Math.round(ts), true)
}

/** 该时刻的逐点值（各序列取时间最近样本，[I§2.5] 单点数据卡逐点值由 series 供给） */
function nearestValue(points: MetricPoint[], ts: number): number | null {
  if (points.length === 0) return null
  let lo = 0
  let hi = points.length - 1
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if ((points[mid]?.ts ?? 0) < ts) lo = mid + 1
    else hi = mid
  }
  const a = points[lo]!
  const b = points[Math.max(0, lo - 1)]!
  const pick = Math.abs(a.ts - ts) <= Math.abs(b.ts - ts) ? a : b
  return pick.value
}

interface HoverRow {
  label: string
  color: string
  unit: string
  value: number | null
}

const hoverRows = computed<HoverRow[]>(() => {
  const ts = cursorTs.value
  if (ts === null) return []
  return chartSeries.value.map((s) => ({
    label: s.label,
    color: s.color,
    unit: s.unit,
    value: nearestValue(
      (replayData.value?.series.find((x) => x.metricId === s.metricId)?.points ?? []) as MetricPoint[],
      ts,
    ),
  }))
})

/** 汇总值行（§12 统计层；FPS/统计族与逐点资源指标同卡，[I§2.5]） */
const hoverStatLine = computed<string>(() => {
  const s = stats.value
  if (!s) return ''
  const part = (label: string, v: number | null, unit: string): string =>
    `${label} ${v === null ? '—' : fmtNum(v)}${v === null ? '' : unit}`
  return [
    part('Avg(FPS)', s.fps.avg, ' fps'),
    part('Smooth', s.smooth.value, ''),
    part('1%Low', s.lowFps.p1, ' fps'),
    part('Jank(/10min)', s.jank.per10min, ''),
  ].join(' · ')
})

function fmtNum(v: number): string {
  if (!Number.isFinite(v)) return '—'
  if (Math.abs(v) >= 1000) return v.toFixed(0)
  if (Number.isInteger(v)) return String(v)
  return v.toFixed(1)
}

const downsampledNote = computed(() => {
  const d = replayData.value
  if (!d?.downsampled) return ''
  return `曲线经 LTTB 降采样至 ${d.series[0]?.points.length ?? 0} 点内（sampling=${d.sampling ?? 'lttb'}，§19 maxPoints 上限 600）`
})
</script>

<template>
  <div class="replay-wrap">
    <div class="report-head">
      <button class="ghost" @click="store.closeReplay()">← 退出回放</button>
      <strong class="report-title">📁 {{ store.state.replay?.caseName ?? store.state.replay?.runId }}</strong>
      <span v-if="replayData" class="chip gray">时长 {{ durationText }}</span>
      <span v-if="replayData" class="chip blue">series ×{{ replayData.series.length }}</span>
      <span
        v-if="replayData"
        class="chip gray"
        data-tip="§15 sidecar 标记数（label/note/mark）"
      >
        标记 ×{{ sidecarMarkers.length }}
      </span>
      <span
        v-if="codeMarkers.length > 0"
        class="chip gray"
        title="manifest context.clockAnchors.markers（代码打点，§15.5）"
      >
        代码打点 ×{{ codeMarkers.length }}
      </span>
      <span style="flex: 1"></span>
      <button
        v-if="store.state.replay"
        class="mini"
        title="同一 run 的报告页（数值卡矩阵 + Label 分段统计占位）"
        @click="store.openRunReport(store.state.replay.runId, store.state.replay.caseName)"
      >
        查看报告
      </button>
      <button
        v-if="store.state.replay"
        class="mini"
        title="深度分析（M7 R50，[I§2.7]）：帧时间线 Overview / 单帧卡 / CPU 调度条带 / M 键定位 + 根因诊断卡"
        @click="store.openDeep(store.state.replay.runId, store.state.replay.caseName)"
      >
        深度分析
      </button>
    </div>

    <div v-if="loading" class="empty-hint">回放数据加载中…（§19 /replay 一次拉取）</div>
    <div v-else-if="errorMsg" class="empty-hint">
      回放数据不可用：{{ errorMsg }}
      <template>（live 后端可能未实现契约 §19 /replay 端点）</template>
    </div>

    <template v-else-if="replayData">
      <!-- 录屏回放区：有 video 工件 = <video> + 缩略图时间条；无 = 合成模式/未录屏占位 -->
      <div class="replay-section">
        <div class="report-sub">录屏回放</div>
        <template v-if="videoArtifacts.length > 0">
          <div class="video-row">
            <video
              ref="videoEl"
              class="replay-video"
              :src="videoUrl"
              controls
              muted
              preload="metadata"
              @timeupdate="videoTime = videoEl?.currentTime ?? 0"
              @loadedmetadata="videoDuration = videoEl?.duration ?? 0"
              @error="videoError = '视频取流失败（/video 端点不可用或编解码器不支持）'"
              @click="onVideoClick"
            ></video>
            <div class="video-side">
              <div v-if="videoArtifacts.length > 1" class="video-seg">
                <label>分段</label>
                <select v-model="selectedVideoName">
                  <option v-for="a in videoArtifacts" :key="a.name" :value="a.name">{{ a.name }}</option>
                </select>
              </div>
              <div v-if="mappingAnchorMs === null" class="stat-note">
                ※ 缺少 clockAnchors（manifest context.clockAnchors 为 null）——视频↔曲线时间映射不可离线复算
                [R§2.9]；缩略图仅按视频相对时间浏览，点击不对齐曲线。
              </div>
              <div v-else class="stat-note">
                ※ 映射锚点 video.firstFrameEpochMs={{ mappingAnchorMs }}；video.currentTime =
                (eventEpochMs − 锚点) / 1000（[R§2.9]，可离线复算）。
              </div>
              <div v-if="videoError" class="stat-note">※ {{ videoError }}</div>
              <div class="stat-note">点击视频或缩略图 → 对齐曲线到该时刻（有锚点时）。</div>
            </div>
          </div>
          <VideoThumbStrip
            :videos="videoArtifacts"
            :url-for="thumbUrlFor"
            :selected-name="selectedVideoName"
            :current-time="videoTime"
            @seek="onThumbSeek"
            @duration="videoDuration = $event"
          />
        </template>
        <div v-else class="video-placeholder">
          <div class="vp-icon">🎬</div>
          <div class="vp-title">无录屏视频</div>
          <div class="vp-text">
            该 run 的 artifacts 索引（§17）中无 kind=video 工件 —— 可能是合成模式（synthetic/mock
            不产生真实录屏，契约 §16.1），或该次测试未开启录屏（POST /api/sessions
            recordScreen），或视频被判废剔除（&lt;1KB）。
            <template v-if="screenshotCount > 0">另有截图工件 {{ screenshotCount }} 个、</template>
            <template v-if="dataArtifactCount > 0">数据工件 {{ dataArtifactCount }} 个。</template>
          </div>
        </div>
      </div>

      <!-- 标记时间轴（label 色带 / note 图标 / mark 红竖线，叠加共享时间轴 [I§2.5]） -->
      <div class="replay-section">
        <div class="report-sub">
          标记时间轴（点击色带/标记对齐曲线{{ mappingAnchorMs !== null && videoArtifacts.length > 0 ? '与视频' : '' }}）
        </div>
        <div
          class="marker-band"
          title="label=场景段着色 · note=批注图标 · mark=红竖线标定（§15）；代码打点为灰竖线（§15.5）"
          @click="onBandClick"
        >
          <div class="band-segments">
            <span v-if="labelSegments.length === 0" class="band-empty">（无 label 段标记）</span>
            <span
              v-for="(seg, i) in labelSegments"
              :key="i"
              class="band-seg"
              :style="{
                left: pctOf(seg.fromTs),
                width: `${((seg.toTs - seg.fromTs) / spanMs) * 100}%`,
                background: seg.color,
              }"
              :title="`${seg.name} · ${fmtRel(seg.fromTs)} 起（label 段，[F§6] 持续到下次 setLabel）`"
            >
              <span class="band-seg-name">{{ seg.name }}</span>
            </span>
          </div>
          <div class="band-line">
            <span
              v-for="(n, i) in notes"
              :key="`n${i}`"
              class="band-note"
              :style="{ left: pctOf(n.tsMs) }"
              :title="`批注 ${fmtRel(n.tsMs)}：${n.text ?? ''}`"
              @click.stop="setCursor(n.tsMs, true)"
            >✎</span>
            <span
              v-for="(m, i) in marks"
              :key="`m${i}`"
              class="band-mark"
              :style="{ left: pctOf(m.tsMs) }"
              :title="`mark 标定 ${fmtRel(m.tsMs)}`"
              @click.stop="setCursor(m.tsMs, true)"
            ></span>
            <span
              v-for="(cm, i) in codeMarkers"
              :key="`c${i}`"
              class="band-mark code"
              :style="{ left: pctOf(cm.epochMs) }"
              :title="`代码打点 ${cm.name} · ${fmtRel(cm.epochMs)}（clockAnchors.markers，§15.5）`"
              @click.stop="setCursor(cm.epochMs, true)"
            ></span>
            <span v-if="cursorTs !== null" class="band-cursor" :style="{ left: pctOf(cursorTs) }"></span>
          </div>
          <div class="band-ruler">
            <span>{{ fmtRel(startTs) }}</span>
            <span>{{ fmtRel(axisMax) }}</span>
          </div>
        </div>
        <div
          v-if="sidecarMarkers.length === 0 && codeMarkers.length === 0"
          class="stat-note"
          style="margin-top: 4px"
        >
          ※ 该 run 无任何标记（§15.3：无标记是常态，不是错误；测试期打标属 M5/R28-R29）。
        </div>
      </div>

      <!-- 曲线区（一个指标一个 Page 复用；游标/标记叠加 + 悬停上报） -->
      <div class="replay-section">
        <div class="report-sub">
          指标曲线（←/→ 逐采样点移动，同步全部图表与视频；悬停出单点数据卡）
          <span v-if="anchorTsList.length > 0" class="hint">（锚序列 {{ anchorTsList.length }} 点）</span>
        </div>
        <div v-if="downsampledNote" class="stat-note" style="margin-bottom: 6px">※ {{ downsampledNote }}</div>
        <div class="replay-charts">
          <div v-if="chartSeries.length === 0" class="empty-hint">
            无样本数据（durationMs={{ replayData.durationMs }}）——该 run 未产生采样点。
          </div>
          <MetricChart
            v-for="s in chartSeries"
            :key="s.metricId"
            :title="s.label"
            :series="[s]"
            :start-ts="startTs"
            :axis-min-ts="startTs"
            :axis-max-ts="axisMax"
            :height="180"
            :cursor-ts="cursorTs ?? undefined"
            :markers="chartMarkers"
            hover-enabled
            @axis-hover="onAxisHover"
          />

          <!-- 悬停单点数据卡（全指标同卡，[I§2.5]；逐点值 ← §19 series，汇总值 ← §12 /statistics） -->
          <div v-if="cursorTs !== null && hoverRows.length > 0" class="hover-card">
            <div class="hc-head">
              <span class="hc-time">{{ fmtRel(cursorTs) }}</span>
              <span v-if="cursorPinned" class="chip blue">←/→ 定位</span>
              <span v-else class="chip gray">悬停</span>
              <button class="close" @click="onCloseCard">✕</button>
            </div>
            <div class="hc-body">
              <div v-for="row in hoverRows" :key="row.label" class="hc-row">
                <span class="swatch" :style="{ background: row.color }"></span>
                <span class="hc-label">{{ row.label }}</span>
                <span class="hc-value">{{ row.value === null ? '—' : fmtNum(row.value) }}</span>
                <span v-if="row.unit" class="hc-unit">{{ row.unit }}</span>
              </div>
            </div>
            <div v-if="hoverStatLine" class="hc-stats">{{ hoverStatLine }}</div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.replay-wrap {
  padding: 12px 14px 20px;
}

.replay-wrap .report-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.replay-wrap .report-title {
  font-size: 14px;
}

.replay-section {
  margin-top: 14px;
}

.replay-section .report-sub {
  font-size: 11px;
  color: var(--text-faint);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 8px;
}

/* ---- 视频区 ---- */

.video-row {
  display: flex;
  gap: 12px;
  align-items: stretch;
}

.replay-video {
  width: 320px;
  max-width: 46%;
  aspect-ratio: 9 / 19.5;
  max-height: 300px;
  background: #05070a;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  flex: none;
  cursor: crosshair;
}

.video-side {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.video-seg {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}

.video-seg label {
  color: var(--text-dim);
}

.video-placeholder {
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  padding: 26px 16px;
  text-align: center;
  color: var(--text-faint);
}

.vp-icon {
  font-size: 26px;
  margin-bottom: 6px;
}

.vp-title {
  font-size: 13px;
  color: var(--text-dim);
  margin-bottom: 6px;
}

.vp-text {
  font-size: 12px;
  line-height: 1.7;
  max-width: 640px;
  margin: 0 auto;
}

/* ---- 标记时间轴 ---- */

.marker-band {
  position: relative;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  padding: 6px 8px 4px;
  cursor: crosshair;
  user-select: none;
}

.band-segments {
  position: relative;
  height: 16px;
  border-radius: 4px;
  overflow: hidden;
  background: #10161d;
}

.band-seg {
  position: absolute;
  top: 0;
  bottom: 0;
  opacity: 0.55;
  display: flex;
  align-items: center;
  overflow: hidden;
}

.band-seg-name {
  font-size: 10px;
  color: #fff;
  padding: 0 4px;
  white-space: nowrap;
  text-shadow: 0 1px 2px rgba(0, 0, 0, 0.7);
}

.band-empty {
  font-size: 10px;
  color: var(--text-faint);
  padding: 1px 6px;
}

.band-line {
  position: relative;
  height: 16px;
  margin-top: 2px;
}

.band-note {
  position: absolute;
  top: 0;
  transform: translateX(-50%);
  font-size: 12px;
  line-height: 16px;
  color: #d2a8ff;
  cursor: pointer;
}

.band-mark {
  position: absolute;
  top: 1px;
  bottom: 1px;
  width: 2px;
  transform: translateX(-50%);
  background: #e5534b;
  cursor: pointer;
}

.band-mark.code {
  background: #8b98a5;
  width: 2px;
  opacity: 0.8;
}

.band-cursor {
  position: absolute;
  top: -18px;
  bottom: -2px;
  width: 2px;
  transform: translateX(-50%);
  background: #2f81f7;
  pointer-events: none;
}

.band-ruler {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}

/* ---- 曲线区 / 悬停数据卡 ---- */

.replay-charts {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.hover-card {
  /* 浮在曲线区右上（避开图例列），随游标即时可见 —— [I§2.5] 单点数据卡形态 */
  position: absolute;
  top: 8px;
  right: 136px;
  z-index: 5;
  width: 300px;
  background: #1a222dee;
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.45);
  padding: 8px 10px;
}

.hc-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}

.hc-time {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
  font-size: 12.5px;
  color: #79b8ff;
}

.hc-head .close {
  margin-left: auto;
  background: none;
  border: none;
  color: var(--text-faint);
  cursor: pointer;
  padding: 0 2px;
}

.hc-head .close:hover {
  color: var(--text);
}

.hc-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 220px;
  overflow-y: auto;
}

.hc-row {
  display: flex;
  align-items: baseline;
  gap: 6px;
  font-size: 12px;
}

.hc-row .swatch {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  flex: none;
  align-self: center;
}

.hc-label {
  flex: 1;
  min-width: 0;
  color: var(--text-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hc-value {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}

.hc-unit {
  font-size: 10px;
  color: var(--text-faint);
  min-width: 26px;
}

.hc-stats {
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px solid var(--border);
  font-size: 10.5px;
  color: var(--text-faint);
  line-height: 1.6;
}
</style>
