<script setup lang="ts">
import { computed } from 'vue'
import type { Device, MetricDef } from '../api-client'
import { metricMeta } from '../metricMeta'
import { METRIC_GROUPS, metricUsability, ungroupedMetricIds } from '../capability'
import { useStore } from '../store'

/**
 * 指标参数控制面板（R21+R26，对标 [I§2.4] 右滑面板 + [I§6] 能力矩阵）：
 * - 勾选同时控制「采集」与「显示」；勾选后框体变深（「框体变深，数据表格展示」）；
 * - 可用性 = capabilities ∩ 设备 profile 交集判定（§20.3：平台一致 + modes 交集 +
 *   资源依赖），叠加服务端 available 权威判定；禁勾行渲染 reason 映射文案（tooltip）；
 * - 按组渲染：CPU 高阶组（频率/规范化/线程，R16）、Memory Detail 组（R17，官方损耗
 *   提示文案随组渲染，§21.3 description / [F§3.8][I§2.4]）。
 */
const store = useStore()

defineEmits<{ (e: 'close'): void }>()

const INTERVALS = [500, 1000, 2000, 5000]

const selectedDevice = computed<Device | null>(
  () => store.state.devices.find((d) => d.id === store.state.selectedDeviceId) ?? null,
)

interface RowVM {
  def: MetricDef
  usable: boolean
  reasons: string[]
  titleText: string
}

function rowOf(def: MetricDef): RowVM {
  const u = metricUsability(def, selectedDevice.value)
  const meta = metricMeta(def.metricId)
  const parts: string[] = []
  if (meta.desc) parts.push(meta.desc)
  if (def.description) parts.push(def.description)
  if (u.reasons.length > 0) parts.push(`不可用原因：${u.reasons.join('；')}`)
  return { def, usable: u.usable, reasons: u.reasons, titleText: parts.join('\n') }
}

/** 分组行（组内全为禁勾时整组折叠提示，但仍渲染行以保持「指标行随模式/平台动态增减」的可见性） */
const groupsWithRows = computed<Array<{ id: string; name: string; note?: string; rows: RowVM[] }>>(() => {
  const defs = store.state.metricDefs
  const byId = new Map(defs.map((d) => [d.metricId, d]))
  const out: Array<{ id: string; name: string; note?: string; rows: RowVM[] }> = []
  for (const g of METRIC_GROUPS) {
    const rows: RowVM[] = []
    if (g.metricIds) {
      for (const id of g.metricIds) {
        const def = byId.get(id)
        if (def) rows.push(rowOf(def))
      }
    } else if (g.prefix) {
      for (const def of defs) if (def.metricId.startsWith(g.prefix)) rows.push(rowOf(def))
    }
    if (rows.length > 0) {
      // 可用性置顶（UI 反馈 #2）：可用行在前、禁勾行沉底；稳定排序保持组内原序
      rows.sort((a, b) => Number(b.usable) - Number(a.usable))
      out.push({ id: g.id, name: g.name, note: g.note, rows })
    }
  }
  const rest = ungroupedMetricIds(defs)
    .map((id) => byId.get(id))
    .filter((d): d is MetricDef => Boolean(d))
  if (rest.length > 0) {
    const restRows = rest.map((d) => rowOf(d))
    restRows.sort((a, b) => Number(b.usable) - Number(a.usable))
    out.push({ id: 'ungrouped', name: '其他', rows: restRows })
  }
  return out
})

const selectedCount = computed(() => Object.keys(store.state.selected).length)

function isChecked(row: RowVM): boolean {
  return store.state.selected[row.def.metricId] !== undefined
}

function currentInterval(row: RowVM): number {
  return store.state.selected[row.def.metricId] ?? row.def.defaultIntervalMs ?? 1000
}

function onRowClick(row: RowVM): void {
  if (!row.usable) {
    // 禁勾行点击：如实给出原因（不写 0、不静默）
    store.setError(`指标 ${row.def.metricId} 禁勾：${row.reasons.join('；')}`)
    return
  }
  void store.toggleMetric(row.def.metricId, !isChecked(row))
}

function onCheck(row: RowVM, ev: Event): void {
  ev.stopPropagation()
  if (!row.usable) {
    store.setError(`指标 ${row.def.metricId} 禁勾：${row.reasons.join('；')}`)
    return
  }
  void store.toggleMetric(row.def.metricId, (ev.target as HTMLInputElement).checked)
}

function onInterval(row: RowVM, ev: Event): void {
  ev.stopPropagation()
  void store.setMetricInterval(row.def.metricId, Number((ev.target as HTMLSelectElement).value))
}
</script>

<template>
  <div class="drawer-mask" @click="$emit('close')"></div>
  <aside class="drawer">
    <div class="drawer-head">
      <div>
        <div style="font-weight: 600">指标采集面板</div>
        <div class="hint">
          勾选 = 采集 + 显示（「勾选，表示需要采集数据」[I§2.4]）·
          可用性 = capabilities ∩ 设备 profile（§20.3）·
          当前设备：{{ selectedDevice?.model ?? '未选择' }}
        </div>
      </div>
      <button class="ghost" @click="$emit('close')">✕</button>
    </div>
    <div class="drawer-body">
      <div v-for="g in groupsWithRows" :key="g.id" class="mp-group">
        <div class="mp-group-head">
          <span>{{ g.name }}</span>
          <span v-if="g.note" class="mp-group-note" :title="g.note">ⓘ</span>
        </div>
        <!-- R26：官方损耗提示文案随组渲染（§21.3 description，[F§3.8][I§2.4]） -->
        <div v-if="g.note" class="mp-group-loss">{{ g.note }}</div>

        <div
          v-for="row in g.rows"
          :key="row.def.metricId"
          class="metric-row"
          :class="{ checked: isChecked(row), disabled: !row.usable }"
          :title="row.titleText"
          @click="onRowClick(row)"
        >
          <span class="swatch" :style="{ background: metricMeta(row.def.metricId).color }"></span>
          <div class="m-main">
            <div class="m-name">
              {{ metricMeta(row.def.metricId).label }}
              <span v-if="!row.usable" class="chip red mp-reason" :title="row.reasons.join('；')">禁勾</span>
            </div>
            <div class="m-desc">
              {{ row.def.description || metricMeta(row.def.metricId).desc || row.def.metricId }}
            </div>
            <!-- reason 映射文案（§20.1/§20.3：人工文案由前端映射表渲染） -->
            <div v-if="!row.usable" class="mp-reason-text">{{ row.reasons.join('；') }}</div>
          </div>
          <span class="chip gray" :title="`证据等级 ${row.def.evidenceLevel}`">{{ row.def.evidenceLevel }}</span>
          <select
            v-if="row.usable"
            :value="currentInterval(row)"
            title="采样间隔（Sampling Frequency）"
            @change="onInterval(row, $event)"
            @click.stop
          >
            <option v-for="ms in INTERVALS" :key="ms" :value="ms">{{ ms >= 1000 ? `${ms / 1000}s` : `${ms}ms` }}</option>
          </select>
          <input
            type="checkbox"
            :checked="isChecked(row)"
            :disabled="!row.usable"
            @click.stop
            @change="onCheck(row, $event)"
          />
        </div>
      </div>

      <div class="hint" style="margin-top: 8px">
        已勾选 {{ selectedCount }} 项 · 高频采样会增加采集开销（R26 开销提示：极限测试建议少勾
        Memory Detail / 截屏类指标）；禁勾行随设备 profile 与模式动态增减（[I§6]），
        服务端 available 为权威判定（§20.3），落点仍受 metric_unavailable 拦截。
      </div>
    </div>
  </aside>
</template>

<style scoped>
.mp-group {
  margin-bottom: 10px;
}

.mp-group-head {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 10.5px;
  color: var(--text-faint);
  text-transform: uppercase;
  letter-spacing: 0.4px;
  margin: 8px 0 4px;
  border-bottom: 1px dashed var(--border);
  padding-bottom: 2px;
}

.mp-group-note {
  cursor: help;
}

.mp-group-loss {
  font-size: 10.5px;
  color: var(--warn);
  background: rgba(210, 153, 34, 0.08);
  border: 1px solid rgba(210, 153, 34, 0.35);
  border-radius: 6px;
  padding: 4px 7px;
  margin-bottom: 4px;
}

.mp-reason {
  font-size: 10px;
  margin-left: 4px;
  cursor: help;
}

.mp-reason-text {
  font-size: 10.5px;
  color: var(--danger);
  margin-top: 2px;
}
</style>
