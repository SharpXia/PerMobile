<script lang="ts">
import type { Statistics } from '../api-client'

/**
 * R32 Label 分段统计表（[I§2.9]「Label 分段统计表」）：
 * - 行 = 标签段：All（全量）+ 每个 label 段（段 = 打标时刻 → 下次打标/数据结束 [F§6]）；
 * - 列 = 全部统计指标（§12 统计层：FPS 族 / FPS>=x[%] 占比 / Jank 族 / FTime 族 / Smooth / 1%Low）；
 * - 右上「参数显示设置」列勾选显隐：**只影响显示不影响导出**（[I§4.2] Web 报告侧
 *   `参数显示设置…只影响显示，不影响导出，刷新还原默认`）——表格导出走 M2 Excel 通道
 *   （报告页「导出全部 (excel)」，§13.1），本表不设独立导出按钮；
 * - 数据 = §12/§18 statistics?fromMs&toMs（每段一次区间查询，闭区间含端点）。
 */

/** 分段行视图模型（父组件负责逐段拉取 §12/§18 区间统计并填充） */
export interface SegmentRowVM {
  /** 行名（All / 段名） */
  name: string
  /** 段着色（All 行无色） */
  color?: string
  /** 区间（epoch ms）；缺省 = 全量查询（All 行） */
  fromMs?: number
  toMs?: number
  stats: Statistics | null
  loading: boolean
  errorMsg: string
}

/** 统计列定义：read 从 Statistics 读出一个显示值（null → '—'，不填假零值 [R§8.6]） */
export interface SegmentColumn {
  key: string
  label: string
  read: (s: Statistics) => number | null
  /** 后缀单位（% / ms 等） */
  unit?: string
  tip: string
}

function fmt(v: number | null): string {
  if (v === null || !Number.isFinite(v)) return '—'
  if (Math.abs(v) >= 1000) return v.toFixed(0)
  if (Number.isInteger(v)) return String(v)
  return v.toFixed(1)
}

/** 静态列清单（[I§2.9] 列 = 全部统计指标；顺序对齐报告 FPS Tab 卡矩阵） */
export const SEGMENT_COLUMNS: SegmentColumn[] = [
  { key: 'fps.avg', label: 'Avg(FPS)', read: (s) => s.fps.avg, tip: 'fps 点列均值 [F§3.1]' },
  { key: 'fps.std', label: 'Std(FPS)', read: (s) => s.fps.std, tip: 'fps 点列标准差（总体口径）[F§3.1]' },
  { key: 'fps.var', label: 'Var(FPS)', read: (s) => s.fps.var, tip: 'fps 点列方差（总体口径）[F§3.1]' },
  { key: 'fps.drop', label: 'Drop(FPS)', read: (s) => s.fps.drop, unit: '/h', tip: '相邻点下降 >8 帧次数 × 3600000 / durationMs [F§3.1]' },
  { key: 'fps.min', label: 'Min(FPS)', read: (s) => s.fps.min, tip: 'fps 点列最小值 [F§3.1]' },
  { key: 'fps.median', label: 'Median', read: (s) => s.fps.median, tip: 'fps 点列中位数 [F§3.1]' },
  { key: 'fps.medRange', label: 'MedRange', read: (s) => s.fps.medRange, unit: '%', tip: '偏离中位数 ±20% 以内点占比 [F§3.1]' },
  { key: 'smooth', label: 'Smooth(稳帧指数)', read: (s) => s.smooth.value, tip: '近似口径：官方未公开公式 [F§3.5][F§10.1]' },
  { key: 'lowFps', label: '1%Low(FPS)', read: (s) => s.lowFps.p1, tip: '帧耗时最长 1% 均值换算 fps [F§3.5]' },
  { key: 'jank.small', label: 'SmallJank', read: (s) => s.jank.small, tip: '双条件判定 [F§3.3]' },
  { key: 'jank.jank', label: 'Jank', read: (s) => s.jank.jank, tip: '双条件判定 [F§3.3]' },
  { key: 'jank.big', label: 'BigJank', read: (s) => s.jank.big, tip: '双条件判定 [F§3.3]' },
  { key: 'jank.stutter', label: 'Stutter', read: (s) => s.jank.stutter, unit: '%', tip: 'Σ Jank 帧耗时 / durationMs [F§3.4]' },
  { key: 'jank.per10min', label: 'Jank(/10min)', read: (s) => s.jank.per10min, tip: 'Jank × 600000 / durationMs [F§3.3]' },
  { key: 'ftime.avg', label: 'Avg(FTime)', read: (s) => s.frameTime.avg, unit: 'ms', tip: '逐帧耗时均值 [F§3.2]' },
  { key: 'ftime.std', label: 'Std(FTime)', read: (s) => s.frameTime.std, unit: 'ms', tip: '逐帧耗时标准差 [F§3.2]' },
  { key: 'ftime.var', label: 'Var(FTime)', read: (s) => s.frameTime.var, unit: 'ms²', tip: '逐帧耗时方差 [F§3.2]' },
  { key: 'ftime.delta', label: 'Delta(FTime)', read: (s) => s.frameTime.delta, unit: '/h', tip: '相邻两帧时间差 >100ms 次数 × 3600000 / durationMs [F§3.2]' },
]
</script>

<script setup lang="ts">
import { computed, ref } from 'vue'

const props = defineProps<{ rows: SegmentRowVM[] }>()

/** FPS>=x[%] 动态列（键来自各段响应 ge 并集，随 R31 阈值输入联动） */
const geColumns = computed<SegmentColumn[]>(() => {
  const keys = new Set<string>()
  for (const r of props.rows) {
    if (!r.stats) continue
    for (const k of Object.keys(r.stats.fps.ge)) keys.add(k)
  }
  return [...keys]
    .sort((a, b) => Number(a) - Number(b))
    .map((t) => ({
      key: `fps.ge.${t}`,
      label: `FPS>=${t}`,
      read: (s: Statistics) => s.fps.ge[t] ?? null,
      unit: '%',
      tip: `FPS ≥ ${t} 的点占比（阈值输入联动）[F§3.1]`,
    }))
})

/** 全列 = FPS 族 + ge 动态列 + Jank/FTime 族（顺序与报告 FPS Tab 卡矩阵一致） */
const allColumns = computed<SegmentColumn[]>(() => {
  const jankIdx = SEGMENT_COLUMNS.findIndex((c) => c.key === 'jank.small')
  const head = SEGMENT_COLUMNS.slice(0, jankIdx === -1 ? SEGMENT_COLUMNS.length : jankIdx)
  const tail = SEGMENT_COLUMNS.slice(jankIdx === -1 ? SEGMENT_COLUMNS.length : jankIdx)
  return [...head, ...geColumns.value, ...tail]
})

/** 「参数显示设置」列显隐（R32：只影响显示不影响导出；默认全显，刷新还原 = 组件重建即默认） */
const hiddenCols = ref<Set<string>>(new Set())
const colSettingsOpen = ref(false)

const visibleColumns = computed<SegmentColumn[]>(() => allColumns.value.filter((c) => !hiddenCols.value.has(c.key)))

function toggleCol(key: string): void {
  const next = new Set(hiddenCols.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  hiddenCols.value = next
}

function cellText(row: SegmentRowVM, col: SegmentColumn): string {
  if (!row.stats) return '—'
  const v = col.read(row.stats)
  return `${fmt(v)}${v === null ? '' : (col.unit ?? '')}`
}

function rowTip(row: SegmentRowVM): string {
  if (row.errorMsg) return `该段统计不可用：${row.errorMsg}`
  if (row.loading) return '该段统计计算中…'
  const s = row.stats
  if (!s) return ''
  const notes = [s.fps.note, s.frameTime.note].filter(Boolean).join(' / ')
  return notes ? `口径 note：${notes}` : ''
}
</script>

<template>
  <div class="seg-table-wrap">
    <div class="seg-toolbar">
      <button class="mini" @click="colSettingsOpen = !colSettingsOpen">
        参数显示设置{{ hiddenCols.size > 0 ? `（隐藏 ${hiddenCols.size} 列）` : '' }} ▾
      </button>
      <span class="seg-toolbar-note">
        列勾选只影响显示，不影响导出 —— 表格导出走 M2 Excel 通道（报告页「导出全部 (excel)」），导出内容不受勾选影响 [I§4.2]；刷新还原默认全显。
      </span>
    </div>
    <div v-if="colSettingsOpen" class="seg-col-settings">
      <label v-for="c in allColumns" :key="c.key" class="seg-col-check" :title="c.tip">
        <input type="checkbox" :checked="!hiddenCols.has(c.key)" @change="toggleCol(c.key)" />
        <span>{{ c.label }}</span>
      </label>
    </div>
    <div class="seg-scroll">
      <table class="seg-table">
        <thead>
          <tr>
            <th class="seg-name-col">Label / 段</th>
            <th v-for="c in visibleColumns" :key="c.key" :title="c.tip">{{ c.label }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.name" :title="rowTip(row)">
            <td class="seg-name-col">
              <span v-if="row.color" class="seg-swatch" :style="{ background: row.color }"></span>
              <span>{{ row.name }}</span>
              <span v-if="row.fromMs !== undefined" class="seg-range mono">{{ row.fromMs }}–{{ row.toMs }}</span>
            </td>
            <td
              v-for="c in visibleColumns"
              :key="c.key"
              :class="{ dim: !row.stats || (!row.loading && c.read(row.stats) === null) }"
            >
              <template v-if="row.loading">…</template>
              <template v-else-if="row.errorMsg && !row.stats">不可用</template>
              <template v-else>{{ cellText(row, c) }}</template>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.seg-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
}

.seg-toolbar-note {
  font-size: 10.5px;
  color: var(--text-faint);
  line-height: 1.5;
}

.seg-col-settings {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  padding: 6px 10px;
  margin-bottom: 8px;
}

.seg-col-check {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--text-dim);
  cursor: pointer;
  white-space: nowrap;
}

.seg-scroll {
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.seg-table {
  border-collapse: collapse;
  width: 100%;
  font-size: 11.5px;
}

.seg-table th,
.seg-table td {
  border-bottom: 1px solid #1c2531;
  padding: 4px 9px;
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.seg-table th {
  background: var(--bg-panel-2);
  color: var(--text-dim);
  font-weight: 600;
  font-size: 10.5px;
  cursor: help;
}

.seg-table td {
  color: var(--text);
}

.seg-table td.dim {
  color: var(--text-faint);
}

.seg-table tr:last-child td {
  border-bottom: none;
}

.seg-name-col {
  text-align: left !important;
  color: var(--text-dim);
}

.seg-swatch {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 2px;
  margin-right: 5px;
  vertical-align: baseline;
}

.seg-range {
  margin-left: 6px;
  font-size: 10px;
  color: var(--text-faint);
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
</style>
