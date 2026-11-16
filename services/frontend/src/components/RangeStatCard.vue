<script setup lang="ts">
import { computed } from 'vue'
import type { Statistics } from '../api-client'

/**
 * R30 框选区间统计卡（[I§2.9] web_case_chart「蓝色框选区间浮出区间统计卡」形态）：
 * - 起止时间戳 + Avg/Std/Min/占比（FPS>=x[%]，阈值联动）+ Jank 族（Small/Jank/Big/Stutter//10min）；
 * - 数据来自 §12/§18 statistics?fromMs&toMs（区间闭区间含端点；fpsGe 阈值由父组件透传）；
 * - 卡上 Save（[I§4.4]「框选即统计 + Save」）：把区间统计写为一条 note（§15.2 POST /notes，
 *   tsMs = 区间起点），仅会话可编辑窗口可用。
 */
const props = defineProps<{
  fromMs: number
  toMs: number
  stats: Statistics | null
  loading: boolean
  errorMsg: string
  /** Save 可用（§15.2 可编辑窗口 = monitoring/recording/stopped） */
  canSave: boolean
  saveBusy?: boolean
  savedText?: string
  fpsNote?: string
  /** 相对时间戳原点（epoch ms；会话起点），卡头 `mm:ss.mmm – mm:ss.mmm` 形态用 */
  originTs?: number
}>()

const emit = defineEmits<{
  'save': []
  'close': []
}>()

/** 数值格式化：null/非有限值显示 —（不填假零值 [R§8.6]），千位以上去小数 */
/** 相对时间戳 mm:ss.mmm（PerfDog 区间卡 `00:30:129-00:46:007` 形态 [I§2.9]；原点 = 会话起点） */
function fmtRel(ts: number): string {
  const origin = typeof props.originTs === 'number' ? props.originTs : 0
  const rel = Math.max(0, ts - origin)
  const ms = Math.floor(rel % 1000)
  const total = Math.floor(rel / 1000)
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}.${String(ms).padStart(3, '0')}`
}

function fmtNum(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  if (Math.abs(v) >= 1000) return v.toFixed(0)
  if (Number.isInteger(v)) return String(v)
  return v.toFixed(1)
}

interface Row {
  label: string
  value: string
  tip?: string
}

const statRows = computed<Row[]>(() => {
  const s = props.stats
  if (!s) return []
  const rows: Row[] = [
    { label: 'Avg(FPS)', value: fmtNum(s.fps.avg), tip: 'fps 点列均值 [F§3.1]' },
    { label: 'Std(FPS)', value: fmtNum(s.fps.std), tip: 'fps 点列标准差（总体口径）[F§3.1]' },
    { label: 'Min(FPS)', value: fmtNum(s.fps.min), tip: 'fps 点列最小值 [F§3.1]' },
    { label: 'Smooth', value: fmtNum(s.smooth.value), tip: s.smooth.note },
    { label: '1%Low(FPS)', value: fmtNum(s.lowFps.p1), tip: '帧耗时最长 1% 均值换算 fps [F§3.5]' },
    { label: 'SmallJank', value: fmtNum(s.jank.small), tip: '双条件判定 [F§3.3]' },
    { label: 'Jank', value: fmtNum(s.jank.jank), tip: '双条件判定 [F§3.3]' },
    { label: 'BigJank', value: fmtNum(s.jank.big), tip: '双条件判定 [F§3.3]' },
    { label: 'Stutter', value: s.jank.stutter === null ? '—' : `${fmtNum(s.jank.stutter)}%`, tip: 'Σ Jank 帧耗时 / durationMs [F§3.4]' },
    { label: 'Jank(/10min)', value: fmtNum(s.jank.per10min), tip: 'Jank × 600000 / durationMs [F§3.3]' },
    { label: 'Avg(FTime)', value: s.frameTime.avg === null ? '—' : `${fmtNum(s.frameTime.avg)}ms`, tip: s.frameTime.note ?? '逐帧耗时均值 [F§3.2]' },
  ]
  // FPS>=x[%] 占比行（键来自响应 ge，随 R31 阈值输入联动 [I§2.9] `FPS>=18[%]` 卡形态）
  const geKeys = Object.keys(s.fps.ge).sort((a, b) => Number(a) - Number(b))
  for (const t of geKeys) {
    rows.push({
      label: `FPS>=${t}`,
      value: s.fps.ge[t] === null || s.fps.ge[t] === undefined ? '—' : `${fmtNum(s.fps.ge[t]!)}%`,
      tip: `区间内 FPS ≥ ${t} 的点占比（阈值输入联动）[F§3.1]`,
    })
  }
  return rows
})

function onSave(): void {
  if (props.canSave && !props.saveBusy) emit('save')
}
</script>

<template>
  <div class="range-card">
    <div class="rc-head">
      <span class="rc-title">区间统计</span>
      <span class="rc-range mono">{{ fmtRel(fromMs) }} – {{ fmtRel(toMs) }}（{{ fromMs }}–{{ toMs }} ms epoch）</span>
      <span style="flex: 1"></span>
      <button v-if="canSave" class="rc-save" :disabled="saveBusy" :title="`Save：把区间统计写为一条批注（§15.2 POST /notes，tsMs=区间起点）${savedText ? `（已保存：${savedText}）` : ''}`" @click="onSave">
        {{ saveBusy ? '保存中…' : savedText ? '已保存 ✓' : 'Save' }}
      </button>
      <button class="rc-close" title="清除框选（各图选区同步清除）" @click="emit('close')">✕</button>
    </div>
    <div v-if="loading" class="rc-body rc-hint">区间统计计算中…（GET /api/sessions/{id}/statistics?fromMs&amp;toMs）</div>
    <div v-else-if="errorMsg" class="rc-body rc-hint rc-err">区间统计不可用：{{ errorMsg }}</div>
    <div v-else-if="stats" class="rc-body">
      <div class="rc-grid">
        <div v-for="r in statRows" :key="r.label" class="rc-cell" :data-tip="r.tip">
          <span class="rc-label">{{ r.label }}</span>
          <span class="rc-value">{{ r.value }}</span>
        </div>
      </div>
      <div v-if="fpsNote" class="rc-note">※ {{ fpsNote }}</div>
    </div>
  </div>
</template>

<style scoped>
.range-card {
  position: relative;
  z-index: 6;
  background: #1a222dee;
  border: 1px solid rgba(47, 129, 247, 0.55);
  border-radius: 8px;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.45);
  padding: 8px 10px;
}

.rc-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.rc-title {
  font-size: 12.5px;
  font-weight: 650;
  color: #79b8ff;
}

.rc-range {
  font-size: 11px;
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.rc-save {
  font-size: 11px;
  padding: 2px 10px;
  border: 1px solid #2f81f7;
  border-radius: 5px;
  background: rgba(47, 129, 247, 0.16);
  color: #79b8ff;
  cursor: pointer;
}

.rc-save:disabled {
  opacity: 0.6;
  cursor: default;
}

.rc-close {
  background: none;
  border: none;
  color: var(--text-faint);
  cursor: pointer;
  padding: 0 2px;
  font-size: 12px;
}

.rc-close:hover {
  color: var(--text);
}

.rc-hint {
  font-size: 11px;
  color: var(--text-dim);
}

.rc-err {
  color: #f85149;
}

.rc-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(118px, 1fr));
  gap: 4px 10px;
}

.rc-cell {
  position: relative;
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 6px;
  font-size: 11.5px;
  border-bottom: 1px dashed #253040;
  padding: 1px 0;
  cursor: help;
}

.rc-cell::after {
  content: attr(data-tip);
  position: absolute;
  left: 0;
  top: calc(100% + 4px);
  z-index: 30;
  width: max-content;
  max-width: 300px;
  background: #1a222d;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text);
  font-size: 10.5px;
  line-height: 1.5;
  padding: 5px 8px;
  white-space: normal;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.1s;
}

.rc-cell:hover::after {
  opacity: 1;
}

.rc-label {
  color: var(--text-dim);
  white-space: nowrap;
}

.rc-value {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}

.rc-note {
  margin-top: 6px;
  font-size: 10.5px;
  color: var(--text-faint);
  line-height: 1.5;
}
</style>
