<script setup lang="ts">
import { computed, ref } from 'vue'
import { useStore } from '../store'
import { baseMetricId, metricMeta } from '../metricMeta'
import { fpsMeetsThreshold, ftimeMeetsThreshold, thresholdSpecOf, type ThresholdState } from '../thresholds'

/**
 * 数值卡（对标 [I§4.4] 数值卡体系：实时层当前值卡）；无数据显示 —，不写 0。
 * R31：有 TDR 口径线（FPS/FTime）的指标卡联动阈值达标着色（fps≥最低档 / ftime≤阈值）。
 * UI 反馈 #4：卡片可拖拽换位 → emit reorder(拖动卡, 目标卡)，父组件联动下方图表面板顺序。
 */
const store = useStore()

const props = defineProps<{ metricIds: string[] }>()

const emit = defineEmits<{ (e: 'reorder', draggedId: string, targetId: string): void }>()

const dragId = ref<string | null>(null)
const overId = ref<string | null>(null)

function onDragStart(id: string, ev: DragEvent): void {
  dragId.value = id
  if (ev.dataTransfer) {
    ev.dataTransfer.setData('text/plain', id)
    ev.dataTransfer.effectAllowed = 'move'
  }
}

function onDragEnter(id: string): void {
  if (dragId.value && dragId.value !== id) overId.value = id
}

function onDrop(id: string, ev: DragEvent): void {
  ev.preventDefault()
  if (dragId.value && dragId.value !== id) emit('reorder', dragId.value, id)
  dragId.value = null
  overId.value = null
}

function onDragEnd(): void {
  dragId.value = null
  overId.value = null
}

interface CardItem {
  id: string
  label: string
  unit: string
  color: string
  value: string
  direction: string
  evidence: string
  /** R31 阈值达标着色（null = 该指标无口径线，不着色） */
  tone: 'good' | 'bad' | null
  toneText: string
}

const items = computed<CardItem[]>(() =>
  props.metricIds.map((id) => {
    const meta = metricMeta(id) // M6：标签序列键 `metricId@tag` 由 metricMeta 内部剥离回查
    const def = store.state.metricDefs.find((d) => d.metricId === baseMetricId(id))
    const latest = store.state.latest[id]
    const spec = thresholdSpecOf(id)
    const t: ThresholdState = store.state.thresholds
    let tone: 'good' | 'bad' | null = null
    let toneText = ''
    if (spec === 'fps' && latest !== undefined && latest !== null) {
      tone = fpsMeetsThreshold(latest, t) ? 'good' : 'bad'
      toneText = tone === 'good' ? `≥${Math.min(...t.fpsGe)} 达标` : `<${Math.min(...t.fpsGe)} 未达标`
    } else if (spec === 'ftime' && latest !== undefined && latest !== null) {
      tone = ftimeMeetsThreshold(latest, t) ? 'good' : 'bad'
      toneText = tone === 'good' ? `≤${t.ftimeMs}ms 达标` : `>${t.ftimeMs}ms 超线`
    }
    return {
      id,
      label: meta.label,
      unit: def?.unit ?? '',
      color: meta.color,
      value: latest === undefined || latest === null ? '—' : fmt(latest),
      direction: def?.direction ?? 'lower_better',
      evidence: def?.evidenceLevel ?? '',
      tone,
      toneText,
    }
  }),
)

function fmt(v: number): string {
  if (Math.abs(v) >= 1000) return v.toFixed(0)
  if (Number.isInteger(v)) return String(v)
  return v.toFixed(1)
}
</script>

<template>
  <div class="value-cards">
    <div
      v-for="it in items"
      :key="it.id"
      class="value-card"
      :class="{ good: it.tone === 'good', bad: it.tone === 'bad', dragging: it.id === dragId, 'drop-hint': it.id === overId }"
      draggable="true"
      :title="`拖动卡片调整下方图表面板顺序（${it.label}）`"
      @dragstart="onDragStart(it.id, $event)"
      @dragenter.prevent="onDragEnter(it.id)"
      @dragover.prevent
      @drop.prevent="onDrop(it.id, $event)"
      @dragend="onDragEnd"
    >
      <div class="vc-label">
        {{ it.label }}
        <span v-if="it.tone" class="vc-tone" :class="it.tone">{{ it.toneText }}</span>
      </div>
      <div class="vc-value" :style="{ color: it.color }">
        {{ it.value }}<span v-if="it.unit" class="unit">{{ it.unit }}</span>
      </div>
      <div class="vc-meta">
        <span class="chip gray">
          {{ it.direction === 'higher_better' ? '↑ 越高越好' : it.direction === 'none' ? '· 观察型（无方向）' : '↓ 越低越好' }}
        </span>
        <span v-if="it.evidence" class="chip gray" :title="`证据等级 ${it.evidence}`">{{ it.evidence }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* UI 反馈 #4：拖拽换位——抓手光标 + 拖起半透明 + 落点高亮 */
.value-card[draggable='true'] {
  cursor: grab;
}

.value-card.dragging {
  opacity: 0.45;
}

.value-card.drop-hint {
  border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent);
}

/* R31 阈值达标着色：左边框指示（数值本体保留指标色，达标态由边框+角标表达） */
.value-card.good {
  border-left: 3px solid #3fb950;
}

.value-card.bad {
  border-left: 3px solid #e5534b;
}

.vc-tone {
  font-size: 9.5px;
  padding: 0 5px;
  border-radius: 8px;
  white-space: nowrap;
}

.vc-tone.good {
  color: #3fb950;
  border: 1px solid rgba(63, 185, 80, 0.5);
  background: rgba(63, 185, 80, 0.12);
}

.vc-tone.bad {
  color: #e5534b;
  border: 1px solid rgba(229, 83, 75, 0.5);
  background: rgba(229, 83, 75, 0.12);
}
</style>

