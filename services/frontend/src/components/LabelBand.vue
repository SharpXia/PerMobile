<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ReplayMarker } from '../api-client'

/**
 * 场景 Label 色带（R28，[I§2.1] 顶部条「SceneTab ｜ 场景Label色带(label1…)」形态）：
 * - 常驻渲染（无 label 时显示占位说明，不是条件消失 —— [I§2.1] 色带是顶部常驻元素）；
 * - 按 label 段着色：段 = 从该 label 起、至下一个 label 或数据结束（[F§6]「持续到下次 setLabel」）；
 * - 双击色区 = 改名（R28；改名走 store.renameLabel —— 契约 §15.2 无 rename 端点，
 *   DELETE + 同 tsMs 同色重建），单击色区 = 对齐游标（与缩略图同一「点击对齐」语义）；
 * - 批注/标定同时以 tick 叠加（note ✎ 紫色 / mark 红竖线），悬停显示文本。
 *
 * 时间域由父组件下发（[fromTs, toTs] = 当前显示窗口），段坐标换算为百分比定位。
 */
const props = defineProps<{
  /** §15.2 会话期标记（label/note/mark，含 id；时间域外的由组件自行过滤） */
  markers: ReplayMarker[]
  /** 色带时间域起点（epoch ms）= 当前显示窗口起点 */
  fromTs: number
  /** 色带时间域终点（epoch ms）= 当前显示窗口终点（≤ fromTs 时以 fromTs+1s 兜底） */
  toTs: number
  /** 可编辑（monitoring/recording/stopped）——否则双击改名不可用 */
  editable?: boolean
}>()

const emit = defineEmits<{
  /** 单击色带空白/色区：对齐游标到该时刻（epoch ms） */
  'seek': [ts: number]
  /** 双击色区：请求改名（markerId + 当前名）；输入框由组件内部渲染，确认后 emit */
  'rename': [markerId: string, name: string]
}>()

/** label 段（段起点 = label 标记时刻，段终点 = 下一 label 时刻或时间域结束 [F§6]） */
interface Segment {
  markerId: string
  name: string
  color: string
  fromTs: number
  toTs: number
}

/** 与 ReplayView 同一色板（label 未显式给色时按序分配，跨视图一致） */
const LABEL_PALETTE = ['#e5534b', '#a371f7', '#2f81f7', '#3fb950', '#d29922']

function paletteAt(i: number): string {
  return LABEL_PALETTE[((i % LABEL_PALETTE.length) + LABEL_PALETTE.length) % LABEL_PALETTE.length] ?? '#a371f7'
}

const domain = computed<[number, number]>(() => {
  const from = props.fromTs
  const to = props.toTs > from ? props.toTs : from + 1000
  return [from, to]
})

const spanMs = computed(() => Math.max(1, domain.value[1] - domain.value[0]))

function pctOf(ts: number): string {
  const clamped = Math.min(Math.max(ts, domain.value[0]), domain.value[1])
  return `${((clamped - domain.value[0]) / spanMs.value) * 100}%`
}

function widthPct(from: number, to: number): string {
  const w = (Math.min(Math.max(to, from), domain.value[1]) - Math.max(from, domain.value[0])) / spanMs.value
  return `${Math.max(0, Math.min(1, w)) * 100}%`
}

const segments = computed<Segment[]>(() => {
  const labels = props.markers
    .filter((m) => m.kind === 'label')
    .sort((a, b) => a.tsMs - b.tsMs)
  const [from, to] = domain.value
  const segs: Segment[] = []
  for (let i = 0; i < labels.length; i++) {
    const m = labels[i]
    if (!m || m.tsMs > to) continue
    const nextTs = labels[i + 1]?.tsMs
    const segTo = Math.min(nextTs ?? to, to)
    if (segTo <= Math.max(m.tsMs, from)) continue
    segs.push({
      markerId: m.id ?? `idx-${i}`,
      name: m.name ?? `label${i + 1}`,
      color: m.color ?? paletteAt(i),
      fromTs: Math.max(m.tsMs, from),
      toTs: segTo,
    })
  }
  return segs
})

const notes = computed(() => props.markers.filter((m) => m.kind === 'note'))
const marks = computed(() => props.markers.filter((m) => m.kind === 'mark'))

/** 相对时间（mm:ss.mmm，原点 = 色带域起点）——tooltip 形态与回放页单点卡一致 [I§2.5] */
function fmtRel(ts: number): string {
  const rel = Math.max(0, ts - domain.value[0])
  const ms = Math.floor(rel % 1000)
  const total = Math.floor(rel / 1000)
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}.${String(ms).padStart(3, '0')}`
}

/* ---------------- R28 双击改名（行内输入框，Enter 确认 / Esc 取消） ---------------- */

const renaming = ref<{ markerId: string; value: string } | null>(null)

function onSegmentDblclick(seg: Segment): void {
  if (!props.editable) return
  renaming.value = { markerId: seg.markerId, value: seg.name }
}

function commitRename(): void {
  const r = renaming.value
  if (!r) return
  const name = r.value.trim()
  if (name) emit('rename', r.markerId, name)
  renaming.value = null
}

function cancelRename(): void {
  renaming.value = null
}

function onBandClick(e: MouseEvent): void {
  const el = e.currentTarget as HTMLElement | null
  if (!el) return
  const rect = el.getBoundingClientRect()
  const ratio = rect.width > 0 ? (e.clientX - rect.left) / rect.width : 0
  emit('seek', Math.round(domain.value[0] + Math.min(Math.max(ratio, 0), 1) * spanMs.value))
}
</script>

<template>
  <div class="label-band" data-band>
    <div
      class="band-canvas"
      title="R28 场景 Label 色带：段 = 打标时刻至下次打标（[F§6]）· 单击对齐游标 · 双击色区改名"
      @click="onBandClick"
    >
      <span v-if="segments.length === 0" class="band-empty">
        （暂无 label 段 —— 工具栏输入段名后「打标」；段按打标时刻起持续到下次打标 [F§6]）
      </span>
      <span
        v-for="seg in segments"
        :key="seg.markerId"
        class="band-seg"
        :style="{ left: pctOf(seg.fromTs), width: widthPct(seg.fromTs, seg.toTs), background: seg.color }"
        :title="`${seg.name} · ${fmtRel(seg.fromTs)} 起${editable ? ' · 双击改名' : ''}`"
        @dblclick.stop="onSegmentDblclick(seg)"
      >
        <input
          v-if="renaming && renaming.markerId === seg.markerId"
          v-model="renaming.value"
          class="band-rename"
          type="text"
          maxlength="40"
          @click.stop
          @dblclick.stop
          @keydown.enter.stop.prevent="commitRename"
          @keydown.esc.stop.prevent="cancelRename"
          @blur="commitRename"
        />
        <span v-else class="band-seg-name">{{ seg.name }}</span>
      </span>
      <!-- 批注 / 标定 tick（与色带同层时间轴；图内另有 markLine 全量渲染） -->
      <span
        v-for="n in notes"
        :key="`n-${n.id ?? n.tsMs}`"
        class="band-note"
        :style="{ left: pctOf(n.tsMs) }"
        :title="`批注 ${fmtRel(n.tsMs)}：${n.text ?? ''}`"
        @click.stop="emit('seek', n.tsMs)"
      >✎</span>
      <span
        v-for="m in marks"
        :key="`m-${m.id ?? m.tsMs}`"
        class="band-mark"
        :style="{ left: pctOf(m.tsMs) }"
        :title="`标定 ${fmtRel(m.tsMs)}`"
        @click.stop="emit('seek', m.tsMs)"
      ></span>
    </div>
    <div class="band-ruler">
      <span>{{ fmtRel(domain[0]) }}</span>
      <span>{{ fmtRel(domain[1]) }}</span>
    </div>
  </div>
</template>

<style scoped>
.label-band {
  user-select: none;
}

.band-canvas {
  position: relative;
  height: 22px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: #10161d;
  cursor: crosshair;
  overflow: hidden;
}

.band-empty {
  font-size: 10px;
  color: var(--text-faint);
  line-height: 22px;
  padding: 0 6px;
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
  pointer-events: none;
}

.band-rename {
  width: 100%;
  min-width: 72px;
  height: 18px;
  margin: 0 2px;
  background: #0d1117;
  border: 1px solid #2f81f7;
  border-radius: 3px;
  color: var(--text);
  font-size: 10px;
  padding: 0 4px;
}

.band-note {
  position: absolute;
  top: 0;
  line-height: 22px;
  font-size: 12px;
  color: #d2a8ff;
  cursor: pointer;
}

.band-mark {
  position: absolute;
  top: 2px;
  bottom: 2px;
  width: 2px;
  transform: translateX(-50%);
  background: #e5534b;
  cursor: pointer;
}

.band-ruler {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
  margin-top: 2px;
}
</style>
